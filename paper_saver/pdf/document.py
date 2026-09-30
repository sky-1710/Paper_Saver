"""Opening a paper and turning its pages into pixels.

The one place PyMuPDF is held open. Everything else works from the extracted
layout, which means the PDF can be closed and reopened, moved into the project
folder, or replaced by a corrected version without any other part of the
application knowing.

Rendering returns raw RGB bytes rather than a ``QImage`` so that this module,
like the rest of :mod:`paper_saver.pdf`, can be tested without a display.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from ..core.geometry import Rect

# Table detection prints an advert for an optional package once per process.
if hasattr(pymupdf, "no_recommend_layout"):
    pymupdf.no_recommend_layout()

log = logging.getLogger(__name__)

__all__ = ["PaperDocument", "PaperOpenError", "RenderedPage", "guess_title"]


class PaperOpenError(RuntimeError):
    """The file is not a PDF this machine can read. Safe to show a user."""


@dataclass(frozen=True, slots=True)
class RenderedPage:
    """One page as RGB888 pixels, and the scale they were drawn at."""

    samples: bytes
    width: int
    height: int
    stride: int
    zoom: float


class PaperDocument:
    """An open PDF, page sizes in points, pages on demand as pixels."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        try:
            self._doc = pymupdf.open(self.path)
        except Exception as exc:  # pymupdf raises several unrelated types
            raise PaperOpenError(f"'{self.path.name}' could not be opened:\n{exc}") from exc
        if self._doc.needs_pass:
            self._doc.close()
            raise PaperOpenError(
                f"'{self.path.name}' is password-protected.\n\n"
                "Save an unlocked copy and open that instead."
            )
        if self._doc.page_count == 0:
            self._doc.close()
            raise PaperOpenError(f"'{self.path.name}' has no pages.")

    # -- lifetime ------------------------------------------------------------

    def close(self) -> None:
        try:
            self._doc.close()
        except Exception:  # pragma: no cover
            pass

    def __enter__(self) -> PaperDocument:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- pages ---------------------------------------------------------------

    @property
    def page_count(self) -> int:
        return int(self._doc.page_count)

    @property
    def raw(self) -> Any:
        """The PyMuPDF document, for the extractor."""
        return self._doc

    def page(self, number: int) -> Any:
        return self._doc[number]

    def page_rect(self, number: int) -> Rect:
        return Rect.from_list(list(self._doc[number].rect))

    def render(self, number: int, zoom: float = 2.0) -> RenderedPage:
        """Draw a page at ``zoom`` times its point size.

        Alpha is switched off: a paper page is opaque, and dropping the channel
        is a third less memory per page and a faster blit while scrolling.
        """
        matrix = pymupdf.Matrix(zoom, zoom)
        pixmap = self._doc[number].get_pixmap(matrix=matrix, alpha=False)
        return RenderedPage(
            samples=pixmap.samples,
            width=pixmap.width,
            height=pixmap.height,
            stride=pixmap.stride,
            zoom=zoom,
        )

    # -- what to call this paper --------------------------------------------

    def title(self) -> str:
        return guess_title(self._doc)


_BAD_TITLES = {"", "untitled", "microsoft word", "pdf", "paper", "main", "manuscript"}


def guess_title(document: Any) -> str:
    """A name for the project folder: the PDF's own title, or its first line.

    PDF metadata titles are unreliable - LaTeX often leaves the name of the tex
    file, Word leaves "Microsoft Word - final(3).doc" - so a metadata title is
    only trusted when it looks like a sentence of English or Korean.
    """
    metadata = (document.metadata or {}).get("title") or ""
    candidate = re.sub(r"\s+", " ", metadata).strip()
    if candidate.lower().split(".")[0] not in _BAD_TITLES and len(candidate) >= 12:
        if not re.search(r"\.(tex|doc|docx|dvi|pdf)$", candidate, re.IGNORECASE):
            return candidate[:120]

    try:
        first = document[0].get_text("dict")
    except Exception:  # pragma: no cover
        return ""
    best_size = 0.0
    best_text = ""
    for block in first.get("blocks", [])[:12]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = re.sub(r"\s+", " ", span.get("text", "")).strip()
                if len(text) < 8 or span.get("size", 0) <= best_size:
                    continue
                best_size, best_text = span["size"], text
    return best_text[:120]
