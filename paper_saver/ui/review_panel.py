"""The right pane's 한 장 요약 and 코넬 노트 (§4d).

The pane above the translation can show, instead of it, a one-page review of
the paper or Cornell notes on it, written by GPT on the reader's press and
kept until they ask for a new one.

Read, the document is drawn with every part that cites the paper as a link:
a point, a cue, a sentence of the summary, a section heading. Clicking one
lights the sentences it rests on in the PDF and scrolls there. "✎ 편집" turns
the same document into its Markdown, in the editor the note uses, saved as it
is typed (§4.3); its citations are the ``[p.3](ref:…)`` links, which Ctrl+click
follows, and a sentence's menu on the page puts a new one at the cursor.

This widget draws and reports; it runs nothing. Generating happens in the
reader view's worker (§7.4).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.review import KINDS, ReviewDoc, ReviewStore, plain_citations
from ..pdf.text import MATERIAL_SUFFIXES
from ..render.review import LINK_STYLE, review_html
from .live_markdown import LiveMarkdownEdit

__all__ = ["ReviewPanel"]

#: Each kind with its object particle, for sentences that need one.
_OBJECT = {"one_page": "한 장 요약을", "cornell": "코넬 노트를"}
_ABOUT = {
    "one_page": "논문 전체를 A4 한 장 분량으로 정리합니다: 한 줄 요약, 문제와 동기, 핵심 아이디어, 방법, "
    "실험과 결과, 기여, 한계와 의문, 기억할 점.",
    "cornell": "왼쪽 <b>단서</b>(복습할 때 스스로 답해 볼 질문) · 오른쪽 <b>노트</b> · 아래 <b>요약</b>으로 된 "
    "코넬 노트를 만듭니다.",
}
_READ_HINT = "항목·단서·제목을 누르면 근거가 된 원문 문장이 왼쪽 PDF에 표시됩니다."
_EDIT_HINT = (
    "Markdown으로 고칩니다 · 자동 저장 · Ctrl+클릭: p.N 링크의 원문 문장 보기 · "
    "원문 문장 우클릭 → '근거 링크 넣기'로 새 링크"
)


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


class ReviewPanel(QWidget):
    #: Make (or remake) a document: "one_page" or "cornell".
    generateRequested = Signal(str)
    stopRequested = Signal(str)
    #: Show these sentences in the reader: the ids a part cites.
    evidenceRequested = Signal(list)
    #: Put a document, its citations as page numbers, into the note.
    toNoteRequested = Signal(str)
    #: The lecture's STT, shared with the quiz tab: files picked to register,
    #: one to take off, one to move earlier (-1) or later (+1).
    transcriptsChosen = Signal(list)
    transcriptRemoved = Signal(str)
    transcriptMoved = Signal(str, int)

    def __init__(
        self,
        store: ReviewStore,
        parent: QWidget | None = None,
        *,
        transcripts: Callable[[], list[tuple[str, str]]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        #: The registered STT files as (name, file), in lecture order.
        self._transcripts = transcripts or (lambda: [])
        self.kind = "one_page"
        self._generating: set[str] = set()
        self._errors: dict[str, str] = {}
        #: The sentence ids of each link drawn, by its number; and the one clicked.
        self._segments: list[list[str]] = []
        self._active = -1
        #: The kind whose Markdown is in the editor; "" when reading.
        self._editing = ""
        #: What the view shows, so a redraw of the same document keeps its place.
        self._drawn: tuple[str, str] = ("", "")

        # Which document this is, the pane's header says.
        self.info = QLabel()
        self.info.setObjectName("muted")
        self.edit_button = QPushButton("✎ 편집")
        self.edit_button.setCheckable(True)
        self.edit_button.setToolTip("Markdown으로 고칩니다. 고친 내용은 바로 저장됩니다.")
        self.edit_button.toggled.connect(self.set_editing)
        self.more = QToolButton(text="⋯")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more)
        menu.addAction("다시 만들기…", self._regenerate)
        menu.addSeparator()
        menu.addAction("노트에 넣기", lambda: self.toNoteRequested.emit(self.kind))
        menu.addAction("Markdown 복사 (근거는 쪽 번호로)", self._copy)
        self.more.setMenu(menu)
        self.stt_button = QToolButton()
        self.stt_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.stt_button.setToolTip(
            "강의 영상의 STT(전사문). 넣으면 각 쪽을 설명한 부분을 그 쪽 아래에 함께 보내, 교수님 설명까지 반영합니다.\n"
            "나눠 찍은 영상은 여러 개를 강의 순서대로 넣으세요. 퀴즈 탭과 같은 목록입니다."
        )
        self._stt_menu = QMenu(self.stt_button)
        self._stt_menu.aboutToShow.connect(self._fill_stt_menu)
        self.stt_button.setMenu(self._stt_menu)
        head = QHBoxLayout()
        head.addWidget(self.info, 1)
        head.addWidget(self.stt_button)
        head.addWidget(self.edit_button)
        head.addWidget(self.more)

        self.error = QLabel()
        self.error.setObjectName("quizError")
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # Before there is a document, and while one is being made.
        self.lead = QLabel()
        self.lead.setObjectName("reviewLead")
        self.lead.setWordWrap(True)
        self.lead.setTextFormat(Qt.TextFormat.RichText)
        self.make_button = QPushButton()
        self.make_button.setObjectName("quizPrimary")
        self.make_button.clicked.connect(self._make)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.lead)
        empty_layout.addSpacing(10)
        empty_layout.addWidget(self.make_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(2)
        self._empty = empty

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.document().setDefaultStyleSheet(LINK_STYLE)
        self.view.anchorClicked.connect(self._clicked)
        self.view.highlighted.connect(self._hovered)

        self.editor = LiveMarkdownEdit()
        self.editor.link_handler = self._editor_link
        self._timer = QTimer(self, singleShot=True, interval=700)
        self._timer.timeout.connect(self.flush)
        self.editor.edited.connect(self._timer.start)

        self.stack = QStackedWidget()
        for page in (empty, self.view, self.editor):
            self.stack.addWidget(page)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 6)
        layout.setSpacing(6)
        layout.addLayout(head)
        layout.addWidget(self.error)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.hint)
        self.refresh()

    # -- what is shown ----------------------------------------------------------------

    @property
    def editing(self) -> bool:
        return bool(self._editing) and self.stack.currentWidget() is self.editor

    def set_kind(self, kind: str) -> None:
        if kind not in KINDS or kind == self.kind:
            return
        self._stop_editing()
        self.kind = kind
        self._active = -1
        self.refresh()

    def refresh(self) -> None:
        kind = self.kind
        doc = self.store.get(kind)
        busy = kind in self._generating
        shown = doc is not None and not busy
        self.info.setText(self._info(doc) if shown else "")
        count = len(self._transcripts())
        self.stt_button.setText(f"🎙 STT {count}개" if count else "🎙 강의 STT")
        self.stt_button.setEnabled(not busy)
        self.edit_button.setVisible(shown)
        self.more.setVisible(shown)
        error = self._errors.get(kind, "")
        self.error.setText(error)
        self.error.setVisible(bool(error))
        if busy or doc is None:
            self._show_empty(busy)
        elif self._editing == kind:
            self.stack.setCurrentWidget(self.editor)
            self.hint.setText(_EDIT_HINT)
        else:
            self._draw(doc)
            self.stack.setCurrentWidget(self.view)
            self.hint.setText(_READ_HINT)

    def _show_empty(self, busy: bool) -> None:
        kind = self.kind
        if busy:
            self.lead.setText(
                f"<p style='font-size:12pt'><b>{_OBJECT[kind]} 만드는 중입니다…</b></p>"
                "<p style='color:#5f6368'>GPT가 논문 전체를 읽고 쓰느라 보통 1–3분 걸립니다. "
                "그동안 다른 탭이나 번역을 봐도 됩니다.</p>"
            )
            self.make_button.setText("중지")
        else:
            heard = self._transcripts()
            spoken = (
                f"<p>강의 STT {len(heard)}개(" + ", ".join(name for name, _ in heard) + ")도 각 쪽을 설명한 부분을 찾아 "
                "함께 보내, 교수님 설명과 강조한 내용까지 반영합니다.</p>"
                if heard
                else "<p>강의 자료라면 위의 <b>🎙 강의 STT</b>로 강의 영상의 전사문을 넣어 교수님 설명까지 반영할 수 "
                "있습니다. 나눠 찍은 영상은 여러 개를 강의 순서대로 넣으세요.</p>"
            )
            self.lead.setText(
                f"<p style='font-size:12pt'><b>{KINDS[kind]}</b></p><p>{_ABOUT[kind]}</p>{spoken}"
                "<p>항목마다 근거가 된 원문 문장이 연결되어, 누르면 왼쪽 PDF에서 그 문장을 보여 줍니다. "
                "만든 뒤 <b>✎ 편집</b>으로 고칠 수 있습니다.</p>"
                "<p style='color:#80868b; font-size:9pt'>PDF의 글 전체(그림·표 속 글 포함, 쪽 번호와 "
                "참고문헌 목록 제외)와 등록한 강의 STT가 Codex를 통해 OpenAI로 전송됩니다. 만든 문서는 이 논문 폴더에 "
                "저장되고, '다시 만들기'를 누를 때만 새로 만듭니다.</p>"
            )
            self.make_button.setText(f"{KINDS[kind]} 만들기")
        self.hint.setText("")
        self.stack.setCurrentWidget(self._empty)

    def _info(self, doc: ReviewDoc) -> str:
        parts = []
        if doc.generated:
            parts.append(f"{_when(doc.generated)} 생성")
        if doc.model:
            parts.append(doc.model)
        now = [name for name, _ in self._transcripts()]
        if doc.transcripts:
            parts.append(f"STT {len(doc.transcripts)}개 반영")
            if now != doc.transcripts:
                parts.append("STT가 바뀜 → 다시 만들기로 반영")
        elif now:
            parts.append("STT 없이 만듦 → 다시 만들기로 반영")
        if doc.edited:
            parts.append("편집함")
        return " · ".join(parts)

    # -- the lecture's STT ---------------------------------------------------------------

    def _fill_stt_menu(self) -> None:
        menu = self._stt_menu
        menu.clear()
        heard = self._transcripts()
        if not heard:
            menu.addAction("등록한 STT가 없습니다").setEnabled(False)
        for number, (name, file) in enumerate(heard, start=1):
            item = menu.addMenu(f"{number}. {name}")
            earlier = item.addAction("강의에서 앞쪽으로", lambda f=file: self.transcriptMoved.emit(f, -1))
            earlier.setEnabled(number > 1)
            later = item.addAction("강의에서 뒤쪽으로", lambda f=file: self.transcriptMoved.emit(f, 1))
            later.setEnabled(number < len(heard))
            item.addSeparator()
            item.addAction("빼기…", lambda f=file, n=name: self._remove_transcript(f, n))
        menu.addSeparator()
        menu.addAction("STT 추가… (PDF·txt·md·srt·vtt, 여러 개 가능)", self._choose_transcripts)
        menu.addAction("퀴즈 탭과 같은 목록입니다").setEnabled(False)

    def _choose_transcripts(self) -> None:
        patterns = " ".join(f"*{s}" for s in MATERIAL_SUFFIXES)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "강의 STT 파일 선택 (여러 개 가능)", str(Path.home() / "Downloads"), f"강의 STT ({patterns})"
        )
        if paths:
            self.transcriptsChosen.emit(list(paths))

    def _remove_transcript(self, file: str, name: str) -> None:
        answer = QMessageBox.question(
            self, "STT 빼기", f"'{name}'을(를) 강의 자료에서 뺄까요? (한 장 요약·코넬 노트와 퀴즈 모두)"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.transcriptRemoved.emit(file)

    def _draw(self, doc: ReviewDoc) -> None:
        html, self._segments = review_html(doc.text, doc.kind, active=self._active)
        bar = self.view.verticalScrollBar()
        same = self._drawn == (doc.kind, doc.text)
        value = bar.value() if same else 0
        self._drawn = (doc.kind, doc.text)
        self.view.setHtml(html)
        # Tied to the bar: dropped, not run, if the panel is gone by then.
        QTimer.singleShot(0, bar, lambda: bar.setValue(value))

    # -- reading -----------------------------------------------------------------------

    def _clicked(self, url: QUrl) -> None:
        if url.scheme() != "seg":
            QDesktopServices.openUrl(url)
            return
        try:
            number = int(url.path())
            refs = self._segments[number]
        except (ValueError, IndexError):
            return
        self._active = number
        doc = self.store.get(self.kind)
        if doc is not None:
            self._draw(doc)
        self.evidenceRequested.emit(list(refs))

    def _hovered(self, url: QUrl) -> None:
        if self.stack.currentWidget() is not self.view:
            return
        if url.scheme() == "seg":
            self.hint.setText("클릭: 이 부분의 근거가 된 원문 문장을 왼쪽 PDF에서 보기")
        else:
            self.hint.setText(_READ_HINT)

    def clear_active(self) -> None:
        """The lights on the page went out; so does the part that lit them."""
        if self._active == -1:
            return
        self._active = -1
        doc = self.store.get(self.kind)
        if doc is not None and self.stack.currentWidget() is self.view:
            self._draw(doc)

    # -- editing -----------------------------------------------------------------------

    def set_editing(self, on: bool) -> None:
        if on and not self._editing:
            doc = self.store.get(self.kind)
            if doc is None or self.kind in self._generating:
                self._sync_button(False)
                return
            self._editing = self.kind
            self.editor.setPlainText(doc.text)
            self._sync_button(True)
            self.refresh()
            self.editor.setFocus()
        elif not on and self._editing:
            self._stop_editing()
            self.refresh()

    def _stop_editing(self) -> None:
        self.flush()
        self._editing = ""
        self._active = -1
        self._sync_button(False)

    def _sync_button(self, on: bool) -> None:
        self.edit_button.blockSignals(True)
        self.edit_button.setChecked(on)
        self.edit_button.setText("완료" if on else "✎ 편집")
        self.edit_button.blockSignals(False)

    def flush(self) -> None:
        """Save what is in the editor now (§4.3)."""
        self._timer.stop()
        if self._editing and self.store.set_text(self._editing, self.editor.toPlainText()):
            doc = self.store.get(self._editing)
            if doc is not None and self._editing == self.kind:
                self.info.setText(self._info(doc))

    def _editor_link(self, target: str) -> bool:
        if not target.startswith("ref:"):
            return False
        ids = [i for i in target[4:].split(",") if i]
        if ids:
            self.evidenceRequested.emit(ids)
        return True

    def insert_citation(self, sentence_id: str, page: int) -> bool:
        """A citation of this sentence at the editor's cursor, while editing."""
        if not self.editing:
            return False
        cursor = self.editor.textCursor()
        before = cursor.block().text()[: cursor.positionInBlock()]
        space = "" if not before or before.endswith(" ") else " "
        cursor.insertText(f"{space}[p.{page + 1}](ref:{sentence_id})")
        self.editor.setFocus()
        return True

    # -- making ------------------------------------------------------------------------

    def _make(self) -> None:
        if self.kind in self._generating:
            self.stopRequested.emit(self.kind)
            return
        self._errors.pop(self.kind, None)
        self.generateRequested.emit(self.kind)

    def _regenerate(self) -> None:
        doc = self.store.get(self.kind)
        if doc is not None:
            edited = " 편집한 내용도 사라집니다." if doc.edited else ""
            answer = QMessageBox.question(
                self, "다시 만들기", f"{_OBJECT[self.kind]} 새로 만들면 지금 내용은 바뀝니다.{edited} 계속할까요?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._make()

    def set_generating(self, kind: str, on: bool) -> None:
        if on:
            if self._editing == kind:
                self._stop_editing()
            self._generating.add(kind)
            self._errors.pop(kind, None)
        else:
            self._generating.discard(kind)
        if on or kind == self.kind:
            self._active = -1
        self.refresh()

    def set_error(self, kind: str, message: str) -> None:
        self._errors[kind] = message
        self.refresh()

    def _copy(self) -> None:
        self.flush()
        doc = self.store.get(self.kind)
        if doc is not None:
            QGuiApplication.clipboard().setText(plain_citations(doc.text))
