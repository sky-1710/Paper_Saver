"""The reading screen: original | translation | dictionary · notes · words.

The two panes scroll as one and highlight as one (§1.3, §2.3). Pointing near a
sentence on either side lights the sentence and its counterpart, and the strip
along the bottom shows the pair in full - useful where the Korean had to be set
small to fit its rectangle.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPixmap,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QInputDialog,
    QLabel,
    QMenu,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..core.geometry import Rect
from ..core.project import HIGHLIGHT_COLORS, WordEntry
from ..translate.dictionary import Dictionary, clean_word
from ..translate.engine import TranslationEngine
from .dictionary_panel import DictionaryPanel
from .notes_panel import NotesPanel
from .page_canvas import MARGIN, MarginNote, PageCanvas, RangeMark
from .session import PaperSession
from .wordbook_panel import WordbookPanel
from .workers import BackgroundTask

__all__ = ["ReaderView"]


NOTE_KEY = "Ctrl+Shift+M" if sys.platform == "darwin" else "Ctrl+M"
HIGHLIGHT_KEY = "Ctrl+Shift+H" if sys.platform == "darwin" else "Ctrl+H"
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


def _pane(title: str, canvas: PageCanvas) -> tuple[QWidget, _Scroll]:
    scroll = _Scroll()
    scroll.setWidget(canvas)
    scroll.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    label = QLabel(title)
    label.setObjectName("paneTitle")
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addWidget(label)
    layout.addWidget(scroll, 1)
    return box, scroll


class ReaderView(QWidget):
    statusMessage = Signal(str)
    pageChanged = Signal(int)

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

        project = session.project
        self.left = PageCanvas(session, translated=False)
        self.right = PageCanvas(session, translated=True)
        left_box, self.left_scroll = _pane("원문", self.left)
        right_box, self.right_scroll = _pane("번역 (직역)", self.right)

        order = {s.id: i for i, (_, _, s) in enumerate(session.layout.sentences())}
        self.dictionary_panel = DictionaryPanel()
        self.notes_panel = NotesPanel(project.notes, order)
        self.wordbook_panel = WordbookPanel(project.wordbook, project.exports_dir)
        self.side = QTabWidget()
        self.side.addTab(self.dictionary_panel, "사전")
        self.side.addTab(self.notes_panel, "메모")
        self.side.addTab(self.wordbook_panel, "단어장")
        self.side.setMinimumWidth(260)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left_box)
        self.splitter.addWidget(right_box)
        self.splitter.addWidget(self.side)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 5)
        self.splitter.setStretchFactor(2, 3)
        self.splitter.setChildrenCollapsible(True)

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
        layout.addWidget(self.splitter, 1)
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
        self.left.rangeHighlighted.connect(lambda page, rects, text: self._add_stroke("source", page, rects, text))
        self.right.rangeHighlighted.connect(lambda page, rects, text: self._add_stroke("translated", page, rects, text))
        for scroll in (self.left_scroll, self.right_scroll):
            scroll.zoomStep.connect(self.zoom_step)
        ls, rs = self.left_scroll, self.right_scroll
        ls.verticalScrollBar().valueChanged.connect(lambda v: self._sync(rs.verticalScrollBar(), v))
        rs.verticalScrollBar().valueChanged.connect(lambda v: self._sync(ls.verticalScrollBar(), v))
        ls.horizontalScrollBar().valueChanged.connect(lambda v: self._sync(rs.horizontalScrollBar(), v))
        rs.horizontalScrollBar().valueChanged.connect(lambda v: self._sync(ls.horizontalScrollBar(), v))
        ls.verticalScrollBar().valueChanged.connect(self._track_page)

        self.dictionary_panel.lookupRequested.connect(
            lambda word, sentence: self.lookup(word, sentence, self.dictionary_panel.sentence_id, self.dictionary_panel.page)
        )
        self.dictionary_panel.starToggled.connect(self._on_star)
        self.notes_panel.jumpRequested.connect(self.jump_to_sentence)
        self.notes_panel.notesChanged.connect(self._apply_notes)
        self.wordbook_panel.jumpRequested.connect(self.jump_to_sentence)

        # Qt maps Ctrl to Cmd on a Mac, where Cmd+M minimises and Cmd+H hides
        # the application before the window ever sees the key - so the Mac
        # gets Shift added. See NOTE_KEY / HIGHLIGHT_KEY.
        QShortcut(QKeySequence(NOTE_KEY), self, activated=self._note_on_hover)
        QShortcut(QKeySequence(HIGHLIGHT_KEY), self, activated=self._highlight_on_hover)
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

    def fit_width(self) -> None:
        widest = max((self.session.page_size(n)[0] for n in range(self.session.page_count)), default=612)
        available = self.left_scroll.viewport().width() - MARGIN * 2 - 4
        self.set_zoom(max(0.3, available / widest))

    def zoom_step(self, direction: int) -> None:
        self.set_zoom(self.left.zoom * (1.12 if direction > 0 else 1 / 1.12))

    def set_zoom(self, zoom: float) -> None:
        bar = self.left_scroll.verticalScrollBar()
        ratio = bar.value() / max(1, bar.maximum())
        self.left.set_zoom(zoom)
        self.right.set_zoom(zoom)
        QTimer.singleShot(0, lambda: bar.setValue(int(ratio * bar.maximum())))
        self.settings.set("zoom", round(self.left.zoom, 3))
        self.statusMessage.emit(f"확대 {self.left.zoom * 100:.0f}%")

    def goto_page(self, number: int) -> None:
        number = max(0, min(self.session.page_count - 1, number))
        self.left_scroll.verticalScrollBar().setValue(self.left.page_top(number))

    def _track_page(self, value: int) -> None:
        probe = value + self.left_scroll.viewport().height() / 3
        page = self.left.page_at_y(probe)
        if page != self._page:
            self._page = page
            self.pageChanged.emit(page)

    @property
    def current_page(self) -> int:
        return self._page

    def jump_to_sentence(self, sentence_id: str) -> None:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        rects = ref.sentence.rects(ref.block)
        top = rects[0].y0 if rects else ref.block.rect.y0
        y = self.left.point_y(ref.page, top) - self.left_scroll.viewport().height() // 3
        self.left_scroll.verticalScrollBar().setValue(max(0, y))
        for canvas in (self.left, self.right):
            canvas.set_focus_sentence(sentence_id)
        QTimer.singleShot(2500, lambda: [c.set_focus_sentence("") for c in (self.left, self.right)])

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

    # -- sentence menu and notes (§4) -------------------------------------------------

    def _on_menu(self, sentence_id: str, pos: QPoint) -> None:
        ref = self.session.sentence(sentence_id)
        if ref is None:
            return
        translation = self.session.translation(sentence_id) or ""
        menu = QMenu(self)
        menu.addAction("메모 추가…", lambda: self.add_note(sentence_id))
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
        for canvas, side in ((self.left, "source"), (self.right, "translated")):
            canvas.set_range_marks([
                RangeMark(s.id, s.page, tuple(Rect.from_list(r) for r in s.rects), s.color)
                for s in marks.strokes
                if s.side == side
            ])

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
            side, page, [r.as_list() for r in rects], _plain(text), pen
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

    # -- lifetime ---------------------------------------------------------------------

    def shutdown(self) -> None:
        self.notes_panel.save_memo()
        if self._lookup_task is not None:
            self._lookup_task.cancel()


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def _plain(text: str) -> str:
    """Formula placeholders shown as a neutral mark in text-only places."""
    return re.sub("⟦m\\d+⟧", "[수식]", text or "")
