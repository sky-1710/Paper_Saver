"""The notes tab: a free memo and the sentence-anchored notes (§4).

The memo saves itself 0.7 s after the last keystroke and again when the
paper closes (§4.3) - there is no Save button because there
is nothing to forget.

Anchored notes are listed in reading order with the sentence they belong to,
so the list doubles as a table of contents of the reader's own thinking.
Clicking one scrolls both panes to its sentence.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.project import AnchoredNote, Notes
from .live_markdown import LiveMarkdownEdit

__all__ = ["NotesPanel", "edit_note_text"]


def edit_note_text(parent: QWidget, title: str, quote: str, text: str = "") -> str | None:
    """A small multi-line prompt; ``None`` when cancelled."""
    label = f"「{quote[:140]}{'…' if len(quote) > 140 else ''}」" if quote else ""
    value, ok = QInputDialog.getMultiLineText(parent, title, label, text)
    return value.strip() if ok else None


class NotesPanel(QWidget):
    #: Jump to a sentence: sentence id.
    jumpRequested = Signal(str)
    #: The anchored notes changed (added, edited, removed).
    notesChanged = Signal()

    def __init__(self, notes: Notes, order: dict[str, int], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.notes = notes
        #: sentence id -> position in reading order, for sorting the list.
        self._order = order

        self.memo = LiveMarkdownEdit()
        self.memo.setPlaceholderText(
            "자유 메모. # 제목, **굵게**, - 목록처럼 Markdown으로 적으면 바로 서식이 됩니다.\n자동 저장됩니다."
        )
        self.memo.setPlainText(notes.memo)
        self._timer = QTimer(self, singleShot=True, interval=700)
        self._timer.timeout.connect(self.save_memo)
        self.memo.edited.connect(self._timer.start)
        self.saved = QLabel()
        self.saved.setObjectName("muted")

        memo_box = QWidget()
        memo_layout = QVBoxLayout(memo_box)
        memo_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("메모장")
        title.setObjectName("sectionTitle")
        memo_layout.addWidget(title)
        memo_layout.addWidget(self.memo, 1)
        memo_layout.addWidget(self.saved)

        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setAlternatingRowColors(True)
        self.list.itemClicked.connect(self._jump)
        self.list.itemDoubleClicked.connect(self._edit_item)
        edit = QPushButton("수정")
        edit.clicked.connect(lambda: self._edit_item(self.list.currentItem()))
        remove = QPushButton("삭제")
        remove.clicked.connect(self._remove)
        buttons = QHBoxLayout()
        hint = QLabel("문장 우클릭 → 메모 추가 (" + ("⌘⇧M" if sys.platform == "darwin" else "Ctrl+M") + ")")
        hint.setObjectName("muted")
        buttons.addWidget(hint, 1)
        buttons.addWidget(edit)
        buttons.addWidget(remove)

        list_box = QWidget()
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_title = QLabel("문장 메모")
        list_title.setObjectName("sectionTitle")
        list_layout.addWidget(list_title)
        list_layout.addWidget(self.list, 1)
        list_layout.addLayout(buttons)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(list_box)
        splitter.addWidget(memo_box)
        splitter.setSizes([300, 300])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)
        self.refresh()

    # -- memo ----------------------------------------------------------------------

    def save_memo(self) -> None:
        self._timer.stop()
        self.notes.set_memo(self.memo.toPlainText())
        self.saved.setText("저장됨")
        QTimer.singleShot(1500, self.saved.clear)

    # -- anchored ------------------------------------------------------------------

    def refresh(self) -> None:
        self.list.clear()
        ordered = sorted(self.notes.anchored, key=lambda n: (self._order.get(n.sentence_id, 1 << 30), n.created))
        for note in ordered:
            quote = note.quote if len(note.quote) <= 90 else note.quote[:90] + "…"
            item = QListWidgetItem(f"p.{note.page + 1}  {note.text}\n    「{quote}」")
            item.setData(Qt.ItemDataRole.UserRole, note.id)
            item.setToolTip(note.quote)
            self.list.addItem(item)

    def _note(self, item: QListWidgetItem | None) -> AnchoredNote | None:
        if item is None:
            return None
        note_id = item.data(Qt.ItemDataRole.UserRole)
        return next((n for n in self.notes.anchored if n.id == note_id), None)

    def _jump(self, item: QListWidgetItem) -> None:
        note = self._note(item)
        if note is not None:
            self.jumpRequested.emit(note.sentence_id)

    def _edit_item(self, item: QListWidgetItem | None) -> None:
        note = self._note(item)
        if note is None:
            return
        text = edit_note_text(self, "메모 수정", note.quote, note.text)
        if text:
            self.notes.edit(note.id, text)
            self.refresh()
            self.notesChanged.emit()

    def edit_by_id(self, note_id: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == note_id:
                self.list.setCurrentItem(item)
                self._edit_item(item)
                return

    def _remove(self) -> None:
        note = self._note(self.list.currentItem())
        if note is not None:
            self.notes.remove(note.id)
            self.refresh()
            self.notesChanged.emit()

    def add_for(self, sentence_id: str, page: int, quote: str) -> None:
        text = edit_note_text(self, "문장 메모 추가", quote)
        if text:
            self.notes.add(sentence_id, page, text, quote)
            self.refresh()
            self.notesChanged.emit()
