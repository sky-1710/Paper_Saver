"""The GPT tab: ask about the paper, with captures of it attached.

The conversation belongs to the paper and comes back when it is reopened.
Captures wait in a strip above the input until the question is sent; a
picture pasted into the input (Ctrl+V of a screenshot) joins them. Every
answer carries a "노트에 넣기" link that puts the question, its captures and the
answer into the paper's note.

Asking is the reader's explicit act, and it is the only way a picture of the
paper leaves the machine (§5.3): the captures attached to that question, and
nothing else.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.chat import ChatLog, ChatMessage
from .keys import CAPTURE_LABEL
from .markdown_view import ImagePasteEdit, markdown_html

__all__ = ["ChatPanel"]

THUMB = 56


class ChatPanel(QWidget):
    #: Send: question text, attached images (relative to the project).
    sendRequested = Signal(str, list)
    stopRequested = Signal()
    #: Put an answer into the note: the assistant message's id.
    toNoteRequested = Signal(str)
    #: A picture was pasted or dropped into the input.
    imagePasted = Signal(QImage)

    def __init__(self, log: ChatLog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log = log
        self.pending: list[str] = []
        self._busy = False
        self._error = ""

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)

        self.attachments = QHBoxLayout()
        self.attachments.setContentsMargins(0, 0, 0, 0)
        self.attachments.setSpacing(4)
        self._strip = QWidget()
        self._strip.setLayout(self.attachments)

        self.input = ImagePasteEdit()
        self.input.submit_on_enter = True
        self.input.setPlaceholderText(
            "논문에 대해 물어보세요. Enter 보내기 · Shift+Enter 줄바꿈\n"
            f"캡처({CAPTURE_LABEL})나 붙여 넣은 이미지를 함께 보낼 수 있습니다."
        )
        self.input.setFixedHeight(76)
        self.input.submitted.connect(self.send)
        self.input.imagePasted.connect(self.imagePasted)

        self.send_button = QPushButton("보내기")
        self.send_button.clicked.connect(self._send_or_stop)
        clear = QPushButton("새 대화")
        clear.setToolTip("대화 기록을 지웁니다 (캡처 파일은 남습니다)")
        clear.clicked.connect(self._clear)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(clear)
        buttons.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.view, 1)
        layout.addWidget(self._strip)
        layout.addWidget(self.input)
        layout.addLayout(buttons)
        self._refresh_strip()
        self.render()

    # -- the transcript ------------------------------------------------------------

    def render(self) -> None:
        width = max(160, self.view.viewport().width() - 40)
        parts: list[str] = []
        if not self.log.messages:
            parts.append(
                "<p style='color:#666'>이 논문에 대해 GPT에게 물어볼 수 있습니다.<br>"
                f"그림·표·수식은 <b>캡처</b>(툴바 또는 {CAPTURE_LABEL})로 잘라서 함께 보내세요.</p>"
                "<p style='color:#999;font-size:9pt'>질문과 그때 첨부한 캡처, 보고 있는 쪽의 본문이 "
                "Codex를 통해 OpenAI로 전송됩니다.</p>"
            )
        for message in self.log.messages:
            parts.append(self._message_html(message, width))
        if self._busy:
            parts.append("<p style='color:#888'><i>GPT가 답을 쓰는 중…</i></p>")
        if self._error:
            text = html.escape(self._error).replace("\n", "<br>")
            parts.append(f"<p style='color:#b00'>{text}</p>")
        bar = self.view.verticalScrollBar()
        self.view.setHtml("".join(parts))
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def _message_html(self, message: ChatMessage, width: int) -> str:
        if message.role == "user":
            where = f" <span style='color:#888'>· p.{message.page + 1}</span>" if message.page is not None else ""
            text = html.escape(message.text).replace("\n", "<br>")
            images = "".join(
                f"<a href='{QUrl.fromLocalFile(str(self.log.absolute(i))).toString()}'>"
                f"<img src='{QUrl.fromLocalFile(str(self.log.absolute(i))).toString()}' width='{min(width, 240)}'></a> "
                for i in message.images
            )
            return (
                "<table width='100%' cellpadding='7' style='background:#eef3fd;margin-top:10px'><tr><td>"
                f"<b style='color:#1a4fb4'>나</b>{where}<br>{text}{'<br>' + images if images else ''}"
                "</td></tr></table>"
            )
        body = markdown_html(message.text, None, width)
        return (
            f"<p style='margin-top:10px'><b style='color:#188038'>GPT</b> "
            f"&nbsp;<a href='note:{message.id}' style='font-size:9pt'>노트에 넣기</a></p>{body}"
        )

    def _link(self, url: QUrl) -> None:
        if url.scheme() == "note":
            self.toNoteRequested.emit(url.path())
            return
        QDesktopServices.openUrl(url)

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)  # type: ignore[arg-type]
        QTimer.singleShot(0, self.render)

    # -- attachments ---------------------------------------------------------------

    def attach(self, image: str) -> None:
        if image not in self.pending:
            self.pending.append(image)
        self._refresh_strip()
        self.input.setFocus()

    def _refresh_strip(self) -> None:
        while self.attachments.count():
            item = self.attachments.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for image in self.pending:
            button = QToolButton()
            pixmap = QPixmap(str(self.log.absolute(image)))
            button.setIcon(QIcon(pixmap.scaled(THUMB, THUMB, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            button.setIconSize(QSize(THUMB, THUMB))
            button.setToolTip("클릭하면 첨부에서 뺍니다")
            button.clicked.connect(lambda _=False, i=image: self._detach(i))
            self.attachments.addWidget(button)
        if self.pending:
            label = QLabel(f"캡처 {len(self.pending)}장 첨부")
            label.setObjectName("muted")
            self.attachments.addWidget(label)
        self.attachments.addStretch(1)
        self._strip.setVisible(bool(self.pending))

    def _detach(self, image: str) -> None:
        self.pending = [i for i in self.pending if i != image]
        self._refresh_strip()

    # -- sending -----------------------------------------------------------------------

    def _send_or_stop(self) -> None:
        if self._busy:
            self.stopRequested.emit()
        else:
            self.send()

    def send(self) -> None:
        question = self.input.toPlainText().strip()
        if self._busy or (not question and not self.pending):
            return
        self._error = ""
        self.sendRequested.emit(question, list(self.pending))

    def accepted(self) -> None:
        """The question went out: clear the input and wait."""
        self.input.clear()
        self.pending = []
        self._refresh_strip()
        self.set_busy(True)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_button.setText("중지" if busy else "보내기")
        self.status.setText("Codex에 묻는 중… (보통 10–60초)" if busy else "")
        self.render()

    def failed(self, message: str, question: str, images: list[str]) -> None:
        """Put the question back so it can be sent again, and say why."""
        self._error = message
        if not self.input.toPlainText().strip():
            self.input.setPlainText(question)
        self.pending = list(dict.fromkeys([*images, *self.pending]))
        self._refresh_strip()
        self.set_busy(False)

    def _clear(self) -> None:
        if self._busy or not self.log.messages:
            return
        answer = QMessageBox.question(self, "새 대화", "이 논문의 GPT 대화 기록을 지울까요?")
        if answer == QMessageBox.StandardButton.Yes:
            self.log.clear()
            self._error = ""
            self.render()

    def show_status(self, text: str) -> None:
        self.status.setText(text)
