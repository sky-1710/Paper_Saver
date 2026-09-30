"""A text report of what the extractor made of a paper.

Layout analysis is heuristics over other people's PDFs, so it is wrong
sometimes, and the only way to tune it is to look at what it did::

    python run.py --inspect paper.pdf 4

Prints one line per paragraph - kind, rectangle, column, font size, sentence
count and the beginning of the text - followed by the protected regions and any
paragraph whose sentences look suspicious. Shipped rather than kept in a
scratch file because the next tuning pass will want it too (§Lecture Saver's
``--diagnose``).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..core.models import TRANSLATABLE_KINDS, PageLayout, PaperLayout
from .document import PaperDocument
from .extract import extract_paper

__all__ = ["inspect_paper", "page_report"]


def _short(text: str, width: int = 74) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def page_report(page: PageLayout) -> list[str]:
    lines = [
        f"— page {page.number + 1}  {page.width:.0f}x{page.height:.0f}pt  "
        f"columns={[f'{a:.0f}-{b:.0f}' for a, b in page.columns]}  "
        f"protected={len(page.protected)}"
    ]
    for rect in page.protected:
        lines.append(f"    protect {rect.as_list()}  {rect.width:.0f}x{rect.height:.0f}")
    for block in page.blocks:
        marker = "T" if block.kind in TRANSLATABLE_KINDS else " "
        lines.append(
            f"  {marker} {block.kind:<9} {block.id:<8} col={block.column:<2} "
            f"sz={block.font_size:4.1f} ld={block.leading:4.1f} "
            f"[{block.rect.x0:5.0f},{block.rect.y0:5.0f},{block.rect.x1:5.0f},{block.rect.y1:5.0f}] "
            f"w={len(block.words):<4} s={len(block.sentences):<3} m={len(block.math):<3} "
            f":: {_short(block.text)}"
        )
        for sentence in block.sentences:
            lines.append(f"        s{sentence.first}-{sentence.last}: {_short(sentence.text, 68)}")
    return lines


def inspect_paper(path: Path | str, page_numbers: list[int] | None = None) -> str:
    """Extract ``path`` and describe it. ``page_numbers`` are 0-based."""
    with PaperDocument(path) as document:
        layout = extract_paper(document.raw, pages=page_numbers)
        title = document.title()
        count = document.page_count

    out = [f"{Path(path).name}  ({count} pages)", f"title guess: {title}", ""]
    for page in layout.pages:
        out += page_report(page)
        out.append("")
    out += _summary(layout)
    return "\n".join(out)


def _summary(layout: PaperLayout) -> list[str]:
    kinds: Counter[str] = Counter()
    sentences = 0
    characters = 0
    for page in layout.pages:
        for block in page.blocks:
            kinds[block.kind] += 1
            if block.kind in TRANSLATABLE_KINDS:
                sentences += len(block.sentences)
                characters += len(block.text)
    return [
        "summary",
        f"  blocks       {dict(sorted(kinds.items()))}",
        f"  sentences    {sentences}",
        f"  characters   {characters} to translate",
    ]
