"""백지 복습, in a pane (§4e).

Keywords only, then everything remembered written out, then marked against
the PDF. The panel goes through four states:

* no sheet yet: what the method is, and the button that has GPT make the
  keyword sheet and its answer key from the PDF;
* the sheet: its topics to tick (each with its last score), the attempts so
  far, and "백지 시작";
* writing: the blank sheet - each topic's heading and keywords, room under
  each - in the note's editor, saved as typed (§4.3). The reader view covers
  the other pane meanwhile (:attr:`writingChanged`), since the point is to
  write without looking;
* marked: what was written wrong, missed (★ where the lecture stressed it),
  partly there and recalled, topic by topic, each linked to its sentences
  in the PDF; "다시 쓰기" starts again on the topics not yet full.

There is one panel, shown in whichever pane chose it. It draws and reports;
generating and marking run in the reader view's worker (§7.4).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.blank import Attempt, BlankStore, result_markdown, wrote_anything
from ..core.review import MODES, plain_citations
from ..render.review import LINK_STYLE, review_html
from .live_markdown import LiveMarkdownEdit

__all__ = ["BlankPanel"]

_ABOUT = (
    "<p style='font-size:12pt'><b>백지 복습</b></p>"
    "<p>키워드만 보고 이 PDF의 내용을 기억나는 대로 <b>통째로</b> 써 본 뒤, GPT가 PDF와 맞춰 채점합니다: "
    "<b>틀리게 쓴 것 · 빠뜨린 것 · 일부만 쓴 것 · 기억한 것</b>. 빠뜨린 것을 누르면 PDF의 그 문장을 보여 줍니다.</p>"
    "<p>기준은 <b>이 PDF 자체</b>입니다 (한 장 요약·코넬 노트가 아닙니다). 본문뿐 아니라 <b>표와 그림</b>도 "
    "채점 기준에 들어가고, 키워드에 '표: …', '그림: …'으로 나와 기억으로 다시 그려 봅니다. "
    "채점 기준은 처음 한 번 만들어 고정하므로, "
    "다시 쓸 때마다 같은 기준으로 점수가 비교됩니다. 강의 STT를 등록했다면 교수님이 오래 설명하고 반복한 내용에 "
    "★가 붙습니다.</p>"
    "<p style='color:#80868b; font-size:9pt'>키워드를 만들 때와 채점할 때 PDF의 글 전체와 등록한 강의 STT가, 키워드를 "
    "만들 때는 아래를 켜 두면 PDF에서 잘라 낸 그림·표 이미지가, 채점할 때는 내가 쓴 글이 Codex를 통해 OpenAI로 "
    "전송됩니다. 쓴 글과 채점 기록은 이 논문 폴더의 blank.json에 저장됩니다.</p>"
)
_WRITE_HINT = (
    "키워드만 보고 기억나는 것을 모두 쓰세요 · 표는 | 칸 | 칸 | 으로, 그림은 화살표(→)로 그려도 됩니다 · "
    "자동 저장 · 옆 창은 채점이 끝날 때까지 가려 둡니다 · 다 쓰면 '채점하기'"
)
_RESULT_HINT = "항목을 누르면 근거가 된 원문 문장이 옆 창의 PDF에 표시됩니다 · ★ 강의에서 강조한 내용"


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).strftime("%m/%d %H:%M")
    except ValueError:
        return stamp


def _fraction(got: float, total: int) -> str:
    return f"{got:g}/{total}"


class BlankPanel(QWidget):
    #: Make (or remake) the keyword sheet.
    sheetRequested = Signal()
    #: Stop whatever is running: the sheet or a marking.
    stopRequested = Signal()
    #: Mark this attempt (its id).
    gradeRequested = Signal(str)
    #: Show these sentences in the reader.
    evidenceRequested = Signal(list)
    #: Put a marked attempt into the note (its id).
    toNoteRequested = Signal(str)
    #: Whether an attempt is being written (or marked): the other pane is covered.
    writingChanged = Signal(bool)

    def __init__(
        self,
        store: BlankStore,
        parent: QWidget | None = None,
        *,
        page_of: Callable[[str], int | None] = lambda _i: None,
        mode: Callable[[], str] = lambda: "paper",
        transcripts: Callable[[], list[str]] = lambda: [],
    ) -> None:
        super().__init__(parent)
        self.store = store
        self._page_of = page_of
        #: 강의 모드 or 논문 모드, as the review panels have it.
        self._mode = mode
        self._transcripts = transcripts
        #: "" | "sheet" | "grade": what is running.
        self.busy = ""
        self._error = ""
        #: The attempt open - being written, or its marking shown; "" for the list.
        self.attempt_id = ""
        self._writing = False
        #: Link number -> sentence ids, and the one last clicked.
        self._segments: list[list[str]] = []
        self._active = -1

        self.title = QLabel()
        self.title.setObjectName("blankTitle")
        self.title.setStyleSheet("font-weight:600")
        self.primary = QPushButton()
        self.primary.setObjectName("quizPrimary")
        self.primary.clicked.connect(self._primary)
        self.again_button = QPushButton("빠뜨린 것 다시 쓰기")
        self.again_button.setToolTip("만점이 아닌 주제만 골라 백지에 다시 씁니다.")
        self.again_button.clicked.connect(self._again)
        self.written_button = QPushButton("내가 쓴 글")
        self.written_button.setCheckable(True)
        self.written_button.setToolTip("채점한 글을 봅니다 (고칠 수는 없습니다).")
        self.written_button.toggled.connect(lambda _on: self.refresh())
        self.back_button = QPushButton("목록으로")
        self.back_button.clicked.connect(self.show_list)
        self.more = QToolButton(text="⋯")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._menu = QMenu(self.more)
        self._menu.aboutToShow.connect(self._fill_menu)
        self.more.setMenu(self._menu)
        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(self.title, 1)
        for widget in (self.again_button, self.written_button, self.back_button, self.primary, self.more):
            head.addWidget(widget)

        self.info = QLabel()
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        self.error = QLabel()
        self.error.setObjectName("quizError")
        self.error.setWordWrap(True)
        self.error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        # No sheet yet, or one being made.
        self.lead = QLabel()
        self.lead.setObjectName("reviewLead")
        self.lead.setWordWrap(True)
        self.lead.setTextFormat(Qt.TextFormat.RichText)
        self.make_button = QPushButton()
        self.make_button.setObjectName("quizPrimary")
        self.make_button.clicked.connect(self._make)
        self.figures_box = QCheckBox("그림·표도 이미지로 보내기 (그림·표가 있는 쪽마다 한 장, 최대 40장)")
        self.figures_box.setToolTip(
            "표가 이미지로 들어간 슬라이드처럼 글자가 없는 그림·표도 채점 기준에 넣으려면 켜 두세요.\n"
            "키워드를 만들 때만 보내고, 채점할 때는 보내지 않습니다."
        )
        self.figures_box.setChecked(store.send_figures)
        self.figures_box.toggled.connect(store.set_send_figures)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.lead)
        empty_layout.addWidget(self.figures_box)
        empty_layout.addSpacing(10)
        empty_layout.addWidget(self.make_button, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addStretch(2)
        self._empty = empty

        # The sheet: topics to tick, attempts so far.
        self.topics = QListWidget()
        self.topics.itemChanged.connect(lambda _item: self._sync_start())
        self.all_button = QPushButton("전체 선택")
        self.all_button.clicked.connect(self._toggle_all)
        self.history = QListWidget()
        self.history.itemActivated.connect(self._open_item)
        self.history.itemClicked.connect(self._open_item)
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self._history_menu)
        home = QWidget()
        home_layout = QVBoxLayout(home)
        home_layout.setContentsMargins(0, 0, 0, 0)
        topics_head = QHBoxLayout()
        topics_head.addWidget(QLabel("<b>쓸 주제</b> <span style='color:#80868b'>(지난 점수)</span>"), 1)
        topics_head.addWidget(self.all_button)
        home_layout.addLayout(topics_head)
        home_layout.addWidget(self.topics, 3)
        home_layout.addWidget(QLabel("<b>기록</b> <span style='color:#80868b'>(눌러서 보기 · 우클릭으로 지우기)</span>"))
        home_layout.addWidget(self.history, 2)
        self._home = home

        self.editor = LiveMarkdownEdit()
        self._timer = QTimer(self, singleShot=True, interval=700)
        self._timer.timeout.connect(self.flush)
        self.editor.edited.connect(self._timer.start)

        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.document().setDefaultStyleSheet(LINK_STYLE)
        self.view.anchorClicked.connect(self._clicked)

        self.stack = QStackedWidget()
        for page in (empty, home, self.editor, self.view):
            self.stack.addWidget(page)
        self.hint = QLabel()
        self.hint.setObjectName("muted")
        self.hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 6)
        layout.setSpacing(6)
        layout.addLayout(head)
        layout.addWidget(self.info)
        layout.addWidget(self.error)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.hint)
        self.refresh()

    # -- state -------------------------------------------------------------------------

    @property
    def writing(self) -> bool:
        """An attempt is open and not yet marked."""
        attempt = self.store.get(self.attempt_id) if self.attempt_id else None
        return attempt is not None and not attempt.checked

    def attempt(self) -> Attempt | None:
        return self.store.get(self.attempt_id) if self.attempt_id else None

    def refresh(self) -> None:
        # What is typed is saved before anything is redrawn from the store.
        self.flush()
        sheet = self.store.sheet
        attempt = self.attempt()
        if attempt is None:
            self.attempt_id = ""
        for widget in (self.again_button, self.written_button, self.back_button, self.primary, self.more):
            widget.hide()
        self.error.setText(self._error)
        self.error.setVisible(bool(self._error))
        self.info.hide()
        if sheet is None or self.busy == "sheet":
            self._show_empty()
        elif attempt is None:
            self._show_home()
        elif attempt.checked and self.busy != "grade":
            self._show_result(attempt)
        else:
            self._show_writing(attempt)
        if self.writing != self._writing:
            self._writing = self.writing
            self.writingChanged.emit(self._writing)

    def _show_empty(self) -> None:
        self.title.setText("백지 복습")
        if self.busy == "sheet":
            self.lead.setText(
                "<p style='font-size:12pt'><b>키워드와 채점 기준을 만드는 중입니다…</b></p>"
                "<p style='color:#5f6368'>GPT가 PDF 전체를 읽고 주제·키워드·채점 기준을 만드느라 보통 1–3분 걸립니다.</p>"
            )
            self.make_button.setText("중지")
            self.figures_box.hide()
        else:
            self.figures_box.setChecked(self.store.send_figures)
            self.figures_box.show()
            mode = MODES.get(self._mode(), "")
            heard = self._transcripts()
            extra = f"<p>{mode}로 만듭니다" + (f" · 강의 STT {len(heard)}개 반영" if heard else "") + ".</p>"
            self.lead.setText(_ABOUT + extra)
            self.make_button.setText("키워드 만들기")
        self.hint.setText("")
        self.stack.setCurrentWidget(self._empty)

    def _show_home(self) -> None:
        sheet = self.store.sheet
        assert sheet is not None
        self.title.setText("백지 복습")
        self.info.setText(
            f"주제 {len(sheet.topics)}개 · 채점 기준 {len(sheet.points())}항목 · {_when(sheet.generated)} 생성"
            + (f" · {MODES[sheet.mode]}" if sheet.mode in MODES else "")
            + (f" · STT {len(sheet.transcripts)}개 반영" if sheet.transcripts else "")
            + (f" · 그림·표 {sheet.figures}개 반영" if sheet.figures else "")
        )
        self.info.show()
        self.more.show()
        self.primary.show()
        checked = self._checked() if self.topics.count() == len(sheet.topics) else None
        self.topics.blockSignals(True)
        self.topics.clear()
        for topic in sheet.topics:
            last = self.store.last_score(topic.number)
            score = f"   {_fraction(*last)}" if last else ""
            item = QListWidgetItem(f"{topic.number}. {topic.heading}{score}")
            item.setToolTip("키워드: " + " · ".join(topic.keywords))
            item.setData(Qt.ItemDataRole.UserRole, topic.number)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = checked is None or topic.number in checked
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            self.topics.addItem(item)
        self.topics.blockSignals(False)
        self.history.clear()
        left = self.store.open_attempt()
        if left is not None and wrote_anything(left.text):
            item = QListWidgetItem(f"✎ 쓰는 중 — {_when(left.started)} · 주제 {len(left.topics)}개 (눌러서 이어 쓰기)")
            item.setData(Qt.ItemDataRole.UserRole, left.id)
            self.history.addItem(item)
        for attempt in self.store.marked():
            got, total = attempt.score()
            percent = round(100 * got / total) if total else 0
            item = QListWidgetItem(
                f"{_when(attempt.checked)} · 주제 {len(attempt.topics)}개 · {_fraction(got, total)} ({percent}%)"
            )
            item.setData(Qt.ItemDataRole.UserRole, attempt.id)
            self.history.addItem(item)
        if not self.history.count():
            self.history.addItem(QListWidgetItem("아직 없습니다"))
            self.history.item(0).setFlags(Qt.ItemFlag.NoItemFlags)
        self._sync_start()
        self.hint.setText("주제를 고르고 '백지 시작'. 키워드만 보이고, 채점 기준은 채점한 뒤에 보입니다.")
        self.stack.setCurrentWidget(self._home)

    def _show_writing(self, attempt: Attempt) -> None:
        grading = self.busy == "grade"
        self.title.setText("채점 중…" if grading else f"백지 쓰는 중 · 주제 {len(attempt.topics)}개")
        self.primary.setText("중지" if grading else "채점하기")
        self.primary.setEnabled(True)
        self.primary.show()
        self.back_button.setText("목록으로")
        self.back_button.setToolTip("쓴 글은 저장되어 있어, 목록에서 눌러 이어 쓸 수 있습니다.")
        self.back_button.setVisible(not grading)
        if self.editor.toPlainText() != attempt.text:
            self.editor.setPlainText(attempt.text)
        self.editor.setReadOnly(grading)
        self.hint.setText(
            "GPT가 PDF와 맞춰 채점하는 중입니다 (보통 1–3분)." if grading else _WRITE_HINT
        )
        self.stack.setCurrentWidget(self.editor)

    def _show_result(self, attempt: Attempt) -> None:
        sheet = self.store.sheet
        assert sheet is not None
        got, total = attempt.score()
        self.title.setText(f"채점 결과 · {_fraction(got, total)}")
        for widget in (self.again_button, self.written_button, self.back_button, self.more):
            widget.show()
        self.back_button.setText("목록으로")
        self.back_button.setToolTip("")
        self.again_button.setEnabled(bool(self._unfinished(attempt)))
        if self.written_button.isChecked():
            self.editor.setPlainText(attempt.text)
            self.editor.setReadOnly(True)
            self.hint.setText("채점한 글입니다. 다시 쓰려면 '빠뜨린 것 다시 쓰기'나 목록에서 '백지 시작'.")
            self.stack.setCurrentWidget(self.editor)
            return
        self._draw(attempt)
        self.hint.setText(_RESULT_HINT)
        self.stack.setCurrentWidget(self.view)

    def _draw(self, attempt: Attempt) -> None:
        sheet = self.store.sheet
        if sheet is None:
            return
        marked = self.store.marked()
        before = marked[marked.index(attempt) + 1 :] if attempt in marked else marked
        markdown = result_markdown(sheet, attempt, self._page_of, before=before)
        html, self._segments = review_html(markdown, "one_page", active=self._active)
        bar = self.view.verticalScrollBar()
        value = bar.value()
        self.view.setHtml(html)
        QTimer.singleShot(0, bar, lambda: bar.setValue(value))

    # -- the sheet -----------------------------------------------------------------------

    def _checked(self) -> list[int]:
        return [
            int(self.topics.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self.topics.count())
            if self.topics.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _sync_start(self) -> None:
        if self.stack.currentWidget() is not self._home and self.attempt_id:
            return
        count = len(self._checked())
        self.primary.setText(f"백지 시작 ({count}개 주제)" if count else "백지 시작")
        self.primary.setEnabled(bool(count))
        every = count == self.topics.count()
        self.all_button.setText("전체 해제" if every else "전체 선택")

    def _toggle_all(self) -> None:
        state = Qt.CheckState.Unchecked if len(self._checked()) == self.topics.count() else Qt.CheckState.Checked
        self.topics.blockSignals(True)
        for row in range(self.topics.count()):
            self.topics.item(row).setCheckState(state)
        self.topics.blockSignals(False)
        self._sync_start()

    def _fill_menu(self) -> None:
        menu = self._menu
        menu.clear()
        attempt = self.attempt()
        if attempt is not None and attempt.checked:
            menu.addAction("노트에 넣기", lambda: self.toNoteRequested.emit(attempt.id))
            menu.addAction("채점 결과 복사 (근거는 쪽 번호로)", lambda: self._copy(attempt))
            menu.addSeparator()
        menu.addAction("키워드 다시 만들기…", self._remake)
        figures = menu.addAction("다시 만들 때 그림·표도 이미지로 보내기")
        figures.setCheckable(True)
        figures.setChecked(self.store.send_figures)
        figures.toggled.connect(self.figures_box.setChecked)

    def _remake(self) -> None:
        if self.store.attempts:
            answer = QMessageBox.question(
                self, "키워드 다시 만들기",
                "새 키워드와 채점 기준을 만들면 지난 채점 기록과 쓰던 글이 지워집니다 (점수는 기준이 같아야 비교됩니다). 계속할까요?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._make()

    def _make(self) -> None:
        if self.busy:
            self.stopRequested.emit()
            return
        self._error = ""
        self.sheetRequested.emit()

    def start(self, topics: list[int]) -> None:
        """A blank sheet on these topics, opened for writing."""
        if not topics or self.store.sheet is None:
            return
        attempt = self.store.start(topics)
        self.attempt_id = attempt.id
        self._error = ""
        self.written_button.setChecked(False)
        self.editor.setPlainText(attempt.text)
        self.refresh()
        cursor = self.editor.textCursor()
        # Under the first topic's keywords.
        first = attempt.text.find("\n\n")
        cursor.setPosition(first + 1 if first >= 0 else 0)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def open_attempt(self, attempt_id: str) -> None:
        if self.store.get(attempt_id) is None:
            return
        self.flush()
        self.attempt_id = attempt_id
        self._active = -1
        self._error = ""
        self.written_button.setChecked(False)
        attempt = self.store.get(attempt_id)
        if attempt is not None:
            self.editor.setPlainText(attempt.text)
        self.refresh()

    def show_list(self) -> None:
        self.flush()
        self.attempt_id = ""
        self._active = -1
        self._error = ""
        self.refresh()

    def _open_item(self, item: QListWidgetItem) -> None:
        attempt_id = item.data(Qt.ItemDataRole.UserRole)
        if attempt_id:
            self.open_attempt(str(attempt_id))

    def _history_menu(self, pos: QPoint) -> None:
        item = self.history.itemAt(pos)
        attempt_id = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if not attempt_id:
            return
        menu = QMenu(self)
        menu.addAction("이 기록 지우기", lambda: self._remove(str(attempt_id)))
        menu.exec(self.history.viewport().mapToGlobal(pos))

    def _remove(self, attempt_id: str) -> None:
        answer = QMessageBox.question(self, "기록 지우기", "이 기록(쓴 글과 채점)을 지울까요?")
        if answer == QMessageBox.StandardButton.Yes:
            self.store.remove(attempt_id)
            self.refresh()

    # -- writing and marking -------------------------------------------------------------

    def _primary(self) -> None:
        attempt = self.attempt()
        if attempt is None:
            self.start(self._checked())
            return
        if self.busy:
            self.stopRequested.emit()
            return
        self.flush()
        if not wrote_anything(attempt.text):
            self.set_error("아직 아무것도 쓰지 않았습니다. 키워드 아래에 기억나는 것을 쓴 뒤 채점하세요.")
            return
        self._error = ""
        self.gradeRequested.emit(attempt.id)

    def flush(self) -> None:
        """Save what is in the editor now (§4.3)."""
        self._timer.stop()
        if self.writing and self.stack.currentWidget() is self.editor and not self.editor.isReadOnly():
            self.store.set_text(self.attempt_id, self.editor.toPlainText())

    def _unfinished(self, attempt: Attempt) -> list[int]:
        sheet = self.store.sheet
        if sheet is None:
            return []
        wrong = {w.topic for w in attempt.wrong}
        out = []
        for number in attempt.topics:
            got, total = attempt.score([number], sheet)
            if got < total or number in wrong:
                out.append(number)
        return out

    def _again(self) -> None:
        attempt = self.attempt()
        if attempt is not None:
            self.start(self._unfinished(attempt))

    def set_busy(self, what: str) -> None:
        if what:
            self.flush()
            self._error = ""
        self.busy = what
        self._active = -1
        self.refresh()

    def set_error(self, message: str) -> None:
        self._error = message
        self.refresh()

    # -- reading the marking -------------------------------------------------------------

    def _clicked(self, url: QUrl) -> None:
        if url.scheme() != "seg":
            return
        try:
            number = int(url.path())
            refs = self._segments[number]
        except (ValueError, IndexError):
            return
        self._active = number
        attempt = self.attempt()
        if attempt is not None:
            self._draw(attempt)
        self.evidenceRequested.emit(list(refs))

    def clear_active(self) -> None:
        if self._active == -1:
            return
        self._active = -1
        attempt = self.attempt()
        if attempt is not None and attempt.checked and self.stack.currentWidget() is self.view:
            self._draw(attempt)

    def result_text(self, attempt_id: str) -> str:
        """A marked attempt as Markdown, its citations as page numbers."""
        attempt = self.store.get(attempt_id)
        sheet = self.store.sheet
        if attempt is None or sheet is None or not attempt.checked:
            return ""
        return plain_citations(result_markdown(sheet, attempt, self._page_of)).strip()

    def _copy(self, attempt: Attempt) -> None:
        QGuiApplication.clipboard().setText(self.result_text(attempt.id))
