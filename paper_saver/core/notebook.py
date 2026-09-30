"""The reader's note for a paper: a plain Markdown file wherever they want it.

A note is an ordinary ``.md`` file - in the project folder by default, or in an
Obsidian vault, a synced folder, anywhere - so it can be opened and edited by
any other Markdown tool. Pasted captures are saved beside it::

    <anywhere>/My note.md
    <anywhere>/assets/capture-p3-20260930-141522-a1b2.png

and linked relatively (``![p.3](assets/capture-....png)``), which Obsidian,
Typora, VS Code and GitHub all display. Moving a note to another folder copies
the images it links along with it, so no link breaks.

The file may be edited by something else while the paper is open. A save
never silently overwrites that: if the file changed on disk since it was last
read, the other version is kept beside it first (§4.3 - nothing the reader
typed may be lost, whichever program they typed it in).
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from .atomic_io import write_text_atomic

__all__ = [
    "ASSETS_DIR",
    "ImageRef",
    "copy_linked_images",
    "image_link",
    "image_refs",
    "read_note",
    "resolve_image",
    "write_note",
]

ASSETS_DIR = "assets"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}

#: ``![alt](path "title")`` and ``![alt](<path with spaces>)``.
_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*(?:<(?P<angled>[^>]+)>|(?P<plain>[^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")
#: Obsidian's ``![[name.png]]`` / ``![[name.png|300]]``.
_WIKI_IMAGE = re.compile(r"!\[\[(?P<target>[^\]|#]+)(?:[|#][^\]]*)?\]\]")


class ImageRef:
    """One image link in a note: where it is in the text and what it names."""

    __slots__ = ("alt", "end", "start", "target", "wiki")

    def __init__(self, start: int, end: int, target: str, alt: str, wiki: bool) -> None:
        self.start, self.end, self.target, self.alt, self.wiki = start, end, target, alt, wiki

    @property
    def remote(self) -> bool:
        return bool(re.match(r"^[a-z][a-z0-9+.-]*://", self.target, re.I)) and not self.target.lower().startswith("file:")


def image_refs(text: str) -> list[ImageRef]:
    refs = [
        ImageRef(m.start(), m.end(), unquote(m.group("angled") or m.group("plain")), m.group("alt"), False)
        for m in _MD_IMAGE.finditer(text)
    ]
    refs += [
        ImageRef(m.start(), m.end(), m.group("target").strip(), "", True)
        for m in _WIKI_IMAGE.finditer(text)
        if Path(m.group("target").strip()).suffix.lower() in IMAGE_SUFFIXES
    ]
    return sorted(refs, key=lambda r: r.start)


def resolve_image(ref: ImageRef | str, base_dir: Path) -> Path | None:
    """The file an image link points at, or ``None`` when it is not there.

    Obsidian resolves ``![[name]]`` by name anywhere in the vault; here the
    note's folder and its ``assets`` folder are searched, which is where
    Paper Saver and Obsidian's default settings put attachments.
    """
    target = ref.target if isinstance(ref, ImageRef) else ref
    wiki = ref.wiki if isinstance(ref, ImageRef) else False
    if target.lower().startswith("file:"):
        target = unquote(re.sub(r"^file:/*", "", target, flags=re.I))
        if not re.match(r"^[A-Za-z]:", target):
            target = "/" + target
    path = Path(target)
    candidates = [path] if path.is_absolute() else [base_dir / path]
    if wiki:
        candidates += [base_dir / ASSETS_DIR / path.name, base_dir / "attachments" / path.name]
    return next((c for c in candidates if c.is_file()), None)


def image_link(note_path: Path, image: Path, alt: str = "") -> str:
    """A Markdown image link from the note to ``image``, relative when it can be."""
    try:
        target = image.resolve().relative_to(note_path.parent.resolve()).as_posix()
    except ValueError:
        target = image.resolve().as_posix()
    if re.search(r"[\s()<>]", target):
        target = f"<{target}>"
    return f"![{alt}]({target})"


def copy_linked_images(text: str, source_dir: Path, target_dir: Path) -> list[Path]:
    """Copy every image the note links relatively from one folder to another.

    Used when a note is saved somewhere new: the links stay exactly as they
    are and keep working, because the files they name come along. An image
    already present at the destination is left alone.
    """
    copied: list[Path] = []
    if source_dir.resolve() == target_dir.resolve():
        return copied
    for ref in image_refs(text):
        if ref.remote or Path(ref.target).is_absolute():
            continue
        source = resolve_image(ref, source_dir)
        if source is None:
            continue
        relative = source.resolve().relative_to(source_dir.resolve()) if _inside(source, source_dir) else Path(source.name)
        destination = target_dir / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def read_note(path: Path) -> tuple[str, int | None]:
    """The note's text and its modification stamp; empty for a note not yet written."""
    if not path.is_file():
        return "", None
    text = path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return text, path.stat().st_mtime_ns


def write_note(path: Path, text: str, *, known_mtime: int | None) -> tuple[int, Path | None]:
    """Save the note; returns its new stamp and where another version was kept.

    ``known_mtime`` is the stamp from when the note was last read or written
    here. If the file on disk has changed since - another editor saved it -
    that version is copied to ``<name> (외부 수정 <time>).md`` before it is
    replaced, and its path is returned so the reader can be told.
    """
    kept: Path | None = None
    if path.is_file() and path.stat().st_mtime_ns != known_mtime:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = path.with_name(f"{path.stem} (외부 수정 {stamp}){path.suffix}")
        shutil.copy2(path, kept)
    write_text_atomic(path, text)
    return path.stat().st_mtime_ns, kept
