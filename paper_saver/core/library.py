"""The reader's papers, sorted into folders for the start screen.

A folder is a label, not a directory: each paper stays in its own project
folder (§6), and its folder is written into its ``paper.json`` so the paper
carries it wherever it goes. Moving a paper between folders therefore never
moves a file - nothing that refers to a project folder (recent papers, an
open tab, a conversation that attached it) can break.

The folder names and their order live in ``library.json`` beside the
projects, so a folder the reader made stays even while it is empty.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import read_json, write_json_atomic

log = logging.getLogger(__name__)

__all__ = ["Library", "LibraryEntry"]

META = "paper.json"


@dataclass
class LibraryEntry:
    """One paper as the start screen lists it."""

    root: Path
    title: str
    #: "" for a paper in no folder.
    folder: str
    created: str = ""


def _read_meta(root: Path) -> dict | None:
    try:
        meta = read_json(root / META)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def clean_folder_name(name: str) -> str:
    return " ".join(name.split())[:60]


class Library:
    """Folders and the papers in them, under one projects directory."""

    FILE = "library.json"

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = Path(projects_dir)
        self.path = self.projects_dir / self.FILE
        data: dict = {}
        if self.path.is_file():
            try:
                loaded = read_json(self.path)
                data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                log.warning("Could not read %s; starting with no folders", self.path, exc_info=True)
        self.folders: list[str] = [f for f in data.get("folders", []) if isinstance(f, str) and f]

    def save(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, {"folders": self.folders})

    # -- papers --------------------------------------------------------------------

    def entries(self, extra_roots: list[Path] | None = None) -> list[LibraryEntry]:
        """Every paper in the projects directory, plus any listed elsewhere
        (a recent paper from before the projects directory was changed)."""
        roots: dict[Path, None] = {}
        if self.projects_dir.is_dir():
            for meta_path in sorted(self.projects_dir.glob(f"*/{META}")):
                roots[meta_path.parent.resolve()] = None
        for root in extra_roots or []:
            if (Path(root) / META).is_file():
                roots.setdefault(Path(root).resolve(), None)
        entries = []
        for root in roots:
            meta = _read_meta(root)
            if meta is None:
                continue
            folder = str(meta.get("folder") or "")
            entries.append(
                LibraryEntry(root, str(meta.get("title") or root.name), folder, str(meta.get("created") or ""))
            )
        # A folder named only in a paper (made on another machine, say) is
        # still a folder.
        known = set(self.folders)
        for entry in entries:
            if entry.folder and entry.folder not in known:
                self.folders.append(entry.folder)
                known.add(entry.folder)
        return entries

    def set_folder(self, root: Path, folder: str) -> None:
        """Put a paper in a folder; "" takes it out of any."""
        folder = clean_folder_name(folder)
        meta = _read_meta(Path(root))
        if meta is None:
            raise FileNotFoundError(f"{root} is not a Paper Saver project")
        if folder:
            self.add_folder(folder)
            meta["folder"] = folder
        else:
            meta.pop("folder", None)
        write_json_atomic(Path(root) / META, meta)

    # -- folders -------------------------------------------------------------------

    def add_folder(self, name: str) -> str:
        name = clean_folder_name(name)
        if name and name not in self.folders:
            self.folders.append(name)
            self.save()
        return name

    def rename_folder(self, old: str, new: str, extra_roots: list[Path] | None = None) -> str:
        new = clean_folder_name(new)
        if not new or new == old:
            return old
        papers = [e.root for e in self.entries(extra_roots) if e.folder == old]
        if new in self.folders:
            # Renamed onto an existing folder: the two become one.
            self.folders = [f for f in self.folders if f != old]
        else:
            self.folders = [new if f == old else f for f in self.folders]
        self.save()
        for root in papers:
            self.set_folder(root, new)
        return new

    def remove_folder(self, name: str, extra_roots: list[Path] | None = None) -> None:
        """Delete a folder; the papers in it stay, in no folder."""
        for entry in self.entries(extra_roots):
            if entry.folder == name:
                self.set_folder(entry.root, "")
        self.folders = [f for f in self.folders if f != name]
        self.save()

    def move_folder(self, name: str, index: int) -> None:
        if name not in self.folders:
            return
        self.folders.remove(name)
        self.folders.insert(max(0, min(index, len(self.folders))), name)
        self.save()
