"""The text of a lecture material file, page by page (§4c).

PDFs are read with PyMuPDF; text formats are decoded as they are. A file of
another kind is refused with a message that names what is accepted, rather
than read as garbage.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from ..core.models import PaperLayout

__all__ = ["MATERIAL_SUFFIXES", "STRAY_MIN_SIZE", "MaterialError", "read_pages", "read_paper", "stray_lines"]

#: Text formats a transcript or a quiz may come in, besides PDF.
TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".srt", ".vtt")
MATERIAL_SUFFIXES = (".pdf", *TEXT_SUFFIXES)


class MaterialError(RuntimeError):
    """The file cannot be read as text. Safe to show a user."""


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def read_pages(path: Path) -> list[str]:
    """One string per page for a PDF, a single string for a text file."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            with pymupdf.open(path) as doc:
                return [page.get_text() for page in doc]
        except Exception as exc:  # pymupdf raises several unrelated types
            raise MaterialError(f"'{path.name}'을(를) 읽지 못했습니다:\n{exc}") from exc
    if suffix in TEXT_SUFFIXES:
        try:
            return [_decode(path.read_bytes())]
        except OSError as exc:
            raise MaterialError(f"'{path.name}'을(를) 읽지 못했습니다:\n{exc}") from exc
    accepted = ", ".join(s.lstrip(".") for s in MATERIAL_SUFFIXES)
    raise MaterialError(f"'{path.name}'은(는) 지원하지 않는 형식입니다. ({accepted})")


def read_paper(root: Path) -> tuple[str, str]:
    """Another paper's title and text, each page headed ``[p.N]``.

    The text is read from the project's layout when it is current, so it is
    the same text the reader saw; otherwise from its PDF.
    """
    from ..core.project import Project

    project = Project(root)
    layout = project.load_layout()
    if layout is not None:
        pages = ["\n\n".join(" ".join(b.text.split()) for b in page.blocks if b.text.strip()) for page in layout.pages]
    else:
        pages = read_pages(project.pdf_path)
    text = "\n\n".join(f"[p.{n}]\n{page.strip()}" for n, page in enumerate(pages, start=1) if page.strip())
    return project.title, text


#: Print smaller than this is a plot's ticks and labels, not something to read.
STRAY_MIN_SIZE = 6.0
_TOKEN = re.compile(r"[A-Za-z0-9가-힣]+")
_SPACE = re.compile(r"\s+")


def stray_lines(path: Path, layout: PaperLayout, *, min_size: float = STRAY_MIN_SIZE) -> dict[int, list[str]]:
    """The lines of each page that no paragraph of ``layout`` holds.

    The layout leaves out what it cannot place: a slide's text box it did not
    read as a paragraph, a formula set in pieces, a plot's ticks. A review of
    a lecture needs the first two. Returns, by page (from 0), each line whose
    words are mostly in none of the page's blocks, in reading order, leaving
    out print under ``min_size`` points.
    """
    found: dict[int, list[str]] = {}
    with pymupdf.open(path) as document:
        for page, layout_page in zip(document, layout.pages):
            text_of_blocks = " ".join(block.text for block in layout_page.blocks)
            held = Counter(_TOKEN.findall(text_of_blocks))
            # Korean is often stored without its spaces: "정형데이터와비정형데이터".
            compact = _SPACE.sub("", text_of_blocks)
            lines = []
            for item in page.get_text("dict", sort=True)["blocks"]:
                for line in item.get("lines", []):
                    spans = line.get("spans") or []
                    text = " ".join("".join(span["text"] for span in spans).split())
                    words = _TOKEN.findall(text)
                    if not words or max(span["size"] for span in spans) < min_size:
                        continue
                    if _SPACE.sub("", text) in compact:
                        continue
                    if sum(1 for word in words if held[word]) * 2 < len(words):
                        lines.append(text)
            if lines:
                found[layout_page.number] = lines
    return found
