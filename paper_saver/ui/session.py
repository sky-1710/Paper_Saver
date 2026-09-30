"""An open paper, as the reader view sees it: pages, sentences, pixels.

Both panes draw from one session, which is how they stay in step: the same
page sizes, the same sentence ids, the same idea of which pages have changed.
Images are rendered on demand and kept in a small LRU cache per pane, at the
zoom they were asked for - scrolling back to a page is a blit, and a paper of
any length costs a dozen page images of memory, not a hundred.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRect, Signal
from PySide6.QtGui import QImage

from ..core.geometry import Rect
from ..core.models import Block, PaperLayout, Sentence, Word
from ..core.project import Project
from ..pdf.document import PaperDocument
from ..render.fonts import FontBook
from ..render.page import TranslatedPage, layout_page, paint_page, rendered_to_qimage

log = logging.getLogger(__name__)

__all__ = ["PaperSession", "SentenceRef", "WordHit"]

CACHE_PAGES = 14
#: Captures are rendered at up to 3x (216 dpi), and no longer than this on
#: their long side - sharp enough to read a subscript, small enough to upload.
CAPTURE_SCALE = 3.0
CAPTURE_MAX_PIXELS = 2400


@dataclass(frozen=True, slots=True)
class SentenceRef:
    page: int
    block: Block
    sentence: Sentence


@dataclass(frozen=True, slots=True)
class WordHit:
    page: int
    text: str
    rect: Rect
    sentence_id: str
    sentence_text: str


class _Lru(OrderedDict):  # type: ignore[type-arg]
    def get_touch(self, key: object) -> QImage | None:
        value = self.get(key)
        if value is not None:
            self.move_to_end(key)
        return value

    def put(self, key: object, value: QImage) -> None:
        self[key] = value
        self.move_to_end(key)
        while len(self) > CACHE_PAGES:
            self.popitem(last=False)


class PaperSession(QObject):
    #: A page's translation changed (-1: possibly all pages).
    pageTranslated = Signal(int)

    def __init__(
        self, project: Project, document: PaperDocument, layout: PaperLayout, fonts: FontBook
    ) -> None:
        super().__init__()
        self.project = project
        self.document = document
        self.layout = layout
        self.fonts = fonts
        self._originals = _Lru()
        self._composed = _Lru()
        self._translated: dict[int, TranslatedPage] = {}
        self._refs: dict[str, SentenceRef] = {}
        for page in layout.pages:
            for block in page.blocks:
                for sentence in block.sentences:
                    self._refs[sentence.id] = SentenceRef(page.number, block, sentence)
        self._source_rects: dict[int, list[tuple[str, Rect]]] = {}
        self._tokens: dict[tuple[int, bool], list[tuple[Rect, str]]] = {}

    def close(self) -> None:
        self.document.close()

    # -- geometry ---------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return self.layout.page_count

    def page_size(self, number: int) -> tuple[float, float]:
        page = self.layout.pages[number]
        return page.width, page.height

    def sentence(self, sentence_id: str) -> SentenceRef | None:
        return self._refs.get(sentence_id)

    def translation(self, sentence_id: str) -> str | None:
        return self.project.translations.get(sentence_id)

    # -- sentences on each side -------------------------------------------------

    def source_rects(self, number: int) -> list[tuple[str, Rect]]:
        cached = self._source_rects.get(number)
        if cached is None:
            cached = []
            for block in self.layout.pages[number].blocks:
                if not block.translatable:
                    continue
                for sentence in block.sentences:
                    for rect in sentence.rects(block):
                        cached.append((sentence.id, rect))
            self._source_rects[number] = cached
        return cached

    def translated_rects(self, number: int) -> list[tuple[str, Rect]]:
        page = self.translated_page(number)
        return [(sid, rect) for sid, rects in page.sentence_rects.items() for rect in rects]

    def rects_of(self, sentence_id: str, *, translated: bool) -> list[Rect]:
        ref = self._refs.get(sentence_id)
        if ref is None:
            return []
        if translated:
            return list(self.translated_page(ref.page).sentence_rects.get(sentence_id, []))
        return ref.sentence.rects(ref.block)

    def word_at(self, number: int, x: float, y: float) -> WordHit | None:
        """The source word under a point, for the dictionary (§3.1)."""
        for block in self.layout.pages[number].blocks:
            if not block.rect.inflated(2).contains(x, y):
                continue
            for index, word in enumerate(block.words):
                if word.math:
                    continue
                if any(rect.inflated(0.6).contains(x, y) for rect in word.rects):
                    return self._hit(number, block, index, word)
        return None

    def _hit(self, number: int, block: Block, index: int, word: Word) -> WordHit:
        sentence = block.sentence_of_word(index)
        text = sentence.text if sentence else block.text
        return WordHit(number, word.text, word.rect, sentence.id if sentence else "", text)

    def page_tokens(self, number: int, *, translated: bool) -> list[tuple[Rect, str]]:
        """Every piece of text on one side of a page, in reading order.

        What the highlighter snaps to: words (and formulas) on the original,
        the laid-out Korean pieces on the translation. Reading order matters -
        a stroke covers everything *between* where it started and ended, and
        "between" has to mean what a reader means by it.
        """
        key = (number, translated)
        cached = self._tokens.get(key)
        if cached is not None:
            return cached
        tokens: list[tuple[Rect, str]] = []
        page = self.layout.pages[number]
        if translated:
            laid_out = self.translated_page(number).layouts
            for block in page.blocks:
                laid = laid_out.get(block.id)
                if laid is None:
                    continue
                for line in laid.lines:
                    top, bottom = line.baseline - laid.size * 0.88, line.baseline + laid.size * 0.12
                    for piece in line.pieces:
                        text = piece.text or "[수식]"
                        tokens.append((Rect(piece.x, top, piece.x + piece.width, bottom), text))
        else:
            for block in page.blocks:
                for word in block.words:
                    text = "[수식]" if word.math else word.text
                    # A word hyphenated across lines has two boxes but is one
                    # word: the text goes with the first box only.
                    for index, rect in enumerate(word.rects):
                        tokens.append((rect, text if index == 0 else ""))
        self._tokens[key] = tokens
        return tokens

    def translated_piece_at(self, number: int, x: float, y: float) -> tuple[str, str] | None:
        """``(text, sentence id)`` of the Korean piece under a point."""
        page = self.translated_page(number)
        for laid in page.layouts.values():
            if not laid.rect.inflated(2).contains(x, y):
                continue
            for line in laid.lines:
                top, bottom = line.baseline - laid.size * 0.9, line.baseline + laid.size * 0.2
                if not top <= y <= bottom:
                    continue
                for piece in line.pieces:
                    if piece.text and piece.x <= x <= piece.x + piece.width:
                        return piece.text, piece.sentence
        return None

    # -- the translated side ----------------------------------------------------

    def translated_page(self, number: int) -> TranslatedPage:
        cached = self._translated.get(number)
        if cached is None:
            cached = layout_page(
                self.layout.pages[number], self.project.translations.snapshot(), self.fonts
            )
            self._translated[number] = cached
        return cached

    def invalidate(self, number: int = -1) -> None:
        """Forget the translated side of a page (or of all pages)."""
        if number < 0:
            self._translated.clear()
            self._composed.clear()
            self._tokens = {k: v for k, v in self._tokens.items() if not k[1]}
        else:
            self._translated.pop(number, None)
            self._tokens.pop((number, True), None)
            for key in [k for k in self._composed if k[0] == number]:
                del self._composed[key]
        self.pageTranslated.emit(number)

    # -- pixels -----------------------------------------------------------------

    def original_image(self, number: int, scale: float) -> QImage:
        key = (number, scale)
        image = self._originals.get_touch(key)
        if image is None:
            rendered = self.document.render(number, scale)
            image = rendered_to_qimage(
                rendered.samples, rendered.width, rendered.height, rendered.stride
            )
            self._originals.put(key, image)
        return image

    def translated_image(self, number: int, scale: float) -> QImage:
        key = (number, scale)
        image = self._composed.get_touch(key)
        if image is None:
            original = self.original_image(number, scale)
            page = self.translated_page(number)
            try:
                image = paint_page(original, self.layout.pages[number], page, self.fonts, scale)
            except Exception:
                log.exception("Could not paint translated page %s", number + 1)
                image = original
            self._composed.put(key, image)
        return image

    # -- captures ---------------------------------------------------------------

    def capture(self, number: int, rect: Rect, *, translated: bool) -> QImage:
        """A region of one page as a sharp image, for a question or a note.

        Rendered afresh at a fixed resolution rather than cut out of the
        screen, so a capture taken zoomed out still shows small print and
        subscripts legibly - which is what GPT and the note both need. Not
        cached: a capture is taken once.
        """
        width, height = self.page_size(number)
        rect = Rect(max(0.0, rect.x0), max(0.0, rect.y0), min(width, rect.x1), min(height, rect.y1))
        longest = max(rect.width, rect.height, 1.0)
        scale = max(1.5, min(CAPTURE_SCALE, CAPTURE_MAX_PIXELS / longest))
        rendered = self.document.render(number, scale)
        image = rendered_to_qimage(rendered.samples, rendered.width, rendered.height, rendered.stride)
        if translated:
            try:
                image = paint_page(image, self.layout.pages[number], self.translated_page(number), self.fonts, scale)
            except Exception:
                log.exception("Could not paint translated page %s for a capture", number + 1)
        box = QRect(int(rect.x0 * scale), int(rect.y0 * scale), max(1, int(rect.width * scale)), max(1, int(rect.height * scale)))
        return image.copy(box.intersected(image.rect()))
