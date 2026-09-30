"""The wordbook tab: starred words with the sentence each came from (§3.3)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.atomic_io import write_text_atomic
from ..core.project import Wordbook

__all__ = ["WordbookPanel"]


class WordbookPanel(QWidget):
    jumpRequested = Signal(str)
    changed = Signal()

    def __init__(self, wordbook: Wordbook, exports_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.wordbook = wordbook
        self.exports_dir = exports_dir

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["단어", "이 문장에서의 뜻", "쪽"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.cellDoubleClicked.connect(self._jump)

        self.count = QLabel()
        self.count.setObjectName("muted")
        remove = QPushButton("삭제")
        remove.clicked.connect(self._remove)
        export = QPushButton("TSV 내보내기 (Anki)")
        export.clicked.connect(self._export)
        buttons = QHBoxLayout()
        buttons.addWidget(self.count, 1)
        buttons.addWidget(remove)
        buttons.addWidget(export)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.table.setRowCount(len(self.wordbook.entries))
        for row, entry in enumerate(self.wordbook.entries):
            word = QTableWidgetItem(entry.lemma or entry.word)
            word.setData(Qt.ItemDataRole.UserRole, entry.sentence_id)
            word.setToolTip(entry.sentence)
            meaning = QTableWidgetItem(entry.in_context)
            meaning.setToolTip("\n".join(entry.senses) + (f"\n\n{entry.sentence}" if entry.sentence else ""))
            page = QTableWidgetItem(str(entry.page + 1))
            self.table.setItem(row, 0, word)
            self.table.setItem(row, 1, meaning)
            self.table.setItem(row, 2, page)
        self.table.resizeRowsToContents()
        self.count.setText(f"{len(self.wordbook.entries)}개 단어 · 더블클릭하면 그 문장으로 이동")

    def _jump(self, row: int, _column: int) -> None:
        item = self.table.item(row, 0)
        if item and item.data(Qt.ItemDataRole.UserRole):
            self.jumpRequested.emit(item.data(Qt.ItemDataRole.UserRole))

    def _remove(self) -> None:
        row = self.table.currentRow()
        if 0 <= row < len(self.wordbook.entries):
            self.wordbook.remove(self.wordbook.entries[row].word)
            self.refresh()
            self.changed.emit()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "단어장 내보내기", str(self.exports_dir / "wordbook.tsv"), "TSV (*.tsv)"
        )
        if path:
            write_text_atomic(Path(path), self.wordbook.to_tsv())
