"""Paragraph regions from PyMuPDF's layout model, when it is installed.

``pymupdf-layout`` is a small ONNX model trained on PDF internals, not on page
images: it reads the same text and drawing objects the extractor does and says
where each paragraph, heading, caption, formula, table and page header is.
It runs on the CPU in about half a second a page.

It is better than geometry rules at exactly the decisions the rules get wrong
- where a paragraph ends when its last line happens to be full, which lines
form one display equation, whether a bold run-in phrase is a heading - so it
decides *paragraph boundaries and kinds*. It is not trusted with anything
below that: characters, words, inline formulas and sentences still come from
:mod:`paper_saver.pdf.extract`, because the highlight and the formula re-plant
need exact character boxes, which the model does not provide.

Everything here is optional. Without the package, or if the model fails on a
page, :func:`model_regions` returns ``None`` and the extractor falls back to
its own rules, which is how every page was read before this module existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from ..core.geometry import Rect
from ..core.models import BlockKind

log = logging.getLogger(__name__)

__all__ = ["Region", "TEXT", "model_available", "model_regions"]

#: Hint for ordinary prose: "this is a paragraph", as opposed to no opinion.
TEXT = "text"

_KINDS = {
    "title": BlockKind.TITLE,
    "section-header": BlockKind.HEADING,
    "caption": BlockKind.CAPTION,
    "formula": BlockKind.EQUATION,
    "page-header": BlockKind.FURNITURE,
    "page-footer": BlockKind.FURNITURE,
    "table": BlockKind.GRAPHIC,
    "picture": BlockKind.GRAPHIC,
    "text": TEXT,
    "list-item": TEXT,
    "footnote": TEXT,
}


@dataclass(frozen=True, slots=True)
class Region:
    rect: Rect
    label: str

    @property
    def kind(self) -> str:
        return _KINDS.get(self.label, TEXT)


@lru_cache(maxsize=1)
def model_available() -> bool:
    try:
        import pymupdf.layout  # noqa: F401 - importing activates the model
    except Exception:  # not installed, or onnxruntime missing
        log.info("pymupdf-layout not available; using rule-based paragraphs")
        return False
    return True


def model_regions(page: Any) -> list[Region] | None:
    """The model's regions for ``page`` in reading order, or ``None``."""
    if not model_available():
        return None
    try:
        page.get_layout()
        info = getattr(page, "layout_information", None)
    except Exception:
        log.warning("Layout model failed on page %s", page.number + 1, exc_info=True)
        return None
    if not info:
        return None
    regions: list[Region] = []
    for item in info:
        try:
            x0, y0, x1, y1, label = item[:5]
            regions.append(Region(Rect.of(float(x0), float(y0), float(x1), float(y1)), str(label)))
        except (TypeError, ValueError):
            continue
    return regions or None
