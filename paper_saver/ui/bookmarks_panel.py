"""The bookmarks tab: everything the reader highlighted, in reading order.

A highlight is a bookmark. Whole sentences (from the sentence menu or
Ctrl+H) show the English and its translation; strokes dragged with the
highlighter show what they cover. Any note on the same sentence is shown
right under it, so the list reads as "what I marked, and what I thought".

Clicking a card scrolls both panes to it; its menu adds or edits a note,
recolours or removes the highlight. This widget draws and reports - the
highlight changes go back through the reader view, which owns the canvases.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.project import HIGHLIGHT_COLORS, AnchoredNote, Highlights, Notes
from .notes_panel import edit_note_text

__all__ = ["BookmarksPanel"]


@dataclass
class _Bookmark:
    #: "sentence" (a whole highlighted sentence) or "stroke" (a dragged one).
    kind: str
    #: The sentence id for a sentence, the stroke id for a stroke.
    key: str
    page: int
    color: str
    text: str
    translation: str
    sentence_id: str
    sort: tuple


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


def _label(text: str, name: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setObjectName(name)
    # Clicks go through to the card, which jumps to the sentence.
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label


class _Card(QFrame):
    clicked = Signal()
    menuRequested = Signal(QPoint)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        self.menuRequested.emit(event.globalPos())


class BookmarksPanel(QWidget):
    #: Scroll to a bookmark: (sentence id or "", page).
    jumpRequested = Signal(str, int)
    #: Recolour: (kind, key, colour).
    recolorRequested = Signal(str, str, str)
    #: Remove the highlight: (kind, key).
    removeRequested = Signal(str, str)
    #: A note was added or edited from here.
    notesChanged = Signal()

    def __init__(
        self,
        highlights: Highlights,
        notes: Notes,
        order: dict[str, int],
        translation_of: Callable[[str], str | None],
        quote_of: Callable[[str], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.highlights = highlights
        self.notes = notes
        self._order = order
        self._translation_of = translation_of
        #: A sentence's English, which a note keeps as its quote.
        self._quote_of = quote_of
        #: The colour shown, or "" for all.
        self._filter = ""

        self.count = QLabel()
        self.count.setObjectName("muted")
        self.count.setWordWrap(True)
        filters = QHBoxLayout()
        filters.setSpacing(4)
        self._filter_buttons: dict[str, QPushButton] = {}
        everything = QPushButton("전체")
        everything.setCheckable(True)
        everything.clicked.connect(lambda: self.set_filter(""))
        self._filter_buttons[""] = everything
        filters.addWidget(everything)
        for name, color in HIGHLIGHT_COLORS.items():
            button = QPushButton()
            button.setIcon(_swatch(color))
            button.setToolTip(f"{name}만 보기")
            button.setCheckable(True)
            button.setFixedWidth(34)
            button.clicked.connect(lambda _on=False, c=color: self.set_filter(c))
            self._filter_buttons[color] = button
            filters.addWidget(button)
        filters.addStretch(1)

        self.cards = QVBoxLayout()
        self.cards.setSpacing(6)
        self.empty = QLabel(
            "아직 북마크가 없습니다.\n\n문장을 우클릭 → 형광펜, 문장 위에서 Ctrl+H, "
            "또는 툴바의 형광펜으로 드래그하면 여기에 모입니다."
        )
        self.empty.setWordWrap(True)
        self.empty.setObjectName("muted")

        title = QLabel("북마크")
        title.setObjectName("sectionTitle")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(title)
        layout.addLayout(filters)
        layout.addWidget(self.count)
        layout.addWidget(self.empty)
        layout.addLayout(self.cards)
        layout.addStretch(1)
        self.refresh()

    # -- contents ------------------------------------------------------------------

    def bookmarks(self) -> list[_Bookmark]:
        far = 1 << 30
        items: list[_Bookmark] = []
        for mark in self.highlights.items.values():
            items.append(
                _Bookmark(
                    "sentence", mark.sentence_id, mark.page, mark.color, mark.quote,
                    self._translation_of(mark.sentence_id) or "", mark.sentence_id,
                    (mark.page, self._order.get(mark.sentence_id, far), 0.0),
                )
            )
        for stroke in self.highlights.strokes:
            top = stroke.rects[0][1] if stroke.rects else 0.0
            items.append(
                _Bookmark(
                    "stroke", stroke.id, stroke.page, stroke.color, stroke.text, "", stroke.sentence_id,
                    (stroke.page, self._order.get(stroke.sentence_id, far), top),
                )
            )
        return sorted(items, key=lambda b: b.sort)

    def set_filter(self, color: str) -> None:
        self._filter = color
        self.refresh()

    def refresh(self) -> None:
        while self.cards.count():
            widget = self.cards.takeAt(0).widget()
            if widget is not None:
                # Gone at once, not at the next event loop turn, so a list
                # rebuilt twice in a row never shows both.
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        everything = self.bookmarks()
        shown = [b for b in everything if not self._filter or b.color == self._filter]
        for color, button in self._filter_buttons.items():
            button.setChecked(color == self._filter)
        for bookmark in shown:
            self.cards.addWidget(self._card(bookmark))
        self.empty.setVisible(not everything)
        with_notes = sum(1 for b in shown if b.sentence_id and self.notes.for_sentence(b.sentence_id))
        self.count.setText(
            f"{len(shown)}개 북마크 · 메모 {with_notes}개 · 클릭하면 이동, 우클릭하면 메모·색 변경" if everything else ""
        )

    def _card(self, bookmark: _Bookmark) -> _Card:
        card = _Card()
        card.setObjectName("bookmarkCard")
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setStyleSheet(f"QFrame#bookmarkCard {{ border-left: 4px solid {bookmark.color}; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 6, 8, 8)
        layout.setSpacing(3)

        where = f"p.{bookmark.page + 1}" + (" · 드래그" if bookmark.kind == "stroke" else "")
        layout.addWidget(_label(where, "bookmarkPage"))
        layout.addWidget(_label(" ".join(bookmark.text.split()), "bookmarkText"))
        if bookmark.translation:
            layout.addWidget(_label(" ".join(bookmark.translation.split()), "bookmarkTranslation"))
        for note in self._notes_of(bookmark):
            layout.addWidget(_label(note.text, "bookmarkNote"))

        card.clicked.connect(lambda b=bookmark: self.jumpRequested.emit(b.sentence_id, b.page))
        card.menuRequested.connect(lambda pos, b=bookmark: self._menu(b, pos))
        return card

    def _notes_of(self, bookmark: _Bookmark) -> list[AnchoredNote]:
        if not bookmark.sentence_id:
            return []
        return sorted(self.notes.for_sentence(bookmark.sentence_id), key=lambda n: n.created)

    # -- the card's menu ---------------------------------------------------------------

    def _menu(self, bookmark: _Bookmark, pos: QPoint) -> None:
        menu = QMenu(self)
        add = menu.addAction("메모 추가…", lambda: self.add_note(bookmark))
        add.setEnabled(bool(bookmark.sentence_id))
        notes = self._notes_of(bookmark)
        for note in notes:
            preview = note.text if len(note.text) <= 24 else note.text[:24] + "…"
            menu.addAction(f"메모 수정: {preview}", lambda n=note: self._edit_note(n))
        for note in notes:
            preview = note.text if len(note.text) <= 24 else note.text[:24] + "…"
            menu.addAction(f"메모 삭제: {preview}", lambda n=note: self._remove_note(n))
        menu.addSeparator()
        colors = menu.addMenu("색 바꾸기")
        for name, color in HIGHLIGHT_COLORS.items():
            action = colors.addAction(
                _swatch(color), name, lambda c=color: self.recolorRequested.emit(bookmark.kind, bookmark.key, c)
            )
            action.setCheckable(True)
            action.setChecked(color == bookmark.color)
        menu.addAction("북마크(형광펜) 지우기", lambda: self.removeRequested.emit(bookmark.kind, bookmark.key))
        menu.addSeparator()
        menu.addAction("텍스트 복사", lambda: QGuiApplication.clipboard().setText(bookmark.text))
        menu.exec(pos)

    def add_note(self, bookmark: _Bookmark) -> None:
        if not bookmark.sentence_id:
            return
        text = edit_note_text(self, "북마크에 메모", bookmark.text)
        if text:
            self.notes.add(bookmark.sentence_id, bookmark.page, text, self._quote_of(bookmark.sentence_id))
            self.notesChanged.emit()

    def _edit_note(self, note: AnchoredNote) -> None:
        text = edit_note_text(self, "메모 수정", note.quote, note.text)
        if text:
            self.notes.edit(note.id, text)
            self.notesChanged.emit()

    def _remove_note(self, note: AnchoredNote) -> None:
        self.notes.remove(note.id)
        self.notesChanged.emit()
