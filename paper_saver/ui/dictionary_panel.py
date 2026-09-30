"""The dictionary tab: a clicked word's meaning in its sentence (§3.1).

Shows the cached entry instantly when there is one; otherwise shows the same
word's entry from another sentence (marked as such) while the contextual
lookup runs, or a plain "찾는 중" when the word is new. The star adds the entry
to the paper's wordbook with this sentence as its example (§3.3).
"""

from __future__ import annotations

import html
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DictionaryPanel"]


class DictionaryPanel(QWidget):
    #: The reader asked for a lookup: word, sentence text.
    lookupRequested = Signal(str, str)
    #: Star toggled on the entry shown: the entry, starred?
    starToggled = Signal(dict, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entry: dict[str, Any] | None = None
        self._sentence = ""
        self._sentence_id = ""
        self._page = 0

        self.search = QLineEdit(placeholderText="단어 입력 후 Enter (논문에서 단어를 클릭해도 됩니다)")
        self.search.returnPressed.connect(self._search)
        self.star = QPushButton("☆ 단어장")
        self.star.setCheckable(True)
        self.star.setEnabled(False)
        self.star.toggled.connect(self._star)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.star)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.status = QLabel()
        self.status.setObjectName("muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.status)
        self.show_welcome()

    # -- context of the current word --------------------------------------------

    @property
    def sentence(self) -> str:
        return self._sentence

    @property
    def sentence_id(self) -> str:
        return self._sentence_id

    @property
    def page(self) -> int:
        return self._page

    def set_context(self, word: str, sentence: str, sentence_id: str, page: int) -> None:
        self._sentence, self._sentence_id, self._page = sentence, sentence_id, page
        self.search.setText(word)

    def _search(self) -> None:
        word = self.search.text().strip()
        if word:
            self.lookupRequested.emit(word, self._sentence)

    # -- what is shown -----------------------------------------------------------

    def show_welcome(self) -> None:
        self._entry = None
        self.star.setEnabled(False)
        self.view.setHtml(
            "<p style='color:#666'>왼쪽 원문에서 모르는 단어를 클릭하면<br>"
            "그 문장 안에서의 뜻을 바로 보여줍니다.</p>"
            "<p style='color:#888;font-size:90%'>한 번 찾은 단어는 저장되어 다음부터 즉시 나타납니다.</p>"
        )
        self.status.clear()

    def show_loading(self, word: str, related: dict[str, Any] | None = None) -> None:
        self.star.setEnabled(False)
        body = f"<h2 style='margin:0'>{html.escape(word)}</h2>"
        if related:
            body += "<p style='color:#a60'>다른 문장에서 찾았던 뜻 (이 문장 기준 뜻을 찾는 중…)</p>"
            body += self._entry_html(related, heading=False)
        else:
            body += "<p style='color:#666'>이 문장에서의 뜻을 찾는 중…</p>"
        body += self._sentence_html(word)
        self.view.setHtml(body)
        self.status.setText("GPT에 묻는 중…")

    def show_entry(self, entry: dict[str, Any], *, starred: bool, cached: bool) -> None:
        self._entry = entry
        self.star.setEnabled(True)
        self.star.blockSignals(True)
        self.star.setChecked(starred)
        self.star.setText("★ 단어장" if starred else "☆ 단어장")
        self.star.blockSignals(False)
        self.view.setHtml(self._entry_html(entry) + self._sentence_html(entry.get("word", "")))
        self.status.setText("저장된 뜻" if cached else "")

    def show_error(self, word: str, message: str) -> None:
        self.star.setEnabled(False)
        self.view.setHtml(
            f"<h2 style='margin:0'>{html.escape(word)}</h2>"
            f"<p style='color:#b00'>{html.escape(message).replace(chr(10), '<br>')}</p>"
        )
        self.status.clear()

    def _entry_html(self, entry: dict[str, Any], *, heading: bool = True) -> str:
        word = html.escape(str(entry.get("word", "")))
        lemma = html.escape(str(entry.get("lemma", "")))
        parts: list[str] = []
        if heading:
            title = word if not lemma or lemma.lower() == word.lower() else f"{word} <span style='color:#888;font-size:70%'>← {lemma}</span>"
            parts.append(f"<h2 style='margin:0 0 2px 0'>{title}</h2>")
        meta = " · ".join(html.escape(str(v)) for v in (entry.get("pos"), entry.get("ipa")) if v)
        if meta:
            parts.append(f"<div style='color:#777'>{meta}</div>")
        parts.append(
            "<div style='margin:10px 0;padding:8px 10px;background:#fff6d6;border-radius:6px'>"
            "<b>이 문장에서</b><br>"
            f"<span style='font-size:115%'>{html.escape(str(entry.get('in_context', '')))}</span></div>"
        )
        senses = entry.get("senses") or []
        if senses:
            items = "".join(f"<li>{html.escape(str(s))}</li>" for s in senses)
            parts.append(f"<b>일반적인 뜻</b><ol style='margin-top:2px'>{items}</ol>")
        if entry.get("note"):
            parts.append(f"<p style='color:#555'>💡 {html.escape(str(entry['note']))}</p>")
        return "".join(parts)

    def _sentence_html(self, word: str) -> str:
        if not self._sentence:
            return ""
        text = html.escape(self._sentence)
        if word:
            marked = html.escape(word)
            text = text.replace(marked, f"<b style='background:#ffe58a'>{marked}</b>", 1)
        return f"<hr><div style='color:#555;font-size:92%'>{text}</div>"

    def _star(self, on: bool) -> None:
        self.star.setText("★ 단어장" if on else "☆ 단어장")
        if self._entry is not None:
            self.starToggled.emit(self._entry, on)


