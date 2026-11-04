"""The reading screen: original | translation | dictionary · GPT · note · memos · words.

The two panes scroll as one and highlight as one (§1.3, §2.3). Pointing near a
sentence on either side lights the sentence and its counterpart, and the strip
along the bottom shows the pair in full - useful where the Korean had to be set
small to fit its rectangle.

Capture (Ctrl+Shift+X, ⌘⇧X on a Mac) turns the next drag on either pane into a rectangle;
the region is rendered sharp and can be asked about in the GPT tab, pasted
into the note, or copied. When GPT's answer cites sentences of the page, the
reader scrolls to them and they stay lit until the next click on a page.

Each pane can show any of five things: the original, the translation, a
one-page review of the paper or Cornell notes on it (§4d), or 백지 복습 (§4e)
- the original and the translation side by side as they start, or the PDF
beside the notes, or the notes beside the summary. While a blank sheet is
being written the other pane is covered. A part of a review, clicked, lights the
sentences it rests on in whichever pane shows a page. Scrolling and zoom
follow the pane that shows one, the left when both do.
"""

from __future__ import annotations

import copy
import logging
import re
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..core.atomic_io import read_json
from ..core.blank import BlankStore, figure_regions
from ..core.chat import ChatLog, render_citations, resolve_citations
from ..core.chat_context import ContextBuilder, PaperSource, SessionState
from ..core.geometry import Rect
from ..core.lecture import (
    EvidenceIndex,
    SlideDeck,
    align_transcript,
    combine_transcripts,
    extract_quiz_examples,
    format_quiz_examples,
    parse_transcript,
    transcript_excerpt,
)
from ..core.lecture_saver import (
    LectureProject,
    LectureSaverError,
    find_projects,
    same_lecture,
    transcript_markdown,
)
from ..core.notebook import IMAGE_SUFFIXES
from ..core.obsidian import ObsidianError, is_vocab_vault, register_word
from ..core.project import HIGHLIGHT_COLORS, Project, WordEntry
from ..core.quiz import QuizQuestion, QuizStore, SlideQuiz, StyleLibrary, id_prefix
from ..core.review import KINDS as REVIEW_KINDS
from ..core.review import ReviewStore, plain_citations, refs_in
from ..pdf.document import PaperDocument
from ..pdf.text import MATERIAL_SUFFIXES, MaterialError, read_pages, read_paper, stray_lines
from ..render.quiz_pdf import PLACEMENTS, quiz_pdf, slides_quiz_pdf
from ..translate.blank import CodexBlankGenerator
from ..translate.codex_cli import forget_codex_session
from ..translate.dictionary import Dictionary, clean_word
from ..translate.engine import (
    EnginePromptRejected,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)
from ..translate.prompts import (
    CHAT_FILE_CHARS,
    CHAT_PAPER_CHARS,
    chat_followup_prompt,
    chat_prompt,
    sentence_tag,
)
from ..translate.quiz import CodexQuizGenerator, QuizGenerationInput
from ..translate.review import CodexReviewGenerator, ReviewRequest, default_lecture, review_request
from .blank_panel import BlankPanel
from .bookmarks_panel import BookmarksPanel
from .chat_panel import ChatPanel
from .dictionary_panel import DictionaryPanel
from .keys import CAPTURE_LABEL
from .markdown_view import save_png
from .note_panel import NotePanel
from .notes_panel import NotesPanel
from .page_canvas import MARGIN, MarginNote, PageCanvas, RangeMark
from .quiz_panel import QuizPanel
from .review_panel import ReviewPanel
from .session import PaperSession
from .wordbook_panel import WordbookPanel
from .workers import BackgroundTask

__all__ = ["ReaderView"]

log = logging.getLogger(__name__)


NOTE_KEY = "Ctrl+Shift+M" if sys.platform == "darwin" else "Ctrl+M"
HIGHLIGHT_KEY = "Ctrl+Shift+H" if sys.platform == "darwin" else "Ctrl+H"
GPT_KEY = "Ctrl+Shift+G" if sys.platform == "darwin" else "Ctrl+G"
#: What "GPT에게 이 문장 설명 요청" asks.
EXPLAIN_QUESTION = "이 문장을 쉽게 풀어서 설명해 주세요. 핵심 용어와, 앞뒤 맥락에서 이 문장이 하는 역할도 알려 주세요."
#: Captures sent again with a follow-up question, at most.
FOLLOW_UP_IMAGES = 3
#: Quizzes made at once by 모두 만들기: as many Codex runs as a translation
#: keeps going (runner.DEFAULT_PARALLEL).
QUIZ_PARALLEL = 3
#: How the keys read in messages: Cmd on a Mac.
_MOD = "⌘⇧" if sys.platform == "darwin" else "Ctrl+"


class _Scroll(QScrollArea):
    """A scroll area that turns Ctrl+wheel into zoom instead of scrolling."""

    zoomStep = Signal(int)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoomStep.emit(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)


#: The bar above each pane: one height for both, so their pages line up (§1.3).
PANE_HEADER_HEIGHT = 28
#: What either pane can show, and the label of each.
PANE_MODES = {"original": "원문", "translation": "번역 (직역)", **REVIEW_KINDS, "blank": "백지 복습"}
#: The pages, as opposed to a review in their place.
PAGE_MODES = ("original", "translation")
_MODE_TIPS = {
    "one_page": "GPT가 쓴 논문 한 장 요약. 항목을 누르면 근거 문장을 원문에서 보여 줍니다.",
    "cornell": "GPT가 쓴 코넬 노트(단서 · 노트 · 요약). 항목을 누르면 근거 문장을 원문에서 보여 줍니다.",
    "blank": "백지 복습: 키워드만 보고 PDF 내용을 통째로 써 본 뒤, PDF와 맞춰 빠뜨린 것·틀린 것을 채점합니다.\n"
    "쓰는 동안 옆 창은 가려 둡니다.",
}
SIDES = ("left", "right")
_SIDE_NAMES = {"left": "왼쪽", "right": "오른쪽"}


def _scroll(canvas: PageCanvas) -> _Scroll:
    scroll = _Scroll()
    scroll.setWidget(canvas)
    scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    return scroll


def _header(*widgets: QWidget) -> QFrame:
    bar = QFrame()
    bar.setObjectName("paneHeader")
    bar.setFixedHeight(PANE_HEADER_HEIGHT)
    layout = QHBoxLayout(bar)
    layout.setContentsMargins(6, 0, 6, 0)
    layout.setSpacing(2)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return bar


def _pane(header: QWidget, body: QWidget) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(header)
    layout.addWidget(body, 1)
    return box


class _SideTabs(QTabWidget):
    """The side tabs, each page in a scroll area of its own.

    A tab widget is as tall as its tallest page needs (the quiz, about 400
    px), and that minimum is what stopped the note below from being dragged
    taller. Wrapped, a page scrolls when the space is short instead. Pages
    are still added, shown and compared as the panels themselves.
    """

    def addTab(self, widget: QWidget, label: str) -> int:  # type: ignore[override]  # noqa: N802
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        # Short of height a page scrolls; short of width it narrows instead.
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return super().addTab(area, label)

    def setCurrentWidget(self, widget: QWidget) -> None:  # noqa: N802
        for index in range(self.count()):
            area = super().widget(index)
            if area is widget or (isinstance(area, QScrollArea) and area.widget() is widget):
                self.setCurrentIndex(index)
                return

    def currentWidget(self) -> QWidget | None:  # noqa: N802
        area = super().currentWidget()
        return area.widget() if isinstance(area, QScrollArea) else area


#: Between a paper's key and a sentence id, in a citation of another paper.
REF_SEPARATOR = "|"


class ReaderView(QWidget):
    statusMessage = Signal(str)
    pageChanged = Signal(int)
    captureModeChanged = Signal(bool)
    #: How many sentences an answer's citations lit in this paper (0: none).
    evidenceLit = Signal(int)

    def __init__(
        self,
        session: PaperSession,
        settings: Settings,
        dictionary: Dictionary,
        engine_provider: Callable[[], TranslationEngine | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.session = session
        self.settings = settings
        self.dictionary = dictionary
        self._engine_provider = engine_provider
        self._hover = ""
        self._lookup_task: BackgroundTask | None = None
        self._lookup_serial = 0
        self._syncing = False
        self._page = 0
        self._chat_task: BackgroundTask | None = None
        self._closed = False
        #: The other papers open in tabs, and how to bring one to the front;
        #: set by the window. A question can be asked of all of them, and an
        #: answer's citations light each paper in its own tab.
        self.peers: Callable[[], list[ReaderView]] = lambda: []
        self.show_peer: Callable[[ReaderView], None] = lambda _reader: None
        #: What each capture of this session shows, for the prompt.
        self._capture_notes: dict[str, str] = {}

        project = session.project
        self.left = PageCanvas(session, translated=False)
        self.right = PageCanvas(session, translated=True)
        self.left_scroll = _scroll(self.left)
        self.right_scroll = _scroll(self.right)

        order = {s.id: i for i, (_, _, s) in enumerate(session.layout.sentences())}
        self.dictionary_panel = DictionaryPanel()
        self.notes_panel = NotesPanel(project.notes, order)
        self.bookmarks_panel = BookmarksPanel(
            project.highlights, project.notes, order, session.translation, self._sentence_text
        )
        self.wordbook_panel = WordbookPanel(project.wordbook, project.exports_dir)
        self.chat_log = ChatLog(project.root / "chat.json")
        self.chat_log.on_session_end = self._forget_session
        self.chat_panel = ChatPanel(
            self.chat_log, page_of=self._page_of, recent_papers=self._recent_papers, paper_of=self._paper_label
        )
        self._attach_task: BackgroundTask | None = None
        self.note_panel = NotePanel(project, settings)
        self.quiz_store = QuizStore(project.root)
        self.style_library = StyleLibrary(settings.quiz_styles_dir, MATERIAL_SUFFIXES)
        self._evidence_index: EvidenceIndex | None = None
        #: The quiz tab stays on its question while the reader looks at
        #: another page for its evidence, until a page is clicked.
        self._quiz_away = False
        self.quiz_panel = QuizPanel(
            self.quiz_store, session.page_count,
            Path(str(project.meta.get("source") or project.pdf_path)).name,
            project.exports_dir, project.title, library=self.style_library, evidence=self._quiz_evidence_of,
        )
        self._lecture_task: BackgroundTask | None = None
        self._deck: SlideDeck | None = None
        self._deck_task: BackgroundTask | None = None
        self._quiz_task: BackgroundTask | None = None
        self._material_task: BackgroundTask | None = None
        self._quiz_pdf_task: BackgroundTask | None = None
        self.review_store = ReviewStore(project.root)
        self.review_store.bind(session.layout)
        # One per pane, over the same documents.
        self.review_panels = {
            side: ReviewPanel(
                self.review_store, transcripts=lambda: [(m.name, m.key) for m in self.quiz_store.transcripts],
                lectures=self.quiz_panel.lecture_choices, default_mode=self._default_review_mode,
            )
            for side in SIDES
        }
        self._review_tasks: dict[str, BackgroundTask] = {}
        # 백지 복습: one panel, in whichever pane chose it (§4e).
        self.blank_store = BlankStore(project.root)
        self.blank_store.bind(session.layout)
        self.blank_panel = BlankPanel(
            self.blank_store, page_of=self._page_of, mode=lambda: self.review_panels["left"].mode,
            transcripts=lambda: [m.name for m in self.quiz_store.transcripts],
        )
        self._blank_task: BackgroundTask | None = None
        #: The reader chose to see the pane covered while writing.
        self._peeking = False
        self.side = _SideTabs()
        self.side.addTab(self.dictionary_panel, "사전")
        self.side.addTab(self.chat_panel, "GPT")
        self.side.addTab(self.quiz_panel, "퀴즈")
        self.side.addTab(self.notes_panel, "메모")
        self.side.addTab(self.bookmarks_panel, "북마크")
        self.side.addTab(self.wordbook_panel, "단어장")
        self.side.setMinimumWidth(260)

        # Each pane: its pages, or a review in their place, chosen in its header.
        self.canvases = {"left": self.left, "right": self.right}
        self.scrolls = {"left": self.left_scroll, "right": self.right_scroll}
        self.stacks: dict[str, QStackedWidget] = {}
        self._mode_buttons: dict[str, dict[str, QToolButton]] = {}
        self._modes = {"left": "original", "right": "translation"}
        #: What hides a pane while 백지 is written in the other one.
        self._covers: dict[str, QWidget] = {}
        boxes = {}
        for side in SIDES:
            buttons: dict[str, QToolButton] = {}
            group = QButtonGroup(self)
            for mode, label in PANE_MODES.items():
                button = QToolButton(text=label, checkable=True)
                button.setToolTip(_MODE_TIPS.get(mode, ""))
                button.clicked.connect(lambda _=False, s=side, m=mode: self.set_pane_mode(s, m))
                group.addButton(button)
                buttons[mode] = button
            buttons[self._modes[side]].setChecked(True)
            self._mode_buttons[side] = buttons
            stack = QStackedWidget()
            stack.addWidget(self.scrolls[side])
            stack.addWidget(self.review_panels[side])
            self._covers[side] = self._cover()
            stack.addWidget(self._covers[side])
            self.stacks[side] = stack
            boxes[side] = _pane(_header(*buttons.values()), stack)
        left_box, right_box = boxes["left"], boxes["right"]

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left_box)
        self.splitter.addWidget(right_box)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setChildrenCollapsible(True)

        # The Markdown note sits under both panes, full width, so it can be
        # written while the page and the side tabs stay in view.
        self.vsplitter = QSplitter(Qt.Orientation.Vertical)
        self.vsplitter.addWidget(self.splitter)
        self.vsplitter.addWidget(self.note_panel)
        self.vsplitter.setStretchFactor(0, 7)
        self.vsplitter.setStretchFactor(1, 3)
        self.vsplitter.setCollapsible(0, False)
        self.vsplitter.setChildrenCollapsible(True)

        self.pair_source = QLabel()
        self.pair_target = QLabel()
        for label in (self.pair_source, self.pair_target):
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pair_source.setObjectName("pairSource")
        self.pair_target.setObjectName("pairTarget")
        pair = QFrame()
        pair.setObjectName("pairBar")
        pair_layout = QVBoxLayout(pair)
        pair_layout.setContentsMargins(12, 6, 12, 6)
        pair_layout.setSpacing(2)
        pair_layout.addWidget(self.pair_source)
        pair_layout.addWidget(self.pair_target)
        self._pair_bar = pair
        self._set_pair("")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.vsplitter, 1)
        layout.addWidget(pair)

        self._wire()
        self._apply_notes()
        self._apply_highlights()
        QTimer.singleShot(0, self._initial_zoom)

    # -- wiring -------------------------------------------------------------------

    def _wire(self) -> None:
        for canvas in (self.left, self.right):
            canvas.hovered.connect(self._on_hover)
            canvas.wordClicked.connect(self._on_word)
            canvas.sentenceMenu.connect(self._on_menu)
            canvas.noteClicked.connect(self._edit_note)
            canvas.markMenu.connect(self._on_mark_menu)
            # A stroke belongs to the side it was drawn on, whichever pane shows it.
            canvas.rangeHighlighted.connect(
                lambda page, rects, text, c=canvas: self._add_stroke(_side_of(c), page, rects, text)
            )
        for scroll in (self.left_scroll, self.right_scroll):
            scroll.zoomStep.connect(self.zoom_step)
        ls, rs = self.left_scroll, self.right_scroll
        ls.verticalScrollBar().valueChanged.connect(lambda v: self._sync(rs.verticalScrollBar(), v))
        rs.verticalScrollBar().valueChanged.connect(lambda v: self._sync(ls.verticalScrollBar(), v))
        ls.horizontalScrollBar().valueChanged.connect(lambda v: self._sync(rs.horizontalScrollBar(), v))
        rs.horizontalScrollBar().valueChanged.connect(lambda v: self._sync(ls.horizontalScrollBar(), v))
        for side in SIDES:
            self.scrolls[side].verticalScrollBar().valueChanged.connect(
                lambda value, s=side: s == self._page_side() and self._track_page(value)
            )

        self.dictionary_panel.lookupRequested.connect(
            lambda word, sentence: self.lookup(word, sentence, self.dictionary_panel.sentence_id, self.dictionary_panel.page)
        )
        self.dictionary_panel.starToggled.connect(self._on_star)
        self.dictionary_panel.registerRequested.connect(self._on_register)
        example = self.dictionary_panel.with_example
        example.setChecked(bool(self.settings.get("obsidian_example", True)))
        example.toggled.connect(lambda on: self.settings.set("obsidian_example", on))
        self.notes_panel.jumpRequested.connect(self.jump_to_sentence)
        self.notes_panel.notesChanged.connect(self._apply_notes)
        self.bookmarks_panel.jumpRequested.connect(self._jump_to_bookmark)
        self.bookmarks_panel.notesChanged.connect(self.notes_panel.refresh)
        self.bookmarks_panel.notesChanged.connect(self._apply_notes)
        self.bookmarks_panel.recolorRequested.connect(self._recolor_bookmark)
        self.bookmarks_panel.removeRequested.connect(self._remove_bookmark)
        self.wordbook_panel.jumpRequested.connect(self.jump_to_sentence)

        for canvas in (self.left, self.right):
            canvas.regionCaptured.connect(
                lambda page, rect, c=canvas: self._on_capture(page, rect, translated=c.translated)
            )
            canvas.captureCancelled.connect(self.cancel_capture)
            canvas.pressed.connect(lambda: self.show_evidence([], scroll=False))
            canvas.pressed.connect(self._follow_quiz)
        self.chat_panel.sendRequested.connect(self.ask_gpt)
        self.chat_panel.stopRequested.connect(self.stop_gpt)
        self.chat_panel.imagePasted.connect(
            lambda image: self.chat_panel.attach(self._save_capture(image, None, "붙여 넣은 이미지"))
        )
        self.chat_panel.toNoteRequested.connect(self.answer_to_note)
        self.chat_panel.filesChosen.connect(self.attach_files)
        self.chat_panel.paperChosen.connect(lambda root: self.attach_paper(Path(root)))
        self.chat_panel.evidenceRequested.connect(lambda ids: self.show_evidence(ids, follow=True))
        self.note_panel.statusMessage.connect(self.statusMessage)

        self.quiz_panel.generateRequested.connect(self.generate_quiz)
        self.quiz_panel.generateAllRequested.connect(self.generate_all_quizzes)
        self.quiz_panel.stopRequested.connect(self.stop_quiz)
        self.quiz_panel.slideRequested.connect(self._show_slide)
        self.quiz_panel.evidenceRequested.connect(self._quiz_evidence)
        for panel in (self.quiz_panel, *self.review_panels.values()):
            panel.transcriptsChosen.connect(self._register_transcripts)
            panel.transcriptRemoved.connect(self._remove_transcript)
            panel.transcriptMoved.connect(self._move_transcript)
            panel.lecturesLinked.connect(self._link_lectures)
        self.quiz_panel.lecturesRescan.connect(self._find_lectures)
        self.quiz_panel.stylesChosen.connect(self._register_styles)
        self._find_lectures()
        self.quiz_panel.pdfExportRequested.connect(self.export_quiz_pdf)
        for side, panel in self.review_panels.items():
            other = self.review_panels[_other(side)]
            panel.generateRequested.connect(self.generate_review)
            panel.stopRequested.connect(self.stop_review)
            panel.evidenceRequested.connect(lambda ids, s=side: self._review_evidence(ids, s))
            panel.toNoteRequested.connect(self.review_to_note)
            # The same documents in both panes: one editor at a time, and an
            # edit shows in the other.
            panel.before_edit = lambda _kind, o=other: o.stop_editing()
            panel.documentChanged.connect(lambda kind, o=other: o.kind == kind and o.refresh())
            panel.modeChosen.connect(lambda _mode, o=other: o.refresh())
        self.evidenceLit.connect(lambda count: count or [p.clear_active() for p in self.review_panels.values()])
        self.evidenceLit.connect(lambda count: count or self.blank_panel.clear_active())
        blank = self.blank_panel
        blank.sheetRequested.connect(self.generate_blank_sheet)
        blank.gradeRequested.connect(self.grade_blank)
        blank.stopRequested.connect(self.stop_blank)
        blank.evidenceRequested.connect(lambda ids: self._review_evidence(ids, self._blank_side() or "left"))
        blank.toNoteRequested.connect(self.blank_to_note)
        blank.writingChanged.connect(self._on_blank_writing)
        self.side.currentChanged.connect(lambda _i: self.side.currentWidget() is self.quiz_panel and self._load_deck())
        # Translations arrive after a sentence is marked; shown, the list is current.
        self.side.currentChanged.connect(
            lambda _i: self.side.currentWidget() is self.bookmarks_panel and self.bookmarks_panel.refresh()
        )

        # Qt maps Ctrl to Cmd on a Mac, where Cmd+M minimises and Cmd+H hides
        # the application before the window ever sees the key - so the Mac
        # gets Shift added. See NOTE_KEY / HIGHLIGHT_KEY.
        QShortcut(QKeySequence(NOTE_KEY), self, activated=self._note_on_hover)
        QShortcut(QKeySequence(HIGHLIGHT_KEY), self, activated=self._highlight_on_hover)
        QShortcut(QKeySequence(GPT_KEY), self, activated=self._gpt_on_hover)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel_capture)
        QShortcut(QKeySequence("Ctrl+="), self, activated=lambda: self.zoom_step(1))
        QShortcut(QKeySequence("Ctrl++"), self, activated=lambda: self.zoom_step(1))
        QShortcut(QKeySequence("Ctrl+-"), self, activated=lambda: self.zoom_step(-1))
        QShortcut(QKeySequence("Ctrl+0"), self, activated=self.fit_width)
        QShortcut(QKeySequence(Qt.Key.Key_PageDown), self, activated=lambda: self.goto_page(self._page + 1))
        QShortcut(QKeySequence(Qt.Key.Key_PageUp), self, activated=lambda: self.goto_page(self._page - 1))

    def _sync(self, other: object, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            other.setValue(value)  # type: ignore[attr-defined]
        finally:
            self._syncing = False

    # -- zoom and pages -----------------------------------------------------------

    def _initial_zoom(self) -> None:
        stored = self.settings.get("zoom")
        if isinstance(stored, (int, float)) and stored > 0:
            self.set_zoom(float(stored))
        else:
            self.fit_width()

    def _page_side(self) -> str | None:
        """The pane that shows pages - the left when both do, None when neither."""
        return next((side for side in SIDES if self._modes[side] in PAGE_MODES), None)

    def _view(self) -> tuple[PageCanvas, _Scroll]:
        """The canvas that scrolling and zoom follow, and its scroll area."""
        side = self._page_side() or "left"
        return self.canvases[side], self.scrolls[side]

    def fit_width(self) -> None:
        widest = max((self.session.page_size(n)[0] for n in range(self.session.page_count)), default=612)
        available = self._view()[1].viewport().width() - MARGIN * 2 - 4
        self.set_zoom(max(0.3, available / widest))

    def zoom_step(self, direction: int) -> None:
        self.set_zoom(self.left.zoom * (1.12 if direction > 0 else 1 / 1.12))

    def set_zoom(self, zoom: float) -> None:
        bar = self._view()[1].verticalScrollBar()
        ratio = bar.value() / max(1, bar.maximum())
        self.left.set_zoom(zoom)
        self.right.set_zoom(zoom)
        QTimer.singleShot(0, bar, lambda: bar.setValue(int(ratio * bar.maximum())))
        self.settings.set("zoom", round(self.left.zoom, 3))
        self.statusMessage.emit(f"확대 {self.left.zoom * 100:.0f}%")

    def goto_page(self, number: int) -> None:
        number = max(0, min(self.session.page_count - 1, number))
        canvas, scroll = self._view()
        scroll.verticalScrollBar().setValue(canvas.page_top(number))

    def _track_page(self, value: int) -> None:
        canvas, scroll = self._view()
        probe = value + scroll.viewport().height() / 3
        page = canvas.page_at_y(probe)
        if page != self._page:
            self._page = page
            self.pageChanged.emit(page)
            if self._quiz_away:
                self.quiz_panel.set_away(page + 1)
            else:
                self.quiz_panel.set_slide(page + 1)

    @property
    def current_page(self) -> int:
        return self._page

    def _scroll_to_sentence(self, sentence_id: str) -> bool:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return False
        rects = ref.sentence.rects(ref.block)
        top = rects[0].y0 if rects else ref.block.rect.y0
        canvas, scroll = self._view()
        y = canvas.point_y(ref.page, top) - scroll.viewport().height() // 3
        scroll.verticalScrollBar().setValue(max(0, y))
        return True

    def _jump_to_bookmark(self, sentence_id: str, page: int) -> None:
        if sentence_id and self.session.sentence(sentence_id) is not None:
            self.jump_to_sentence(sentence_id)
        else:
            self.goto_page(page)

    def jump_to_sentence(self, sentence_id: str) -> None:
        if not self._scroll_to_sentence(sentence_id):
            return
        for canvas in (self.left, self.right):
            canvas.set_focus_sentence(sentence_id)
        QTimer.singleShot(2500, lambda: [c.set_focus_sentence("") for c in (self.left, self.right)])

    def show_evidence(self, sentence_ids: list[str], *, scroll: bool = True, follow: bool = False) -> None:
        """Light the sentences a GPT answer cited, on both pages, and bring
        the first into view; an empty list puts the lights out.

        Citations of other open papers light those papers, each in its own
        tab. With ``follow`` (a citation link was clicked) and nothing here
        to show, the tab of the paper cited is brought to the front.
        """
        own: list[str] = []
        elsewhere: dict[ReaderView, list[str]] = {}
        for ref in sentence_ids:
            reader, sentence_id = self._resolve_ref(ref)
            if reader is self:
                own.append(sentence_id)
            elif reader is not None:
                elsewhere.setdefault(reader, []).append(sentence_id)
        ids = [i for i in own if self.session.sentence(i) is not None]
        if ids or not elsewhere:
            for canvas in (self.left, self.right):
                canvas.set_evidence(ids)
            if ids and scroll:
                self._scroll_to_sentence(ids[0])
                self._set_pair(ids[0])
            self.evidenceLit.emit(len(ids))
        for reader, group in elsewhere.items():
            reader.show_evidence(group, scroll=scroll)
        if follow and not ids and elsewhere:
            self.show_peer(next(iter(elsewhere)))

    @property
    def paper_key(self) -> str:
        """Names this paper in a citation made from another one."""
        return str(self.session.project.root.resolve())

    def _resolve_ref(self, ref: str) -> tuple[ReaderView | None, str]:
        """The open paper a cited sentence is in, and its id there."""
        if REF_SEPARATOR not in ref:
            return self, ref
        key, sentence_id = ref.rsplit(REF_SEPARATOR, 1)
        if key == self.paper_key:
            return self, sentence_id
        return next((r for r in self.peers() if r.paper_key == key), None), sentence_id

    def _page_of(self, ref: str) -> int | None:
        reader, sentence_id = self._resolve_ref(ref)
        found = reader.session.sentence(sentence_id) if reader is not None else None
        return found.page if found is not None else None

    def _paper_label(self, ref: str) -> str:
        """A cited paper's short title; "" for this one."""
        reader, _ = self._resolve_ref(ref)
        if reader is None or reader is self:
            return ""
        title = reader.session.project.title
        return title if len(title) <= 24 else title[:23] + "…"

    # -- hover pair (§2.3) -----------------------------------------------------------

    def _on_hover(self, sentence_id: str) -> None:
        self._hover = sentence_id
        self.left.set_hover(sentence_id)
        self.right.set_hover(sentence_id)
        self._set_pair(sentence_id)

    def _set_pair(self, sentence_id: str) -> None:
        ref = self.session.sentence(sentence_id) if sentence_id else None
        if ref is None:
            self.pair_source.setText("문장 위에 마우스를 올리면 원문과 번역이 함께 표시됩니다.")
            self.pair_target.setText("단어 클릭 → 사전 · 드래그 → 형광펜 · 우클릭 → 메모 · " + ("⌘+스크롤" if sys.platform == "darwin" else "Ctrl+휠") + " → 확대")
            return
        self.pair_source.setText(_plain(ref.sentence.text))
        translation = self.session.translation(sentence_id)
        self.pair_target.setText(_plain(translation) if translation else "(아직 번역되지 않음)")

    # -- dictionary (§3) ---------------------------------------------------------------

    def _on_word(self, page: int, text: str, sentence_id: str, sentence: str, _pos: QPoint) -> None:
        latin = re.search(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]|[A-Za-z]", text)
        if latin is None:
            return
        self.lookup(latin.group(0), sentence, sentence_id, page)

    def lookup(self, raw: str, sentence: str, sentence_id: str, page: int) -> None:
        word = clean_word(raw)
        if not word:
            return
        self.side.setCurrentWidget(self.dictionary_panel)
        self._reveal_side()
        panel = self.dictionary_panel
        panel.set_context(word, sentence, sentence_id, page)
        self._lookup_serial += 1
        serial = self._lookup_serial
        if self._lookup_task is not None:
            self._lookup_task.cancel()

        cached = self.dictionary.cached(word, sentence)
        if cached is not None:
            panel.show_entry(cached, starred=self.session.project.wordbook.contains(word), cached=True)
            return

        engine = self._engine_provider()
        related = self.dictionary.related(word)
        if engine is None:
            if related:
                panel.show_entry(related, starred=self.session.project.wordbook.contains(word), cached=True)
            else:
                panel.show_error(word, "사전은 GPT(Codex)를 사용합니다. 번역 사용에 동의하면 뜻을 찾을 수 있습니다.")
            return
        panel.show_loading(word, related)

        def work(cancel, _report):
            return self.dictionary.lookup(word, sentence, engine, cancel=cancel)

        task = BackgroundTask(work, name="lookup")
        task.succeeded.connect(
            lambda entry: serial == self._lookup_serial
            and panel.show_entry(entry, starred=self.session.project.wordbook.contains(word), cached=False)
        )
        task.failed.connect(lambda exc: serial == self._lookup_serial and panel.show_error(word, str(exc)))
        self._lookup_task = task.start()

    def _on_star(self, entry: dict, on: bool) -> None:
        wordbook = self.session.project.wordbook
        word = str(entry.get("word", ""))
        if on:
            panel = self.dictionary_panel
            wordbook.add(
                WordEntry(
                    word=word,
                    lemma=str(entry.get("lemma", "")),
                    pos=str(entry.get("pos", "")),
                    ipa=str(entry.get("ipa", "")),
                    in_context=str(entry.get("in_context", "")),
                    senses=[str(s) for s in entry.get("senses", [])],
                    note=str(entry.get("note", "")),
                    sentence=panel.sentence,
                    sentence_id=panel.sentence_id,
                    page=panel.page,
                )
            )
        else:
            wordbook.remove(word)
        self.wordbook_panel.refresh()

    def _on_register(self, entry: dict, with_example: bool) -> None:
        """File the shown word in the Obsidian vocabulary vault, off the UI thread."""
        vault = self._obsidian_vault()
        if vault is None:
            return
        panel = self.dictionary_panel
        example = example_ko = ""
        if with_example:
            example = panel.sentence
            example_ko = self.session.project.translations.get(panel.sentence_id) or ""
        surface = str(entry.get("word", ""))
        panel.register.setEnabled(False)
        panel.show_status("Obsidian에 기입하는 중…")

        def work(_cancel, _report):
            return register_word(vault, entry, example=example, example_ko=example_ko, surface=surface)

        def finished(result) -> None:
            panel.register.setEnabled(True)
            where = f"{result.korean_file.parent.name}/{result.korean_file.name}"
            if result.already:
                panel.show_status(f"이미 같은 뜻으로 등록된 단어입니다 — {where}")
                return
            added = []
            if result.appended:
                added.append(f"새 뜻 {result.appended}개")
            if result.example_added:
                added.append("기존 뜻에 예문")
            if added:
                message = f"'{result.word}'에 {' · '.join(added)} 추가됨 — {where}"
            else:
                message = f"'{result.word}' 등록됨 — {where}"
            if example and not example_ko:
                message += " (문장 번역이 아직 없어 영문 예문만)"
            panel.show_status(message)

        def failed(exc) -> None:
            panel.register.setEnabled(True)
            message = str(exc) if isinstance(exc, ObsidianError) else f"기입하지 못했습니다: {exc}"
            panel.show_status(message)

        task = BackgroundTask(work, name="obsidian")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        task.start()

    def _obsidian_vault(self) -> Path | None:
        """The vault folder from settings, or asked for once and remembered."""
        stored = self.settings.get("obsidian_vocab_dir") or ""
        if stored and is_vocab_vault(Path(stored)):
            return Path(stored)
        path = QFileDialog.getExistingDirectory(
            self, "Obsidian 영어 단어장 폴더 선택 (dictionary 폴더가 들어 있는 곳)", stored or str(Path.home())
        )
        if not path:
            return None
        if not is_vocab_vault(Path(path)):
            QMessageBox.warning(self, "Obsidian 등록", "선택한 폴더 안에 dictionary 폴더가 없습니다.")
            return None
        self.settings.set("obsidian_vocab_dir", path)
        return Path(path)

    def _reveal_side(self) -> None:
        sizes = self.splitter.sizes()
        if len(sizes) == 3 and sizes[2] < 40:
            total = sum(sizes)
            self.splitter.setSizes([int(total * 0.36), int(total * 0.36), int(total * 0.28)])

    def toggle_side(self) -> None:
        sizes = self.splitter.sizes()
        total = sum(sizes)
        if sizes[2] < 40:
            self.splitter.setSizes([int(total * 0.36), int(total * 0.36), int(total * 0.28)])
        else:
            self.splitter.setSizes([total // 2, total - total // 2, 0])

    def _reveal_note(self) -> None:
        sizes = self.vsplitter.sizes()
        if sizes[1] < 40:
            total = sum(sizes)
            self.vsplitter.setSizes([int(total * 0.7), total - int(total * 0.7)])

    def toggle_note(self) -> None:
        sizes = self.vsplitter.sizes()
        if sizes[1] < 40:
            self._reveal_note()
        else:
            self.vsplitter.setSizes([sum(sizes), 0])

    # -- sentence menu and notes (§4) -------------------------------------------------

    def _on_menu(self, sentence_id: str, pos: QPoint) -> None:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        translation = self.session.translation(sentence_id) or ""
        menu = QMenu(self)
        menu.addAction("메모 추가…", lambda: self.add_note(sentence_id))
        menu.addAction(f"GPT 입력창에 넣기\t{_MOD}G", lambda: self.quote_to_gpt(sentence_id))
        menu.addAction("GPT에게 이 문장 설명 요청", lambda: self.quote_to_gpt(sentence_id, ask=True))
        editing = next((p for p in self.review_panels.values() if p.editing), None)
        if editing is not None:
            menu.addAction(
                f"{REVIEW_KINDS[editing.kind]}에 근거 링크 넣기",
                lambda: editing.insert_citation(sentence_id, ref.page),
            )
        pens = menu.addMenu("형광펜")
        current = self.session.project.highlights.color_of(sentence_id)
        for name, color in HIGHLIGHT_COLORS.items():
            action = pens.addAction(_swatch(color), name, lambda c=color: self.highlight(sentence_id, c))
            action.setCheckable(True)
            action.setChecked(current == color)
        pens.addSeparator()
        erase = pens.addAction("지우기", lambda: self.highlight(sentence_id, None))
        erase.setEnabled(current is not None)
        menu.addSeparator()
        menu.addAction("원문 복사", lambda: QGuiApplication.clipboard().setText(_plain(ref.sentence.text)))
        copy = menu.addAction("번역 복사", lambda: QGuiApplication.clipboard().setText(_plain(translation)))
        copy.setEnabled(bool(translation))
        menu.addAction("번역 직접 고치기…", lambda: self._edit_translation(sentence_id))
        menu.exec(pos)

    def highlight(self, sentence_id: str, color: str | None) -> None:
        """Colour a sentence on both pages, or clear it (``None``)."""
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        marks = self.session.project.highlights
        if color is None:
            marks.remove(sentence_id)
        else:
            marks.set(sentence_id, ref.page, color, _plain(ref.sentence.text))
            self.settings.set("last_highlight", color)
        self._apply_highlights()

    def _highlight_on_hover(self) -> None:
        """Ctrl+H: the last pen used on the sentence under the pointer; again
        on an already highlighted sentence clears it."""
        if not self._hover:
            self.statusMessage.emit(f"칠할 문장 위에 마우스를 올리고 {_MOD}H를 누르세요.")
            return
        if self.session.project.highlights.color_of(self._hover):
            self.highlight(self._hover, None)
        else:
            self.highlight(self._hover, self.settings.get("last_highlight") or "#ffd400")

    def _apply_highlights(self) -> None:
        marks = self.session.project.highlights
        colors = marks.colors()
        self.left.set_highlights(colors)
        self.right.set_highlights(colors)
        for canvas in (self.left, self.right):
            canvas.set_range_marks([
                RangeMark(s.id, s.page, tuple(Rect.from_list(r) for r in s.rects), s.color)
                for s in marks.strokes
                if s.side == _side_of(canvas)
            ])
        self.bookmarks_panel.refresh()

    def _recolor_bookmark(self, kind: str, key: str, color: str) -> None:
        if kind == "stroke":
            self._recolor(key, color)
        else:
            self.highlight(key, color)

    def _remove_bookmark(self, kind: str, key: str) -> None:
        if kind == "stroke":
            self._remove_stroke(key)
        else:
            self.highlight(key, None)

    def _sentence_text(self, sentence_id: str) -> str:
        ref = self.session.sentence(sentence_id)
        return _plain(ref.sentence.text) if ref is not None else ""

    def _sentence_under(self, side: str, page: int, rects: list) -> str:
        """The sentence a stroke starts in, on the side it was drawn on."""
        try:
            placed = self.session.source_rects(page) if side == "source" else self.session.translated_rects(page)
        except (IndexError, KeyError):
            return ""
        for rect in sorted(rects, key=lambda r: (r.y0, r.x0)):
            best = max(
                ((sid, overlap.area) for sid, other in placed if (overlap := rect.intersection(other)) is not None),
                key=lambda pair: pair[1],
                default=None,
            )
            if best is not None:
                return best[0]
        return ""

    # -- free highlighter -------------------------------------------------------------------

    def set_pen(self, color: str) -> None:
        """The colour a drag paints with; "" turns dragging-to-highlight off."""
        self.left.pen_color = color
        self.right.pen_color = color

    def _add_stroke(self, side: str, page: int, rects: list, text: str) -> None:
        pen = self.left.pen_color
        if not pen or not rects:
            return
        self.session.project.highlights.add_stroke(
            side, page, [r.as_list() for r in rects], _plain(text), pen,
            sentence_id=self._sentence_under(side, page, rects),
        )
        self._apply_highlights()

    def _on_mark_menu(self, stroke_id: str, pos: QPoint) -> None:
        marks = self.session.project.highlights
        stroke = next((s for s in marks.strokes if s.id == stroke_id), None)
        if stroke is None:
            return
        menu = QMenu(self)
        for name, color in HIGHLIGHT_COLORS.items():
            action = menu.addAction(_swatch(color), name, lambda c=color: self._recolor(stroke_id, c))
            action.setCheckable(True)
            action.setChecked(stroke.color == color)
        menu.addSeparator()
        menu.addAction("형광펜 지우기", lambda: self._remove_stroke(stroke_id))
        menu.addAction("이 부분 복사", lambda: QGuiApplication.clipboard().setText(stroke.text))
        menu.exec(pos)

    def _recolor(self, stroke_id: str, color: str) -> None:
        self.session.project.highlights.recolor_stroke(stroke_id, color)
        self._apply_highlights()

    def _remove_stroke(self, stroke_id: str) -> None:
        self.session.project.highlights.remove_stroke(stroke_id)
        self._apply_highlights()

    def _gpt_on_hover(self) -> None:
        if self._hover:
            self.quote_to_gpt(self._hover)
        else:
            self.statusMessage.emit(f"GPT에게 보낼 문장 위에 마우스를 올리고 {_MOD}G를 누르세요.")

    def quote_to_gpt(self, sentence_id: str, *, ask: bool = False) -> None:
        """Quote a sentence, and its translation, into the GPT tab's question.

        With ``ask`` the question is sent at once, asking for an explanation;
        otherwise the reader writes their own question under the quote.
        """
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        lines = [f"> p.{ref.page + 1} · {' '.join(_plain(ref.sentence.text).split())}"]
        translation = " ".join(_plain(self.session.translation(sentence_id) or "").split())
        if translation:
            lines.append(f"> (번역) {translation}")
        self.chat_panel.add_quote("\n".join(lines))
        self._show_tab(self.chat_panel)
        if not ask:
            return
        if self.chat_panel.busy:
            self.chat_panel.show_status("앞 질문의 답을 기다리는 중입니다. 끝나면 보내기를 누르세요.")
            return
        current = self.chat_panel.input.toPlainText().rstrip()
        self.chat_panel.input.setPlainText(f"{current}\n\n{EXPLAIN_QUESTION}")
        self.chat_panel.send()

    def _note_on_hover(self) -> None:
        if self._hover:
            self.add_note(self._hover)
        else:
            self.statusMessage.emit(f"메모할 문장 위에 마우스를 올리고 {_MOD}M을 누르세요.")

    def add_note(self, sentence_id: str) -> None:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        self.side.setCurrentWidget(self.notes_panel)
        self._reveal_side()
        self.notes_panel.add_for(sentence_id, ref.page, _plain(ref.sentence.text))

    def _apply_notes(self) -> None:
        notes = [
            MarginNote(note.id, note.sentence_id, note.text)
            for note in self.session.project.notes.anchored
        ]
        self.left.set_margin_notes(notes)
        self.right.set_margin_notes(notes)
        self.bookmarks_panel.refresh()

    def _edit_note(self, note_id: str) -> None:
        self.side.setCurrentWidget(self.notes_panel)
        self._reveal_side()
        self.notes_panel.edit_by_id(note_id)

    def _edit_translation(self, sentence_id: str) -> None:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        current = self.session.translation(sentence_id) or ""
        text, ok = QInputDialog.getMultiLineText(
            self, "번역 고치기", _plain(ref.sentence.text)[:300], current
        )
        if ok and text.strip():
            self.session.project.translations.set(sentence_id, " ".join(text.split()))
            self.session.invalidate(ref.page)
            self._set_pair(sentence_id)

    # -- capture ------------------------------------------------------------------------

    @property
    def capturing(self) -> bool:
        return self.left.capture_mode

    def toggle_capture(self) -> None:
        if self.capturing:
            self.cancel_capture()
        else:
            self.start_capture()

    def start_capture(self) -> None:
        for canvas in (self.left, self.right):
            canvas.set_capture_mode(True)
        self.captureModeChanged.emit(True)
        self.statusMessage.emit(f"캡처할 영역을 원문이나 번역 위에서 드래그하세요. (Esc·우클릭·{CAPTURE_LABEL}: 취소)")

    def cancel_capture(self) -> None:
        if not self.capturing:
            return
        for canvas in (self.left, self.right):
            canvas.set_capture_mode(False)
        self.captureModeChanged.emit(False)

    def _on_capture(self, page: int, rect: Rect, *, translated: bool) -> None:
        self.cancel_capture()
        image = self.session.capture(page, rect, translated=translated)
        alt = f"p.{page + 1} {'번역' if translated else '원문'}"
        menu = QMenu(self)
        ask = menu.addAction("GPT에게 질문하기")
        note = menu.addAction("노트에 붙이기")
        both = menu.addAction("노트에 붙이고 GPT에게도 질문")
        menu.addSeparator()
        copy = menu.addAction("클립보드에 복사")
        chosen = menu.exec(QCursor.pos())
        action = {ask: "ask", note: "note", both: "both", copy: "copy"}.get(chosen)
        if action:
            self.use_capture(image, page, alt, action)

    def use_capture(self, image: QImage, page: int | None, alt: str, action: str) -> None:
        """Do what was chosen with a capture: "ask", "note", "both" or "copy"."""
        if action == "copy":
            QGuiApplication.clipboard().setImage(image)
            self.statusMessage.emit("캡처를 클립보드에 복사했습니다.")
            return
        if action in ("note", "both"):
            if self.note_panel.insert_image(image, alt, page) is not None and action == "note":
                self._show_tab(self.note_panel)
                self.note_panel.editor.setFocus()
        if action in ("ask", "both"):
            self.chat_panel.attach(self._save_capture(image, page, alt))
            self._show_tab(self.chat_panel)

    def _save_capture(self, image: QImage, page: int | None, what: str) -> str:
        path = save_png(image, self.chat_log.new_capture_path(page))
        relative = self.chat_log.relative(path)
        self._capture_notes[relative] = f"{what} 영역을 잘라낸 캡처" if page is not None else what
        return relative

    def _show_tab(self, widget: QWidget) -> None:
        if widget is self.note_panel:
            self._reveal_note()
            return
        self.side.setCurrentWidget(widget)
        self._reveal_side()

    # -- GPT --------------------------------------------------------------------------

    def _paper_text(self, numbering: dict[int, str] | None = None, prefix: str = "") -> tuple[str, dict[int, str]]:
        """Every page, each headed [p.N], tagged as one numbering."""
        numbering = {} if numbering is None else numbering
        pages = []
        for number in range(self.session.page_count):
            text, _ = self._page_text(number, numbering, prefix)
            if text.strip():
                pages.append(f"[p.{number + 1}]\n{text}")
        return "\n\n".join(pages), numbering

    def _page_text(
        self, number: int, numbering: dict[int, str] | None = None, prefix: str = ""
    ) -> tuple[str, dict[int, str]]:
        """The page's text for GPT with every linkable sentence tagged, and
        the sentence id each tag number stands for (continuing ``numbering``
        when one is given; ``prefix`` goes before each id)."""
        numbering = {} if numbering is None else numbering
        paragraphs: list[str] = []
        for block in self.session.layout.pages[number].blocks:
            if not block.text.strip():
                continue
            if not (block.translatable and block.sentences):
                paragraphs.append(_plain(block.text))
                continue
            parts: list[str] = []
            for sentence in block.sentences:
                numbering[len(numbering) + 1] = prefix + sentence.id
                parts.append(f"{sentence_tag(len(numbering))} {_plain(sentence.text)}")
            paragraphs.append(" ".join(parts))
        return "\n\n".join(paragraphs), numbering

    def _sources(self) -> tuple[PaperSource, list[PaperSource]]:
        """This paper and the others open in tabs, for the context builder."""
        own = PaperSource("", self.session.project.title, self.session.layout, self.session.translation)
        others = [
            PaperSource(r.paper_key, r.session.project.title, r.session.layout, r.session.translation)
            for r in self.peers()
        ]
        return own, others

    def _context(self, state: SessionState, scope: str, page: int, query: str) -> list[str]:
        """What has not yet gone to this session, for a question in ``scope``."""
        builder = ContextBuilder(state)
        own, others = self._sources()
        parts: list[str | None] = []
        if scope == "paper":
            parts.append(builder.paper(own, limit=CHAT_PAPER_CHARS))
        elif scope == "tabs" and others:
            papers = [own, *others]
            labels = [
                f"[논문 {i}] {p.title}{' (독자가 보고 있는 논문)' if i == 1 else ''} — " for i, p in enumerate(papers, 1)
            ]
            parts.append(builder.page(own, page, labels[0]))
            parts += builder.related(papers, query, labels)
        elif scope == "related":
            # "이 논문" cut down after OpenAI refused the whole of it.
            parts.append(builder.page(own, page))
            parts += builder.related([own], query, [""])
        else:
            parts.append(builder.page(own, page))
        for index, item in enumerate(self.chat_log.files, start=1):
            header = f"[자료 {index}] {item.name}" + (" (다른 논문)" if item.kind == "paper" else "")
            parts.append(builder.document(item.id, header, self.chat_log.file_text(item), CHAT_FILE_CHARS))
        return [p for p in parts if p]

    def _forget_session(self, state: SessionState) -> None:
        """A session ended (새 대화, a file taken off): delete its file."""
        if state.id and state.engine == "codex":
            forget_codex_session(state.id)

    def ask_gpt(self, question: str, images: list[str]) -> None:
        """Send a question; in a session, only what it has not been sent.

        Two prompts are made: the follow-up for the session the conversation
        is in, and a fresh one carrying everything (the first question, an
        engine without sessions, or a session that could not be resumed).
        """
        if self._chat_task is not None and self._chat_task.running:
            return
        engine = self._engine_provider()
        if engine is None:
            self.chat_panel.failed("GPT 사용에 동의하고 Codex가 설치되어 있어야 질문할 수 있습니다.", question, images)
            return
        pages = [int(m.group(1)) - 1 for i in images if (m := re.search(r"capture-p(\d+)-", i))]
        # A quoted sentence (quote_to_gpt) says which page the question is about.
        pages += [int(n) - 1 for n in re.findall(r"^> p\.(\d+) · ", question, re.MULTILINE)]
        pages = [p for p in pages if 0 <= p < self.session.page_count]
        page = pages[0] if pages else self._page
        scope = self.chat_panel.scope
        # A follow-up ("그건 왜?") is matched together with the question before.
        previous = next((m.text for m in reversed(self.chat_log.messages) if m.role == "user"), "")
        query = f"{question}\n{previous}"

        key = str(getattr(engine, "key", ""))
        state = self.chat_log.session if getattr(engine, "supports_sessions", False) else None
        if state is not None and state.engine != key:
            self.chat_log.end_session()
            state = None
        follow_up: tuple[str, SessionState, list[Path]] | None = None
        if state is not None:
            # The session saw every earlier capture; only new ones go.
            continued = state.copy()
            parts = self._context(continued, scope, page, query)
            notes = [self._capture_notes.get(i, "논문 화면에서 잘라낸 캡처") for i in images]
            prompt = chat_followup_prompt(question, context=parts, image_notes=notes)
            follow_up = (prompt, continued, [self.chat_log.absolute(i) for i in images])

        # A fresh start re-sends the latest captures: every new run starts blind.
        earlier = [] if images else self.chat_log.recent_images(FOLLOW_UP_IMAGES)
        attached = [i for i in [*earlier, *images] if self.chat_log.absolute(i).is_file()]
        notes = [
            ("이전 질문에 첨부했던 " if i in earlier else "") + self._capture_notes.get(i, "논문 화면에서 잘라낸 캡처")
            for i in attached
        ]
        fresh = SessionState(engine=key)
        parts = self._context(fresh, scope, page, query)
        fresh_prompt = chat_prompt(
            question, title=self.session.project.title, history=self.chat_log.history(), image_notes=notes,
            cite=bool(fresh.numbering), context=parts,
        )
        fresh_paths = [self.chat_log.absolute(i) for i in attached]
        # If OpenAI refuses the whole paper (a quoted grading prompt in an
        # appendix will do it), the same question once more with only the
        # page in view and the passages that match it.
        narrow: tuple[str, SessionState] | None = None
        if scope == "paper":
            cut = SessionState(engine=key)
            narrow_parts = self._context(cut, "related", page, query)
            narrow = (
                chat_prompt(
                    question, title=self.session.project.title, history=self.chat_log.history(), image_notes=notes,
                    cite=bool(cut.numbering), context=narrow_parts,
                ),
                cut,
            )

        message = self.chat_log.add(
            "user", question, images=images, page=page, files=[f.name for f in self.chat_log.files]
        )
        self.chat_panel.accepted()
        if follow_up is not None:
            new_chars = len(follow_up[0]) - len(question)
            self.chat_panel.show_status(
                f"이어서 묻는 중… 앞에서 보낸 본문은 다시 보내지 않습니다 (새로 보내는 글 {new_chars:,}자)"
            )
        old = state.id if state is not None else None

        def work(cancel, _report):  # type: ignore[no-untyped-def]
            whole_refused = False
            if follow_up is not None:
                prompt, continued, paths = follow_up
                try:
                    answer, session = _converse(engine, prompt, old, paths, cancel)
                    return answer, session, continued, None, ""
                except (TranslationCancelled, EngineRateLimited):
                    raise
                except EnginePromptRejected:
                    # Sending it all again would be refused again.
                    if narrow is None:
                        raise
                    whole_refused = True
                except TranslationError:
                    pass  # the session is gone or broken: start afresh below
            if not whole_refused:
                try:
                    answer, session = _converse(engine, fresh_prompt, None, fresh_paths, cancel)
                    return answer, session, fresh, old, ""
                except EnginePromptRejected:
                    if narrow is None:
                        raise
            prompt, cut = narrow
            answer, session = _converse(engine, prompt, None, fresh_paths, cancel)
            note = (
                "OpenAI가 논문 전체 본문을 거절해서(부록에 실린 평가용 프롬프트 같은 인용문 때문일 때가 많습니다) "
                "보고 있는 쪽과 질문과 관련된 부분만 보내 답을 받았습니다."
            )
            return answer, session, cut, old, note

        # The paper may be closed before the answer comes back: the log still
        # takes it, but the panel is gone and must not be touched.
        def answered(result) -> None:  # type: ignore[no-untyped-def]
            answer, session, used, dropped, note = result
            if session:
                used.id = session
                self.chat_log.set_session(used)
            elif dropped:
                self.chat_log.end_session()
            if dropped and dropped != session and hasattr(engine, "forget_session"):
                engine.forget_session(dropped)
            text, refs = resolve_citations(answer.strip(), used.numbering)
            self.chat_log.add("assistant", text, refs=refs)
            if not self._closed:
                self.chat_panel.set_busy(False)
                if note:
                    self.chat_panel.show_status(note)
                self.show_evidence(refs)

        def failed(exc: Exception) -> None:
            self.chat_log.remove(message.id)
            if isinstance(exc, EnginePromptRejected) and old:
                # The session now holds the refused message, and would carry
                # it into every later question: the next one starts afresh.
                self.chat_log.end_session()
            if not self._closed:
                text = "질문을 멈췄습니다." if isinstance(exc, TranslationCancelled) else str(exc)
                if isinstance(exc, EnginePromptRejected):
                    text += "\n\n범위를 '보는 쪽'이나 '모든 탭'으로 줄이거나, 문제가 될 만한 쪽을 피해서 다시 물어보세요."
                self.chat_panel.failed(text, question, images)

        task = BackgroundTask(work, name="chat")
        task.succeeded.connect(answered)
        task.failed.connect(failed)
        self._chat_task = task.start()

    # -- files attached to the conversation -------------------------------------------

    def _recent_papers(self) -> list[tuple[str, str]]:
        """Other papers opened before, as (title, folder), for 📎."""
        here = self.session.project.root.resolve()
        papers = []
        for root in self.settings.recent_projects():
            if root.resolve() == here:
                continue
            try:
                title = str(read_json(root / Project.META).get("title") or root.name)
            except (OSError, ValueError, AttributeError):
                continue
            papers.append((title, str(root)))
        return papers

    def attach_files(self, paths: list) -> None:
        """Pictures join the question's captures; documents are read in a
        worker, copied into the project and stay attached."""
        documents = []
        for path in map(Path, paths):
            if path.suffix.lower() in IMAGE_SUFFIXES:
                image = QImage(str(path))
                if not image.isNull():
                    self.chat_panel.attach(self._save_capture(image, None, f"첨부한 이미지 {path.name}"))
            elif self.chat_log.has_source(path):
                self.chat_panel.show_status(f"'{path.name}'은(는) 이미 첨부되어 있습니다.")
            else:
                documents.append(path)
        if not documents:
            return
        log = self.chat_log

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            added = []
            for source in documents:
                pages = read_pages(source)
                if len(pages) > 1:
                    text = "\n\n".join(f"[p.{n}]\n{t.strip()}" for n, t in enumerate(pages, start=1) if t.strip())
                else:
                    text = "".join(pages).strip()
                if not text:
                    raise MaterialError(f"'{source.name}'에서 글자를 찾지 못했습니다. (스캔한 PDF라면 캡처로 보내 주세요)")
                added.append(log.import_file(source, text))
            return added

        self._run_attach(work, f"{len(documents)}개 파일을 읽는 중…")

    def attach_paper(self, root: Path) -> None:
        if self.chat_log.has_source(root):
            self.chat_panel.show_status("그 논문은 이미 첨부되어 있습니다.")
            return
        log = self.chat_log

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            title, text = read_paper(root)
            if not text.strip():
                raise MaterialError(f"'{title}'에서 글자를 찾지 못했습니다.")
            return [log.import_paper(title, text, root)]

        self._run_attach(work, "다른 논문의 본문을 읽는 중…")

    def _run_attach(self, work, message: str) -> None:  # type: ignore[no-untyped-def]
        if self._attach_task is not None and self._attach_task.running:
            self.chat_panel.show_status("앞의 첨부를 읽는 중입니다. 잠시 뒤에 다시 해 주세요.")
            return

        # The files are recorded even if the paper closed meanwhile.
        def finished(added) -> None:  # type: ignore[no-untyped-def]
            for item in added:
                self.chat_log.attach_file(item)
            if not self._closed:
                self.chat_panel.refresh_files()
                names = ", ".join(item.name for item in added)
                self.chat_panel.show_status(f"첨부했습니다: {names} — 떼기 전까지 모든 질문에 함께 갑니다.")

        def failed(exc: Exception) -> None:
            if not self._closed:
                text = str(exc) if isinstance(exc, MaterialError) else f"첨부하지 못했습니다: {exc}"
                self.chat_panel.show_status(text.split("\n")[0])

        task = BackgroundTask(work, name="chat-attach")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        self.chat_panel.show_status(message)
        self._attach_task = task.start()

    def stop_gpt(self) -> None:
        if self._chat_task is not None and self._chat_task.running:
            self._chat_task.cancel()

    def answer_to_note(self, answer_id: str) -> None:
        """Question, its captures and the answer, appended to the note."""
        answer = next((m for m in self.chat_log.messages if m.id == answer_id), None)
        if answer is None:
            return
        question = self.chat_log.question_for(answer_id)
        parts: list[str] = []
        if question is not None:
            if question.text.strip():
                parts.append("> **Q.** " + question.text.strip().replace("\n", "\n> "))
            where = f"p.{question.page + 1}" if question.page is not None else ""
            parts += [self.note_panel.link_file(self.chat_log.absolute(i), where) for i in question.images]
        parts.append(render_citations(answer.text.strip(), answer.refs, self._cite_pages))
        self.note_panel.append("\n\n".join(parts))
        self._show_tab(self.note_panel)
        self.statusMessage.emit("GPT 답변을 노트에 넣었습니다.")

    def _cite_pages(self, ids: list[str]) -> str:
        """Citations as plain text for the note: " (p.3, p.4)", or
        " (Other Paper p.2)" for another paper's sentence."""
        pages = dict.fromkeys(
            f"{label + ' ' if (label := self._paper_label(i)) else ''}p.{p + 1}"
            for i in ids
            if (p := self._page_of(i)) is not None
        )
        return f" ({', '.join(pages)})" if pages else ""

    # -- quiz (§4c) ------------------------------------------------------------------

    def _show_slide(self, slide: int) -> None:
        """The quiz tab's slide buttons: scroll there, and make sure the tab
        follows even where the last page cannot be scrolled to the top."""
        self._quiz_away = False
        self.quiz_panel.set_away(None)
        self.goto_page(slide - 1)
        self.quiz_panel.set_slide(slide)

    def _quiz_evidence_of(self, question: QuizQuestion) -> dict[int, list[str]]:
        """The sentences a quiz question rests on, by page (§4c.5)."""
        if self._evidence_index is None:
            self._evidence_index = EvidenceIndex(
                (page.number, s.id, s.text) for page, _, s in self.session.layout.sentences()
            )
        return self._evidence_index.evidence(f"{question.question}\n{question.explanation}", question.slide - 1)

    def _quiz_evidence(self, ids: list[str], page: int) -> None:
        """Light what a quiz question rests on; with ``page`` (from 1), go
        there, the quiz tab staying on the question if that is another slide."""
        if page and self._page_side() is None:
            # Both panes show documents: the left turns to the original.
            self.set_pane_mode("left", "original")
            QTimer.singleShot(0, self, lambda: self._quiz_evidence(ids, page))
            return
        self.show_evidence(ids, scroll=False)
        if not page:
            return
        slide = self.quiz_panel.slide
        # The starred view does not follow the pages, so it is never away.
        if page != slide and not self.quiz_panel.showing_favorites:
            self._quiz_away = True
            self.quiz_panel.set_away(page)
        first = next((i for i in ids if self._page_of(i) == page - 1), None)
        if first is None or not self._scroll_to_sentence(first):
            self.goto_page(page - 1)
        if first is not None:
            self._set_pair(first)
        if page == slide:
            self._follow_quiz(slide)

    def _follow_quiz(self, slide: int | None = None) -> None:
        """The quiz tab follows the pages again, from ``slide`` or the page in view."""
        if self._quiz_away:
            self._quiz_away = False
            self.quiz_panel.set_away(None)
            self.quiz_panel.set_slide(slide or self._page + 1)

    def _load_deck(self) -> None:
        """Read every slide's text once, for the titles the quiz tab shows."""
        if self._deck is not None or (self._deck_task is not None and self._deck_task.running):
            return
        pdf = self.session.project.pdf_path

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            return SlideDeck(read_pages(pdf))

        task = BackgroundTask(work, name="slides")
        task.succeeded.connect(self._deck_loaded)
        self._deck_task = task.start()

    def _deck_loaded(self, deck: SlideDeck) -> None:
        self._deck = deck
        if not self._closed:
            self.quiz_panel.set_titles({page + 1: title for page, title in deck.titles().items()})

    def _quiz_maker(
        self, engine: TranslationEngine
    ) -> tuple[Callable[[int, threading.Event], SlideQuiz], Callable[[], SlideDeck]]:
        """What a slide's quiz is made from, taken on the UI thread: a function
        that makes one slide's quiz in a worker, and one that gives the deck.

        The deck, the STT matched to it and the style examples are read once,
        however many slides are made; PyMuPDF is used by one thread at a time.
        """
        project, store = self.session.project, self.quiz_store
        pdf, deck = project.pdf_path, self._deck
        transcripts, read_styles = store.transcript_parts(), self._style_reader()
        images = store.root / QuizStore.FOLDER / "slides" if store.send_slide_image else None
        prefix, lecture = id_prefix(project.title), project.title
        lock = threading.Lock()
        read: dict[str, Any] = {}

        def context() -> dict[str, Any]:
            with lock:
                if not read:
                    slides = deck or SlideDeck(read_pages(pdf))
                    sections = combine_transcripts([parse_transcript(text) for _, text in transcripts]) if transcripts else []
                    read.update(
                        slides=slides, sections=sections, styles=read_styles(),
                        matched=align_transcript(slides.pages, sections) if sections else {},
                    )
                return read

        def make(slide: int, cancel: threading.Event) -> SlideQuiz:
            found = context()
            slides, page = found["slides"], slide - 1
            content = slides.content(page)
            excerpt = section = ""
            if found["sections"]:
                excerpt, section = transcript_excerpt(found["sections"], found["matched"].get(page, []))
            image_path = images / f"slide-{slide}.png" if images is not None else None
            if not content.strip() and not excerpt.strip() and image_path is None:
                raise TranslationError(
                    "이 슬라이드에서 텍스트를 찾지 못했고 연결된 STT도 없습니다.\n"
                    "그림뿐인 슬라이드라면 자료에서 '슬라이드 이미지도 함께 보내기'를 켜 주세요."
                )
            image = None
            if image_path is not None:
                with lock, PaperDocument(pdf) as document:
                    image = document.save_png(page, image_path)
            request = QuizGenerationInput(
                slide_number=slide, slide_title=slides.title(page), slide_content=content,
                transcript_content=excerpt, transcript_section=section, quiz_style_examples=found["styles"],
                lecture_title=lecture, slide_image=image, id_prefix=prefix,
            )
            return CodexQuizGenerator(engine).generate_slide_quiz(request, cancel=cancel)

        return make, lambda: context()["slides"]

    def generate_quiz(self, slide: int) -> None:
        if self._quiz_task is not None and self._quiz_task.running:
            return
        engine = self._engine_provider()
        if engine is None:
            self.quiz_panel.set_error(slide, "GPT 사용에 동의하고 Codex가 설치되어 있어야 퀴즈를 만들 수 있습니다.")
            return
        store = self.quiz_store
        make, deck_of = self._quiz_maker(engine)

        def work(cancel, _report):  # type: ignore[no-untyped-def]
            quiz = make(slide, cancel)
            return deck_of(), quiz

        def finished(result) -> None:  # type: ignore[no-untyped-def]
            slides, quiz = result
            store.put(quiz)
            if self._deck is None:
                self._deck_loaded(slides)
            if not self._closed:
                self.quiz_panel.set_generating(None)
                self.statusMessage.emit(f"Slide {slide} 퀴즈를 만들었습니다.")

        def failed(exc: Exception) -> None:
            if self._closed:
                return
            self.quiz_panel.set_generating(None)
            if isinstance(exc, TranslationCancelled):
                self.statusMessage.emit("퀴즈 생성을 멈췄습니다.")
            else:
                self.quiz_panel.set_error(slide, str(exc))

        task = BackgroundTask(work, name="quiz")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        self.quiz_panel.set_generating(slide)
        self._quiz_task = task.start()

    def generate_all_quizzes(self) -> None:
        """A quiz for every slide that has none, ``QUIZ_PARALLEL`` Codex runs at
        a time (§4c.7). Each is kept as it arrives; one that fails is marked on
        its slide and the rest go on; a usage limit or 중지 ends the run."""
        if self._quiz_task is not None and self._quiz_task.running:
            return
        store = self.quiz_store
        wanted = [n for n in range(1, self.session.page_count + 1) if store.get(n) is None]
        if not wanted:
            return
        engine = self._engine_provider()
        if engine is None:
            self.quiz_panel.finish_batch("GPT 사용에 동의하고 Codex가 설치되어 있어야 퀴즈를 만들 수 있습니다.")
            return
        make, deck_of = self._quiz_maker(engine)
        counts = {"made": 0, "failed": 0}

        def work(cancel, report):  # type: ignore[no-untyped-def]
            deck_of()  # read once, before the runs share it
            limited: EngineRateLimited | None = None
            queue, running = list(wanted), {}
            with ThreadPoolExecutor(max_workers=QUIZ_PARALLEL, thread_name_prefix="quiz") as pool:
                while queue or running:
                    while queue and len(running) < QUIZ_PARALLEL and not cancel.is_set():
                        slide = queue.pop(0)
                        running[pool.submit(make, slide, cancel)] = slide
                        report(("start", slide, None))
                    if not running:
                        break
                    finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
                    for future in finished:
                        slide = running.pop(future)
                        try:
                            report(("quiz", slide, future.result()))
                        except TranslationCancelled:
                            report(("stopped", slide, None))
                        except EngineRateLimited as exc:
                            # The others would meet the same limit: stop them.
                            limited = exc
                            cancel.set()
                            report(("stopped", slide, None))
                        except Exception as exc:  # noqa: BLE001 - one slide's failure is that slide's
                            report(("failed", slide, str(exc)))
            if limited is not None:
                raise limited
            if cancel.is_set():
                raise TranslationCancelled()
            return deck_of()

        def progress(event) -> None:  # type: ignore[no-untyped-def]
            kind, slide, payload = event
            if kind == "quiz":
                # Imported meanwhile from a file: what the reader brought stays.
                if store.get(slide) is None:
                    store.put(payload)
                counts["made"] += 1
            elif kind == "failed":
                counts["failed"] += 1
            if self._closed:
                return
            if kind == "failed":
                self.quiz_panel.set_error(slide, payload)
            self.quiz_panel.batch_progress(slide, kind)

        def summary() -> str:
            made, failed = counts["made"], counts["failed"]
            return f"퀴즈 {made}장을 만들었습니다" + (f" · 실패 {failed}장 (그 슬라이드에서 Retry)" if failed else "")

        def finished(slides) -> None:  # type: ignore[no-untyped-def]
            if self._deck is None:
                self._deck_loaded(slides)
            if not self._closed:
                self.quiz_panel.finish_batch(summary() + ".")
                self.statusMessage.emit(summary() + ".")

        def failed(exc: Exception) -> None:
            if self._closed:
                return
            if isinstance(exc, TranslationCancelled):
                message = f"멈췄습니다. {summary()}."
            elif isinstance(exc, EngineRateLimited):
                message = f"ChatGPT 사용량 한도에 걸려 멈췄습니다. {summary()}. 나머지는 나중에 다시 누르면 이어서 만듭니다."
            else:
                message = f"{summary()}. 멈춘 이유: {exc}"
            self.quiz_panel.finish_batch(message)
            self.statusMessage.emit(message)

        task = BackgroundTask(work, name="quiz-all")
        task.progress.connect(progress)
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        self.quiz_panel.start_batch(len(wanted))
        self._quiz_task = task.start()

    def stop_quiz(self) -> None:
        if self._quiz_task is not None and self._quiz_task.running:
            self._quiz_task.cancel()

    def export_quiz_pdf(self, path: str, layout: str = "quiz") -> None:
        """Every slide's quiz in one PDF, then the answers.

        ``layout`` is "quiz" for the questions alone, or "right" / "below" for
        every slide with its questions set beside or under it.
        """
        if self._quiz_pdf_task is not None and self._quiz_pdf_task.running:
            return
        store = self.quiz_store
        quizzes, attempts = dict(store.quizzes), {n: copy.deepcopy(a) for n, a in store.attempts.items()}
        titles = self._deck.titles() if self._deck is not None else {}
        titles = {page + 1: title for page, title in titles.items()}
        target, lecture, slides = Path(path), self.session.project.title, self.session.project.pdf_path
        if layout == "favorites":
            # The starred questions alone, the key at the end but not the
            # answers once given: a sheet to do again.
            quizzes, attempts, layout, lecture = store.favorite_quizzes(), {}, "quiz", f"{lecture} · 즐겨찾기"

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            if layout in PLACEMENTS:
                return slides_quiz_pdf(
                    slides, quizzes, target, placement=layout, lecture=lecture, attempts=attempts, titles=titles
                )
            return quiz_pdf(quizzes, target, lecture=lecture, attempts=attempts, titles=titles)

        def finished(count: int) -> None:
            if self._closed:
                return
            self.quiz_panel.set_pdf_busy(False, f"퀴즈 {count}개 슬라이드를 PDF로 저장했습니다: {target}")
            self.statusMessage.emit(f"퀴즈 PDF를 저장했습니다: {target.name}")

        def failed(exc: Exception) -> None:
            if self._closed:
                return
            self.quiz_panel.set_pdf_busy(False, "")
            QMessageBox.warning(self, "퀴즈 PDF", f"PDF를 만들지 못했습니다.\n\n{exc}")

        task = BackgroundTask(work, name="quiz-pdf")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        self.quiz_panel.set_pdf_busy(True, "퀴즈 PDF를 만드는 중…")
        self._quiz_pdf_task = task.start()

    def _material_running(self) -> bool:
        if self._material_task is not None and self._material_task.running:
            self.quiz_panel.materials_status.setText("앞의 자료를 읽는 중입니다. 잠시 후 다시 시도하세요.")
            return True
        return False

    def _register_transcripts(self, paths: list) -> None:
        """Read STT files off the UI thread, copy them in, and say what was
        found. They join the list after the ones there, in the order picked."""
        if self._material_running():
            self.statusMessage.emit("앞의 자료를 읽는 중입니다. 잠시 후 다시 시도하세요.")
            return
        store, sources = self.quiz_store, [Path(p) for p in paths]

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            added = []
            for source in sources:
                text = "\n".join(read_pages(source))
                if not text.strip():
                    raise MaterialError(
                        f"'{source.name}'에서 텍스트를 찾지 못했습니다. (스캔한 PDF라면 텍스트 PDF나 txt로 바꿔 주세요)"
                    )
                added.append((store.import_material(source, text), parse_transcript(text)))
            return added

        def finished(added) -> None:  # type: ignore[no-untyped-def]
            found = []
            for material, sections in added:
                store.add_transcript(material)
                slides = sum(1 for s in sections if s.number is not None)
                found.append(f"{material.name} — " + (
                    f"슬라이드 표시 {slides}개 구간" if slides else f"{len(sections)}개 구간 (슬라이드 표시 없음 · 내용으로 연결)"
                ))
            if not self._closed:
                message = "STT 등록: " + " / ".join(found)
                self.quiz_panel.set_material_busy(False, message)
                for panel in self.review_panels.values():
                    panel.refresh()
                self.statusMessage.emit(message)

        self._run_material(work, finished, f"STT {len(sources)}개를 읽는 중…")

    def _remove_transcript(self, file: str) -> None:
        self.quiz_store.remove_transcript(file)
        self._transcripts_changed()

    def _move_transcript(self, file: str, step: int) -> None:
        self.quiz_store.move_transcript(file, step)
        self._transcripts_changed()

    def _transcripts_changed(self) -> None:
        self.quiz_panel.refresh_materials()
        for panel in self.review_panels.values():
            panel.refresh()

    def _register_styles(self, paths: list) -> None:
        """Read the professor's earlier quizzes, put them in the style
        library (once, for every lecture) and link them to this one."""
        if self._material_running():
            return
        store, library = self.quiz_store, self.style_library

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            added = []
            for path in paths:
                source = Path(path)
                text, count = _style_examples(source)
                if not text.strip():
                    raise MaterialError(f"'{source.name}'에서 텍스트를 찾지 못했습니다.")
                added.append((library.add(source), count))
            return added

        def finished(added) -> None:  # type: ignore[no-untyped-def]
            for link, _count in added:
                store.link_style(link)
            summary = ", ".join(f"{link} (문제 {n}개 인식)" if n else f"{link} (문항 형식 없음 · 본문 일부 사용)" for link, n in added)
            if not self._closed:
                self.quiz_panel.set_material_busy(False, f"스타일 참고 추가: {summary} — 퀴즈 스타일 폴더에 넣었습니다")

        self._run_material(work, finished, "이전 퀴즈를 읽는 중…")

    def _style_reader(self) -> Callable[[], str]:
        """The style references as they stand now, to be read in a worker:
        the copied ones, then every file the library links reach."""
        store, library = self.quiz_store, self.style_library
        copied, links, skip = store.style_text(), list(store.style_links), [m.name for m in store.styles]

        def read() -> str:
            linked = library.text(links, lambda path: _style_examples(path)[0], skip) if links else ""
            return "\n\n".join(part for part in (copied, linked) if part)

        return read

    # -- Lecture Saver ------------------------------------------------------------------

    def _find_lectures(self) -> None:
        """What Lecture Saver holds, and which recordings are named like this PDF."""
        if self._lecture_task is not None and self._lecture_task.running:
            return
        places = self.settings.lecture_saver_places()
        project = self.session.project
        name = Path(str(project.meta.get("source") or project.pdf_path)).name

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            found = find_projects(places)
            return found, [p.key for p in found if same_lecture(name, p.name)]

        def finished(result) -> None:  # type: ignore[no-untyped-def]
            if not self._closed:
                self.quiz_panel.set_lectures(*result)

        task = BackgroundTask(work, name="lecture-saver")
        task.succeeded.connect(finished)
        task.failed.connect(lambda exc: log.warning("Could not look through Lecture Saver: %s", exc))
        self._lecture_task = task.start()

    def _link_lectures(self, keys: list) -> None:
        """Read the STT from Lecture Saver projects from now on, after what is there."""
        if self._material_running():
            self.statusMessage.emit("앞의 자료를 읽는 중입니다. 잠시 후 다시 시도하세요.")
            return
        store = self.quiz_store
        known = {p.key: p for p in self.quiz_panel.lectures}
        projects = [known.get(key) or LectureProject(Path(key), Path(key).name) for key in keys]

        def work(_cancel, _report):  # type: ignore[no-untyped-def]
            return [(project, transcript_markdown(project.root)) for project in projects]

        def finished(read) -> None:  # type: ignore[no-untyped-def]
            found = []
            for project, text in read:
                store.link_lecture(project, text)
                sections = parse_transcript(text)
                slides = sum(1 for s in sections if s.number is not None)
                found.append(f"{project.name} — " + (f"슬라이드 {slides}개 구간" if slides else f"{len(sections)}개 구간"))
            if not self._closed:
                message = "Lecture Saver STT 연결: " + " / ".join(found)
                self.quiz_panel.set_material_busy(False, message)
                for panel in self.review_panels.values():
                    panel.refresh()
                self.statusMessage.emit(message)

        self._run_material(work, finished, f"Lecture Saver에서 STT {len(projects)}개를 읽는 중…")

    def _run_material(self, work, finished, message: str) -> None:  # type: ignore[no-untyped-def]
        def failed(exc: Exception) -> None:
            if not self._closed:
                text = str(exc) if isinstance(exc, (MaterialError, LectureSaverError)) else f"자료를 읽지 못했습니다: {exc}"
                self.quiz_panel.set_material_busy(False, text)
                self.statusMessage.emit(text.split("\n")[0])

        task = BackgroundTask(work, name="quiz-material")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        self.quiz_panel.set_material_busy(True, message)
        self._material_task = task.start()

    # -- one-page review and Cornell notes (§4d) ------------------------------------------

    def _default_review_mode(self) -> str:
        """강의 모드 for slides or a paper with its STT, 논문 모드 otherwise."""
        lecture = default_lecture(self.session.layout, bool(self.quiz_store.transcripts))
        return "lecture" if lecture else "paper"

    def pane_mode(self, side: str) -> str:
        """What a pane shows: "original", "translation", "one_page" or "cornell"."""
        return self._modes[side]

    def set_pane_mode(self, side: str, mode: str) -> None:
        if side not in SIDES or mode not in PANE_MODES:
            return
        other = _other(side)
        if mode == "blank" and self._modes[other] == "blank":
            # One 백지 panel: it comes here, and the other pane shows the original.
            self.set_pane_mode(other, "original")
        self._mode_buttons[side][mode].setChecked(True)
        panel, stack = self.review_panels[side], self.stacks[side]
        before = self._page_side()
        if self._modes[side] == "blank" and mode != "blank":
            self.blank_panel.flush()
        self._modes[side] = mode
        if mode == "blank":
            panel.stop_editing()
            if stack.indexOf(self.blank_panel) < 0:
                self.stacks[other].removeWidget(self.blank_panel)
                stack.addWidget(self.blank_panel)
            self._show_stacks()
            return
        if mode not in PAGE_MODES:
            panel.set_kind(mode)
            self._show_stacks()
            return
        panel.stop_editing()
        self.canvases[side].set_translated(mode == "translation")
        self._apply_highlights()
        self._show_stacks()
        # Hidden, these pages kept scrolling with the others but were not laid
        # out; line them up again once they are.
        source = before if before != side else None
        QTimer.singleShot(0, self, lambda: self._align(side, source))

    def _align(self, side: str, source: str | None) -> None:
        """Line a pane's pages up with the other pane's, or with the page last in view."""
        bars = self.scrolls[side]
        if source is not None:
            self._sync(bars.verticalScrollBar(), self.scrolls[source].verticalScrollBar().value())
            self._sync(bars.horizontalScrollBar(), self.scrolls[source].horizontalScrollBar().value())
        else:
            self._sync(bars.verticalScrollBar(), self.canvases[side].page_top(self._page))

    def _review_evidence(self, ids: list[str], side: str = "right") -> None:
        found = [i for i in ids if self.session.sentence(i) is not None]
        if not found:
            self.statusMessage.emit("근거 문장을 이 논문에서 찾지 못했습니다. (링크가 가리키는 문장이 없습니다)")
            return
        if self._page_side() is None:
            # Both panes show documents: the other one turns to the original.
            self.set_pane_mode(_other(side), "original")
            QTimer.singleShot(0, self, lambda: self._review_evidence(found, side))
            return
        self.show_evidence(found)
        pages = ", ".join(dict.fromkeys(f"p.{self._page_of(i) + 1}" for i in found))
        self.statusMessage.emit(f"근거 문장 {len(found)}개 ({pages}) — 페이지를 누르면 표시가 꺼집니다.")

    def generate_review(self, kind: str) -> None:
        """Write a one-page review or Cornell notes of the whole paper."""
        task = self._review_tasks.get(kind)
        if kind not in REVIEW_KINDS or (task is not None and task.running):
            return
        engine = self._engine_provider()
        if engine is None:
            for panel in self.review_panels.values():
                panel.set_error(kind, "GPT 사용에 동의하고 Codex가 설치되어 있어야 만들 수 있습니다.")
            return
        layout, store = self.session.layout, self.review_store
        prepare, names, lecture = self._paper_request(kind)
        mode = "lecture" if lecture else "paper"

        def work(cancel, _report):  # type: ignore[no-untyped-def]
            generator = CodexReviewGenerator(engine)
            markdown, note = _whole_paper(prepare(), lambda request: generator.generate(request, cancel=cancel))
            texts = {s.id: s.text for _, _, s in layout.sentences()}
            anchors = {i: texts[i] for i in refs_in(markdown) if i in texts}
            return markdown, anchors, note

        # Kept even if the paper closed meanwhile: it was paid for.
        def finished(result) -> None:  # type: ignore[no-untyped-def]
            markdown, anchors, note = result
            model = str(getattr(engine, "chat_model", "") or "")
            store.put(
                kind, markdown, anchors=anchors, engine=str(getattr(engine, "key", "")), model=model,
                transcripts=list(names), mode=mode,
            )
            if not self._closed:
                for panel in self.review_panels.values():
                    panel.set_generating(kind, False)
                self.statusMessage.emit(note or f"{REVIEW_KINDS[kind]} 완성 — 항목을 누르면 근거 문장을 원문에서 보여 줍니다.")

        def failed(exc: Exception) -> None:
            if self._closed:
                return
            for panel in self.review_panels.values():
                panel.set_generating(kind, False)
            if isinstance(exc, TranslationCancelled):
                self.statusMessage.emit("만들기를 멈췄습니다.")
                return
            text = str(exc)
            if isinstance(exc, EnginePromptRejected):
                text += "\n\nOpenAI가 이 논문의 본문을 거절했습니다. 부록을 뺀 본문도 거절되었습니다."
            for panel in self.review_panels.values():
                panel.set_error(kind, text)

        task = BackgroundTask(work, name=f"review-{kind}")
        task.succeeded.connect(finished)
        task.failed.connect(failed)
        for panel in self.review_panels.values():
            panel.set_generating(kind, True)
        self._review_tasks[kind] = task.start()

    def _paper_request(self, kind: str) -> tuple[Callable[[], Callable[..., ReviewRequest]], tuple[str, ...], bool]:
        """What a run over the whole paper is asked from, gathered now and
        read in the worker: ``prepare()`` there returns ``ask(until_references
        =False)``, which builds the request. Also the STT files' names, and
        whether it is written as a lecture (the mode the review panes show)."""
        layout, title = self.session.layout, self.session.project.title
        pdf, deck = self.session.project.pdf_path, self._deck
        transcripts = self.quiz_store.transcript_parts()
        read_styles = self._style_reader()
        lecture = self.review_panels["left"].mode == "lecture"

        def prepare() -> Callable[..., ReviewRequest]:
            styles = read_styles()
            try:
                strays = stray_lines(pdf, layout)
            except Exception:  # pymupdf raises several unrelated types
                log.warning("Could not read the lines the layout left out", exc_info=True)
                strays = {}
            # The STT is matched to the slides as the quiz matches them, all of it.
            slides = (deck or SlideDeck(read_pages(pdf))).pages if transcripts else []

            def ask(until_references: bool = False) -> ReviewRequest:
                return review_request(
                    kind, title, layout, strays=strays, transcripts=transcripts, slides=slides, styles=styles,
                    lecture=lecture, until_references=until_references,
                )

            return ask

        return prepare, tuple(name for name, _ in transcripts), lecture

    def stop_review(self, kind: str) -> None:
        task = self._review_tasks.get(kind)
        if task is not None and task.running:
            task.cancel()

    def review_to_note(self, kind: str) -> None:
        """The document into the note, its citations as page numbers."""
        for panel in self.review_panels.values():
            panel.flush()
        doc = self.review_store.get(kind)
        if doc is None:
            return
        self.note_panel.append(plain_citations(doc.text).strip())
        self._show_tab(self.note_panel)
        self.statusMessage.emit(f"노트에 넣었습니다: {REVIEW_KINDS[kind]}")

    # -- 백지 복습 (§4e) -----------------------------------------------------------------

    def _cover(self) -> QWidget:
        """What a pane shows while 백지 is written in the other one."""
        cover = QWidget()
        cover.setObjectName("blankCover")
        layout = QVBoxLayout(cover)
        layout.addStretch(1)
        label = QLabel(
            "<p style='font-size:13pt'><b>백지 쓰는 중</b></p>"
            "<p style='color:#5f6368'>보지 않고 쓰도록 이쪽은 가려 두었습니다.<br>채점이 끝나면 다시 보입니다.</p>"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        peek = QToolButton(text="그래도 보기")
        peek.setToolTip("이번 백지가 끝날 때까지 가리지 않습니다.")
        peek.clicked.connect(self._peek)
        layout.addWidget(peek, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(2)
        return cover

    def _blank_side(self) -> str | None:
        return next((side for side in SIDES if self._modes[side] == "blank"), None)

    def covered(self, side: str) -> bool:
        """Whether a pane is hidden because 백지 is being written in the other."""
        blank = self._blank_side()
        return blank is not None and side != blank and self.blank_panel.writing and not self._peeking

    def _show_stacks(self) -> None:
        """Each pane's stack on what its mode shows, or on its cover."""
        for side in SIDES:
            mode, stack = self._modes[side], self.stacks[side]
            if self.covered(side):
                stack.setCurrentWidget(self._covers[side])
            elif mode == "blank":
                stack.setCurrentWidget(self.blank_panel)
            elif mode in PAGE_MODES:
                stack.setCurrentWidget(self.scrolls[side])
            else:
                stack.setCurrentWidget(self.review_panels[side])

    def _on_blank_writing(self, on: bool) -> None:
        if on:
            self._peeking = False
            self.show_evidence([], scroll=False)
        self._show_stacks()

    def _peek(self) -> None:
        self._peeking = True
        self._show_stacks()

    def generate_blank_sheet(self) -> None:
        """The keyword sheet and its answer key, from the PDF."""
        if self._blank_task is not None and self._blank_task.running:
            return
        panel = self.blank_panel
        engine = self._engine_provider()
        if engine is None:
            panel.set_error("GPT 사용에 동의하고 Codex가 설치되어 있어야 만들 수 있습니다.")
            return
        layout, store, pdf = self.session.layout, self.blank_store, self.session.project.pdf_path
        prepare, names, lecture = self._paper_request("blank")
        send_figures = store.send_figures and bool(getattr(engine, "supports_images", True))

        def work(cancel, _report):  # type: ignore[no-untyped-def]
            generator = CodexBlankGenerator(engine)
            regions = figure_regions(layout) if send_figures else []
            # The pictures exist only for this run.
            with tempfile.TemporaryDirectory(prefix="paper-saver-figures-", ignore_cleanup_errors=True) as folder:
                figures = []
                if regions:
                    try:
                        with PaperDocument(pdf) as document:
                            for number, (page, rect) in enumerate(regions, start=1):
                                path = document.save_region_png(page, rect, Path(folder) / f"figure-{number}.png")
                                figures.append((page, path))
                    except Exception:  # pymupdf raises several unrelated types
                        log.warning("Could not cut the figures out of the PDF", exc_info=True)
                        figures = []
                sheet, note = _whole_paper(
                    prepare(), lambda request: generator.sheet(request, figures=figures, cancel=cancel)
                )
            sheet.figures = len(figures)
            texts = {s.id: s.text for _, _, s in layout.sentences()}
            anchors = {i: texts[i] for point in sheet.points() for i in point.refs if i in texts}
            return sheet, anchors, note

        # Kept even if the paper closed meanwhile: it was paid for.
        def finished(result) -> None:  # type: ignore[no-untyped-def]
            sheet, anchors, note = result
            sheet.engine = str(getattr(engine, "key", ""))
            sheet.model = str(getattr(engine, "chat_model", "") or "")
            sheet.mode = "lecture" if lecture else "paper"
            sheet.transcripts = list(names)
            store.put_sheet(sheet, anchors=anchors)
            if not self._closed:
                panel.set_busy("")
                self.statusMessage.emit(
                    note or f"백지 복습 준비 완료 — 주제 {len(sheet.topics)}개. 주제를 고르고 '백지 시작'을 누르세요."
                )

        task = BackgroundTask(work, name="blank-sheet")
        task.succeeded.connect(finished)
        task.failed.connect(self._blank_failed)
        panel.set_busy("sheet")
        self._blank_task = task.start()

    def grade_blank(self, attempt_id: str) -> None:
        """Mark what the reader wrote against the key, the PDF beside it."""
        if self._blank_task is not None and self._blank_task.running:
            return
        panel, store = self.blank_panel, self.blank_store
        attempt, sheet = store.get(attempt_id), store.sheet
        if attempt is None or sheet is None:
            return
        engine = self._engine_provider()
        if engine is None:
            panel.set_error("GPT 사용에 동의하고 Codex가 설치되어 있어야 채점할 수 있습니다.")
            return
        layout = self.session.layout
        prepare, _names, _lecture = self._paper_request("blank")
        topics, written = list(attempt.topics), attempt.text

        def work(cancel, _report):  # type: ignore[no-untyped-def]
            generator = CodexBlankGenerator(engine)
            graded, note = _whole_paper(
                prepare(), lambda request: generator.grade(request, sheet, topics, written, cancel=cancel)
            )
            texts = {s.id: s.text for _, _, s in layout.sentences()}
            anchors = {i: texts[i] for wrong in graded[2] for i in wrong.refs if i in texts}
            return graded, anchors, note

        # Kept even if the paper closed meanwhile: it was paid for.
        def finished(result) -> None:  # type: ignore[no-untyped-def]
            (marks, missing, wrong, overall), anchors, note = result
            marked = store.grade(
                attempt_id, marks, missing, wrong, overall, anchors=anchors,
                model=str(getattr(engine, "chat_model", "") or ""),
            )
            if not self._closed:
                panel.set_busy("")
                if marked is not None:
                    got, total = marked.score()
                    self.statusMessage.emit(
                        note or f"채점 완료 — {got:g}/{total}. 빠뜨린 것을 누르면 PDF에서 그 문장을 보여 줍니다."
                    )

        task = BackgroundTask(work, name="blank-grade")
        task.succeeded.connect(finished)
        task.failed.connect(self._blank_failed)
        panel.set_busy("grade")
        self._blank_task = task.start()

    def _blank_failed(self, exc: Exception) -> None:
        if self._closed:
            return
        self.blank_panel.set_busy("")
        if isinstance(exc, TranslationCancelled):
            self.statusMessage.emit("멈췄습니다. 쓴 글은 그대로 저장되어 있습니다.")
            return
        text = str(exc)
        if isinstance(exc, EnginePromptRejected):
            text += "\n\nOpenAI가 이 PDF의 본문을 거절했습니다. 부록을 뺀 본문도 거절되었습니다."
        self.blank_panel.set_error(text)

    def stop_blank(self) -> None:
        if self._blank_task is not None and self._blank_task.running:
            self._blank_task.cancel()

    def blank_to_note(self, attempt_id: str) -> None:
        """A marked attempt into the note, its citations as page numbers."""
        text = self.blank_panel.result_text(attempt_id)
        if not text:
            return
        self.note_panel.append(text)
        self._show_tab(self.note_panel)
        self.statusMessage.emit("노트에 넣었습니다: 백지 채점")

    # -- lifetime ---------------------------------------------------------------------

    def shutdown(self) -> None:
        self._closed = True
        self.cancel_capture()
        self.notes_panel.save_memo()
        self.note_panel.shutdown()
        for panel in self.review_panels.values():
            panel.flush()
        self.stop_gpt()
        self.stop_quiz()
        for kind in list(self._review_tasks):
            self.stop_review(kind)
        self.blank_panel.flush()
        self.stop_blank()
        if self._lookup_task is not None:
            self._lookup_task.cancel()


_SHORTENED = "OpenAI가 논문 전체를 거절해서(부록에 인용된 프롬프트 때문일 때가 많습니다) 참고문헌 앞까지만 보내 만들었습니다."


def _whole_paper(ask, run):  # type: ignore[no-untyped-def]
    """``run(request)`` over the whole paper, and a note for the reader.

    A grading prompt quoted in an appendix makes OpenAI refuse a paper: then
    it goes once more up to its references, and the note says so.
    """
    request = ask()
    if not request.numbering:
        raise TranslationError("이 PDF에서 정리할 본문 문장을 찾지 못했습니다. (스캔한 PDF에는 글자가 없습니다)")
    try:
        return run(request), ""
    except EnginePromptRejected:
        shorter = ask(until_references=True)
        if not shorter.numbering or len(shorter.text) >= len(request.text):
            raise
        return run(shorter), _SHORTENED


def _side_of(canvas: PageCanvas) -> str:
    """Which side of the paper a canvas shows, as highlights name it."""
    return "translated" if canvas.translated else "source"


def _other(side: str) -> str:
    return "right" if side == "left" else "left"


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def _style_examples(path: Path) -> tuple[str, int]:
    """A professor's earlier quiz as the examples a prompt carries, and how
    many questions were recognised in it."""
    pages = read_pages(path)
    examples = extract_quiz_examples(pages)
    return format_quiz_examples(examples, fallback="\n".join(pages)), len(examples)


def _plain(text: str) -> str:
    """Formula placeholders shown as a neutral mark in text-only places."""
    return re.sub("⟦m\\d+⟧", "[수식]", text or "")


def _converse(engine, prompt: str, session: str | None, images: list[Path], cancel):  # type: ignore[no-untyped-def]
    """One turn through ``engine``; an engine with only ``ask`` has no sessions."""
    if hasattr(engine, "converse"):
        return engine.converse(prompt, session=session, images=images, cancel=cancel)
    return engine.ask(prompt, images=images, cancel=cancel), None
