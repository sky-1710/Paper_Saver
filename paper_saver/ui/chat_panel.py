"""The GPT tab: ask about the paper, with captures of it attached.

The conversation belongs to the paper and comes back when it is reopened.
Captures wait in a strip above the input until the question is sent; a
picture pasted into the input (Ctrl+V of a screenshot) joins them. Every
answer carries a "노트에 넣기" link that puts the question, its captures and the
answer into the paper's note. An answer's citations show as page links;
following one scrolls the reader to those sentences and lights them.

Files can be attached too (📎, or dropped on the input): a PDF, notes, a
transcript, or another paper opened before. Their text goes with every
question until the reader takes them off - the chips above the input say
what is attached.

Asking is the reader's explicit act, and it is the only way a picture of the
paper leaves the machine (§5.3): the captures attached to that question, and
nothing else.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, unquote

from PySide6.QtCore import QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.chat import ChatFile, ChatLog, ChatMessage, render_citations
from ..core.notebook import IMAGE_SUFFIXES
from ..pdf.text import MATERIAL_SUFFIXES
from .keys import CAPTURE_LABEL
from .markdown_view import ImagePasteEdit, markdown_html

__all__ = ["ChatPanel"]

THUMB = 56

#: The text a question can carry, as the selector offers it.
SCOPES = (("보는 쪽", "page"), ("이 논문", "paper"), ("모든 탭", "tabs"))
SCOPE_SAYS = {"page": "보고 있는 쪽을", "paper": "논문 전체를", "tabs": "열린 탭의 논문 전체를"}


class ChatPanel(QWidget):
    #: Send: question text, attached images (relative to the project).
    sendRequested = Signal(str, list)
    stopRequested = Signal()
    #: Put an answer into the note: the assistant message's id.
    toNoteRequested = Signal(str)
    #: A picture was pasted or dropped into the input.
    imagePasted = Signal(QImage)
    #: Show these sentences in the reader: the ids an answer cited.
    evidenceRequested = Signal(list)
    #: Attach these files (list of Paths): picked with 📎 or dropped.
    filesChosen = Signal(list)
    #: Attach another paper: its project folder.
    paperChosen = Signal(str)

    def __init__(
        self,
        log: ChatLog,
        parent: QWidget | None = None,
        *,
        page_of: Callable[[str], int | None] | None = None,
        recent_papers: Callable[[], list[tuple[str, str]]] | None = None,
        paper_of: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.log = log
        #: The page a sentence id is on, for labelling citations.
        self._page_of = page_of or (lambda _id: None)
        #: Other papers that can be attached: (title, project folder).
        self._recent_papers = recent_papers or (lambda: [])
        #: The short title of the paper a cited sentence is in; "" for this one.
        self._paper_of = paper_of or (lambda _id: "")
        self.pending: list[str] = []
        self._busy = False
        self._error = ""

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.anchorClicked.connect(self._link)

        # Attached files (they stay), then this question's captures.
        self.file_chips = QHBoxLayout()
        self.file_chips.setContentsMargins(0, 0, 0, 0)
        self.file_chips.setSpacing(4)
        self.attachments = QHBoxLayout()
        self.attachments.setContentsMargins(0, 0, 0, 0)
        self.attachments.setSpacing(4)
        self._strip = QWidget()
        strip = QVBoxLayout(self._strip)
        strip.setContentsMargins(0, 0, 0, 0)
        strip.setSpacing(3)
        strip.addLayout(self.file_chips)
        strip.addLayout(self.attachments)

        self.input = ImagePasteEdit()
        self.input.submit_on_enter = True
        self.input.accept_documents = True
        self.input.setPlaceholderText(
            "논문에 대해 물어보세요. Enter 보내기 · Shift+Enter 줄바꿈\n"
            f"캡처({CAPTURE_LABEL})·이미지, 📎로 PDF·txt·md 파일이나 다른 논문을 함께 보낼 수 있습니다."
        )
        self.input.setFixedHeight(76)
        self.input.submitted.connect(self.send)
        self.input.imagePasted.connect(self.imagePasted)
        self.input.documentsDropped.connect(self.filesChosen)

        self.attach_menu = QMenu(self)
        self.attach_menu.addAction("파일 첨부… (PDF·txt·md·srt·vtt·이미지)", self._pick_files)
        self._papers_menu = self.attach_menu.addMenu("다른 논문 첨부")
        self._papers_menu.aboutToShow.connect(self._fill_papers)
        self.attach_button = QToolButton()
        self.attach_button.setText("📎")
        self.attach_button.setToolTip("파일이나 다른 논문을 대화에 첨부합니다. 떼기 전까지 모든 질문에 함께 갑니다.")
        self.attach_button.setMenu(self.attach_menu)
        self.attach_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        self.send_button = QPushButton("보내기")
        self.send_button.clicked.connect(self._send_or_stop)
        clear = QToolButton()
        clear.setText("새 대화")
        clear.setToolTip("대화 기록을 지웁니다 (캡처와 첨부한 파일은 남습니다)")
        clear.clicked.connect(self._clear)
        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)

        # What text goes with a question: the page in view, this whole paper,
        # or every paper open in a tab.
        self.scope_box = QComboBox()
        for label, scope in SCOPES:
            self.scope_box.addItem(label, scope)
        self.scope_box.setToolTip(
            "질문과 함께 보낼 본문\n"
            "· 보는 쪽: 지금 보고 있는 쪽만\n"
            "· 이 논문: 이 논문의 모든 쪽 (요약, 다른 절과의 관계 등)\n"
            "· 모든 탭: 탭으로 열어 둔 모든 논문 (비교 질문). 근거 문장은 각 탭에 표시됩니다.\n"
            "보내는 글이 길수록 답이 느려집니다."
        )
        self.scope_box.currentIndexChanged.connect(self._scope_changed)

        # The status gets a line of its own: beside four controls it made the
        # row wider than the side panel.
        buttons = QHBoxLayout()
        buttons.addWidget(self.attach_button)
        buttons.addWidget(clear)
        buttons.addWidget(self.scope_box, 1)
        buttons.addWidget(self.send_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.view, 1)
        layout.addWidget(self._strip)
        layout.addWidget(self.input)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
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
                "<p style='color:#999;font-size:9pt'>질문과 그때 첨부한 캡처, 보고 있는 쪽의 본문, "
                "📎로 첨부한 파일의 글자가 Codex를 통해 OpenAI로 전송됩니다. 본문과 자료는 대화마다 한 번만 보내고, 이어지는 질문은 같은 Codex 세션에서 묻습니다(세션 기록은 이 컴퓨터에 저장되고 '새 대화'를 누르면 지워집니다).</p>"
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
        # Tied to the bar: dropped, not run, if the panel is gone by then.
        QTimer.singleShot(0, bar, lambda: bar.setValue(bar.maximum()))

    def _message_html(self, message: ChatMessage, width: int) -> str:
        if message.role == "user":
            where = f" <span style='color:#888'>· p.{message.page + 1}</span>" if message.page is not None else ""
            text = html.escape(message.text).replace("\n", "<br>")
            images = "".join(
                f"<a href='{QUrl.fromLocalFile(str(self.log.absolute(i))).toString()}'>"
                f"<img src='{QUrl.fromLocalFile(str(self.log.absolute(i))).toString()}' width='{min(width, 240)}'></a> "
                for i in message.images
            )
            files = "".join(
                f"<br><span style='color:#5f6368;font-size:9pt'>📄 {html.escape(name)}</span>" for name in message.files
            )
            return (
                "<table width='100%' cellpadding='7' style='background:#eef3fd;margin-top:10px'><tr><td>"
                f"<b style='color:#1a4fb4'>나</b>{where}<br>{text}{'<br>' + images if images else ''}{files}"
                "</td></tr></table>"
            )
        refs = [r for r in message.refs if self._page_of(r) is not None]
        body = markdown_html(render_citations(message.text, refs, self._cite_link), None, width)
        evidence = (
            f" &nbsp;<a href='{_ref_href(refs)}' style='font-size:9pt'>근거 문장 보기</a>" if refs else ""
        )
        return (
            f"<p style='margin-top:10px'><b style='color:#188038'>GPT</b> "
            f"&nbsp;<a href='note:{message.id}' style='font-size:9pt'>노트에 넣기</a>{evidence}</p>{body}"
        )

    def _cite_link(self, ids: list[str]) -> str:
        """Cited sentences as one Markdown link per page: " ([p.3](ref:a,b))",
        with the paper's short title before the page when it is another one."""
        pages: dict[tuple[str, int], list[str]] = {}
        for sentence_id in ids:
            page = self._page_of(sentence_id)
            if page is not None:
                pages.setdefault((self._paper_of(sentence_id), page), []).append(sentence_id)
        links = [
            f"[{label + ' ' if label else ''}p.{page + 1}]({_ref_href(group)})" for (label, page), group in pages.items()
        ]
        return f" ({', '.join(links)})" if links else ""

    def _link(self, url: QUrl) -> None:
        if url.scheme() == "note":
            self.toNoteRequested.emit(url.path())
            return
        if url.scheme() == "ref":
            self.evidenceRequested.emit(_refs_of(url))
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

    def _pick_files(self) -> None:
        patterns = " ".join(f"*{s}" for s in (*MATERIAL_SUFFIXES, *IMAGE_SUFFIXES))
        paths, _ = QFileDialog.getOpenFileNames(self, "첨부할 파일", "", f"문서·이미지 ({patterns})")
        if paths:
            self.filesChosen.emit([Path(p) for p in paths])

    def _fill_papers(self) -> None:
        self._papers_menu.clear()
        papers = self._recent_papers()
        if not papers:
            self._papers_menu.addAction("최근에 연 다른 논문이 없습니다").setEnabled(False)
        for title, root in papers:
            action = self._papers_menu.addAction(title if len(title) <= 60 else title[:58] + "…")
            action.setToolTip(root)
            action.triggered.connect(lambda _=False, r=root: self.paperChosen.emit(r))

    def refresh_files(self) -> None:
        self._refresh_strip()

    def _detach_file(self, item: ChatFile) -> None:
        self.log.detach_file(item.id)
        self._refresh_strip()

    @staticmethod
    def _clear_layout(layout: QHBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _refresh_strip(self) -> None:
        self._clear_layout(self.file_chips)
        for item in self.log.files:
            chip = QToolButton()
            name = item.name if len(item.name) <= 28 else item.name[:26] + "…"
            chip.setText(f"{'📑' if item.kind == 'paper' else '📄'} {name}  ✕")
            what = "다른 논문" if item.kind == "paper" else "파일"
            chip.setToolTip(f"{what}: {item.name}\n모든 질문에 함께 갑니다. 클릭하면 첨부에서 뺍니다.")
            chip.clicked.connect(lambda _=False, f=item: self._detach_file(f))
            self.file_chips.addWidget(chip)
        self.file_chips.addStretch(1)
        self._clear_layout(self.attachments)
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
        self._strip.setVisible(bool(self.pending or self.log.files))

    def add_quote(self, quote: str) -> None:
        """Put a quoted sentence in the question, with the other quotes at its top.

        Anything already typed stays below the quotes, so several sentences
        can be gathered first and asked about together.
        """
        lines = self.input.toPlainText().split("\n")
        split = 0
        while split < len(lines) and (lines[split].startswith(">") or not lines[split].strip()):
            split += 1
        quotes = "\n".join(line for line in lines[:split] if line.strip())
        rest = "\n".join(lines[split:]).strip()
        text = (quotes + "\n" if quotes else "") + quote.strip() + "\n\n" + rest
        self.input.setPlainText(text)
        cursor = self.input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.input.setTextCursor(cursor)
        self.input.setFocus()

    @property
    def busy(self) -> bool:
        return self._busy

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

    @property
    def scope(self) -> str:
        """``page``, ``paper`` or ``tabs``: what text goes with a question."""
        return str(self.scope_box.currentData())

    def set_scope(self, scope: str) -> None:
        index = self.scope_box.findData(scope)
        if index >= 0:
            self.scope_box.setCurrentIndex(index)

    @property
    def whole_paper(self) -> bool:
        """Questions carry every page, not only the one in view."""
        return self.scope != "page"

    def _scope_changed(self, _index: int) -> None:
        if not self._busy:
            self.status.setText(f"다음 질문부터 {SCOPE_SAYS[self.scope]} 함께 보냅니다.")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_button.setText("중지" if busy else "보내기")
        if busy:
            self.status.setText(
                f"{SCOPE_SAYS[self.scope]} 보내 묻는 중… (보통 30초–2분)"
                if self.whole_paper
                else "Codex에 묻는 중… (보통 10–60초)"
            )
        else:
            self.status.setText("")
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


def _ref_href(ids: list[str]) -> str:
    """A link naming sentences; ids of another paper carry its folder, so
    each is percent-encoded whole."""
    return "ref:" + ",".join(quote(i, safe="") for i in ids)


def _refs_of(url: QUrl) -> list[str]:
    encoded = url.toString(QUrl.ComponentFormattingOption.FullyEncoded)
    return [unquote(part) for part in encoded.removeprefix("ref:").split(",") if part]
