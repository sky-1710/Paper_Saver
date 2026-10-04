"""The text of a lecture material file, page by page (§4c).

PDFs are read with PyMuPDF; text formats are decoded as they are. A file of
another kind is refused with a message that names what is accepted, rather
than read as garbage.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

__all__ = ["MATERIAL_SUFFIXES", "MaterialError", "read_pages", "read_paper"]

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
