"""The parts of a page whose pixels must survive untouched (§1.2, §1.4).

A figure, a plot, a table or a display equation is not text that happens to look
complicated - it is a picture with its own internal geometry. Redrawing one is
how "the figure moved" happens, so Paper Saver never redraws one: it finds them
and leaves them alone, and the translated page is the original page with only
the paragraphs repainted.

Three sources, deliberately overlapping, because each misses a different kind of
figure:

``get_image_info``
    Raster figures - screenshots, photographs, anything pasted in as a bitmap.
``find_tables``
    Ruled tables. Over-detection is the safe direction here: a paragraph wrongly
    taken for a table stays in English, which the reader can see, whereas a
    table wrongly taken for a paragraph would be overwritten.
``cluster_drawings``
    Vector figures, which is what most plots in a LaTeX paper are: hundreds of
    tiny line and curve commands that only mean something as a group.

Rules, hairlines and the thin boxes that ruled tables are made of are excluded
by size - protecting them would pin down nothing, and a rule that spans a column
would wrongly protect the paragraph beside it. A plain filled rectangle behind
most of the page is excluded too: it is the page's background, which every
slide exported from PowerPoint begins with, and clustering it would make the
whole page one figure.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.geometry import Rect

log = logging.getLogger(__name__)

__all__ = ["protected_regions"]

#: Smallest fraction of the page a drawing cluster must cover to count as a
#: figure. Below this it is an underline, an equation rule or a bullet glyph.
MIN_CLUSTER_AREA = 0.004
#: ... and it must be at least this many points across in both directions, so a
#: full-width hairline (large area, no height) is not mistaken for a figure.
MIN_CLUSTER_SIDE = 14.0
#: A single filled rectangle covering at least this fraction of the page is a
#: background, not a figure.
BACKGROUND_FILL_AREA = 0.5


def _rect(value: Any) -> Rect:
    return Rect.of(float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _images(page: Any) -> list[Rect]:
    try:
        return [_rect(info["bbox"]) for info in page.get_image_info()]
    except Exception:  # pragma: no cover - a malformed PDF must not stop a page
        log.warning("Could not read image positions on page %s", page.number, exc_info=True)
        return []


def _tables(page: Any) -> list[Rect]:
    """Ruled tables with at least two rows and two columns.

    The row/column floor rejects ``find_tables``' favourite false positive, a
    single-column "table" that is really a paragraph beside a vertical rule.
    """
    try:
        finder = page.find_tables()
    except Exception:
        log.warning("Table detection failed on page %s", page.number, exc_info=True)
        return []
    found: list[Rect] = []
    for table in getattr(finder, "tables", []):
        try:
            if table.col_count < 2 or len(table.rows) < 2:
                continue
            found.append(_rect(table.bbox))
        except Exception:
            continue
    return found


def _is_background(drawing: dict[str, Any], page_area: float) -> bool:
    """A plain filled rectangle behind most of the page."""
    items = drawing.get("items") or []
    return (
        drawing.get("type") == "f"
        and len(items) == 1
        and items[0][0] == "re"
        and _rect(drawing["rect"]).area >= page_area * BACKGROUND_FILL_AREA
    )


def _drawing_clusters(page: Any) -> list[Rect]:
    if not hasattr(page, "cluster_drawings"):  # pragma: no cover - older PyMuPDF
        return []
    page_area = max(1.0, page.rect.get_area())
    try:
        drawings = [d for d in page.get_drawings() if not _is_background(d, page_area)]
        clusters = page.cluster_drawings(drawings=drawings) if drawings else []
    except Exception:
        log.warning("Drawing clustering failed on page %s", page.number, exc_info=True)
        return []
    kept: list[Rect] = []
    for cluster in clusters:
        rect = _rect(cluster)
        if rect.width < MIN_CLUSTER_SIDE or rect.height < MIN_CLUSTER_SIDE:
            continue
        if rect.area < page_area * MIN_CLUSTER_AREA:
            continue
        kept.append(rect)
    return kept


def _merge_overlapping(rects: list[Rect]) -> list[Rect]:
    """Fuse regions that touch, so a figure reported twice is one region.

    Repeated until nothing changes: a table found by ``find_tables`` and the
    cluster of its ruling lines are usually a few points apart, and two regions
    that overlap at all describe one picture.
    """
    merged = list(rects)
    changed = True
    while changed:
        changed = False
        result: list[Rect] = []
        for rect in merged:
            for index, existing in enumerate(result):
                if existing.inflated(2.0).intersects(rect):
                    result[index] = existing.union(rect)
                    changed = True
                    break
            else:
                result.append(rect)
        merged = result
    return merged


def protected_regions(page: Any) -> list[Rect]:
    """Every region of ``page`` that the translated page copies verbatim."""
    regions = _images(page) + _tables(page) + _drawing_clusters(page)
    # A picture placed partly off the page is protected only where it shows.
    bounds = _rect(page.rect)
    visible = [clipped for r in regions if (clipped := r.intersection(bounds)) is not None]
    return _merge_overlapping(visible)
