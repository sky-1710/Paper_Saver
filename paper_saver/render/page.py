"""The translated page: the original page, with only its paragraphs repainted (§1.2).

Two steps, kept apart because the UI needs the first without the second:

:func:`layout_page`
    Sets every translated paragraph into its rectangle and records where each
    sentence landed. The right pane's hover and click work from this alone.
:func:`paint_page`
    Paints the result over a render of the original page. Everything the
    layout did not touch - figures, tables, equations, author lines, the
    reference list, rules, page furniture - is the original's own pixels at the
    original's own coordinates. Nothing else in the application can make "the
    figure is in exactly the same place" true; this makes it true by never
    moving the figure in the first place.

The painting order is what keeps figures safe even when a paragraph's
rectangle brushes against one: every translated paragraph is blanked first,
then every region that must not change is copied back from the original on top
of the blanking, and only then is Korean drawn.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from ..core.geometry import Rect
from ..core.models import MATH_TOKEN_END, MATH_TOKEN_START, Block, PageLayout
from .fit import BlockLayout, fit_block
from .fonts import DPI72_DOTS_PER_METER, FontBook

log = logging.getLogger(__name__)

__all__ = ["TranslatedPage", "layout_page", "paint_page", "rendered_to_qimage"]

_TOKEN_RE = re.compile(f"{re.escape(MATH_TOKEN_START)}m\\d+{re.escape(MATH_TOKEN_END)}")


@dataclass
class TranslatedPage:
    number: int
    #: Block id -> how its translation was set.
    layouts: dict[str, BlockLayout] = field(default_factory=dict)
    #: Sentence id -> the strips it occupies on the translated page.
    sentence_rects: dict[str, list[Rect]] = field(default_factory=dict)
    translated: int = 0
    total: int = 0

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.translated >= self.total

    @property
    def any(self) -> bool:
        return self.translated > 0


def _restore_tokens(source: str, translated: str) -> str:
    """Put back any formula the translation dropped.

    GPT keeps placeholders almost always; when it does not, the formula would
    silently vanish from the page. Appending it at the end of the sentence is
    not where the author put it, but it keeps the mathematics on the page,
    which matters more.
    """
    missing = [token for token in _TOKEN_RE.findall(source) if token not in translated]
    return translated if not missing else f"{translated} {' '.join(missing)}"


def _block_sentences(
    block: Block, translations: Mapping[str, str], *, fallback_to_source: bool
) -> tuple[list[tuple[str, str]], int]:
    pairs: list[tuple[str, str]] = []
    done = 0
    for sentence in block.sentences:
        text = translations.get(sentence.id)
        if text:
            pairs.append((sentence.id, _restore_tokens(sentence.text, text)))
            done += 1
        elif fallback_to_source:
            pairs.append((sentence.id, sentence.text))
    return pairs, done


def layout_page(
    page: PageLayout,
    translations: Mapping[str, str],
    fonts: FontBook,
    *,
    fallback_to_source: bool = True,
) -> TranslatedPage:
    """Set every translated paragraph of ``page``.

    A paragraph none of whose sentences is translated yet is left out, so it
    keeps its original pixels. A paragraph with *some* sentences translated is
    set with the original English standing in for the rest - a half-translated
    paragraph reads better than one with holes, and it fills in as the rest of
    the translation arrives.
    """
    result = TranslatedPage(number=page.number)
    for block in page.blocks:
        if not block.translatable:
            continue
        result.total += len(block.sentences)
        pairs, done = _block_sentences(
            block, translations, fallback_to_source=fallback_to_source
        )
        if not done:
            continue
        result.translated += done
        metrics = fonts.metrics(serif=block.serif, bold=block.bold)
        try:
            laid = fit_block(
                block.rect,
                pairs,
                metrics,
                font_size=block.font_size,
                leading=block.leading,
                indent=block.indent,
                align=block.align,
                math_rects=block.math,
                math_baselines=block.math_baseline,
            )
        except Exception:
            log.exception("Could not set block %s; leaving the original", block.id)
            continue
        result.layouts[block.id] = laid
        for sentence_id, rects in laid.sentence_rects.items():
            result.sentence_rects.setdefault(sentence_id, []).extend(rects)
    return result


# -- painting -----------------------------------------------------------------


def _background(image: QImage, rect: Rect, zoom: float) -> QColor:
    """The paper colour around a paragraph, sampled just outside it.

    Nearly always white. Sampled anyway, because a paper with a tinted
    background, a shaded abstract box or a scanned page would otherwise get a
    white rectangle stamped over every paragraph.
    """
    x0, y0 = int(rect.x0 * zoom) - 3, int(rect.y0 * zoom) - 3
    x1, y1 = int(rect.x1 * zoom) + 3, int(rect.y1 * zoom) + 3
    width, height = image.width(), image.height()
    samples: list[int] = []
    step = max(1, (x1 - x0) // 24)
    for x in range(x0, x1, step):
        for y in (y0, y1):
            if 0 <= x < width and 0 <= y < height:
                samples.append(image.pixel(x, y) & 0xFFFFFF)
    step = max(1, (y1 - y0) // 12)
    for y in range(y0, y1, step):
        for x in (x0, x1):
            if 0 <= x < width and 0 <= y < height:
                samples.append(image.pixel(x, y) & 0xFFFFFF)
    if not samples:
        return QColor(255, 255, 255)
    # The lightest common colour, not the mean: a sample that lands on a
    # neighbouring glyph must not grey the whole paragraph.
    brightest = sorted(samples, key=lambda c: -((c >> 16) + ((c >> 8) & 255) + (c & 255)))
    return QColor.fromRgb(statistics.mode(brightest[: max(1, len(brightest) * 2 // 3)]))


def _qrect(rect: Rect, zoom: float = 1.0) -> QRectF:
    return QRectF(rect.x0 * zoom, rect.y0 * zoom, rect.width * zoom, rect.height * zoom)


def paint_page(
    original: QImage,
    page: PageLayout,
    translated: TranslatedPage,
    fonts: FontBook,
    zoom: float,
) -> QImage:
    """The translated page as an image the same size as ``original``.

    ``original`` is the page rendered at ``zoom``; it is not modified.
    """
    result = original.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if not translated.layouts:
        return result
    # One point is one unit before the zoom, so fonts sized in points land at
    # the right size (see fonts.py).
    result.setDotsPerMeterX(DPI72_DOTS_PER_METER)
    result.setDotsPerMeterY(DPI72_DOTS_PER_METER)

    blocks = {block.id: block for block in page.blocks}
    painter = QPainter(result)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # 1. Blank every translated paragraph.
        blanked: list[Rect] = []
        for block_id in translated.layouts:
            block = blocks[block_id]
            area = block.rect.inflated(0.8, 0.6)
            painter.fillRect(_qrect(area, zoom), _background(original, block.rect, zoom))
            blanked.append(area)

        # 2. Copy back anything that must not change and was caught by (1).
        keep = list(page.protected) + [
            block.rect for block in page.blocks if block.id not in translated.layouts
        ]
        for region in keep:
            for area in blanked:
                overlap = region.intersection(area)
                if overlap is not None:
                    target = _qrect(overlap, zoom)
                    painter.drawImage(target, original, target)

        # 3. Draw the translation, in point units.
        painter.scale(zoom, zoom)
        for block_id, laid in translated.layouts.items():
            block = blocks[block_id]
            metrics = fonts.metrics(serif=block.serif, bold=block.bold)
            painter.setFont(metrics.reference_font)
            painter.setPen(QColor.fromRgb(block.color & 0xFFFFFF))
            shrink = laid.size / metrics.reference_size
            if laid.overflow:
                painter.save()
                painter.setClipRect(_qrect(block.rect.inflated(0.8, 0.6)))
            for line in laid.lines:
                for piece in line.pieces:
                    if piece.math and piece.math_rect is not None:
                        source = block.math.get(piece.math)
                        if source is not None:
                            painter.drawImage(
                                _qrect(piece.math_rect), original, _qrect(source, zoom)
                            )
                    elif piece.text:
                        painter.save()
                        painter.translate(piece.x, line.baseline)
                        painter.scale(shrink, shrink)
                        painter.drawText(QPointF(0.0, 0.0), piece.text)
                        painter.restore()
            if laid.overflow:
                painter.restore()
    finally:
        painter.end()
    return result


def rendered_to_qimage(samples: bytes, width: int, height: int, stride: int) -> QImage:
    """Wrap PyMuPDF's RGB bytes as a QImage that owns its memory."""
    image = QImage(samples, width, height, stride, QImage.Format.Format_RGB888)
    return image.copy()

