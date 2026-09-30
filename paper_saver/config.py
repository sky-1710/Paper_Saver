"""Application directories and user settings.

A small local JSON file: window geometry, recent papers, the translation
switch and engine options, the reader's zoom. No cloud sync, no telemetry
(§7.2).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from .core.atomic_io import read_json, write_json_atomic

log = logging.getLogger(__name__)

APP_NAME = "PaperSaver"
APP_DISPLAY_NAME = "Paper Saver"
MAX_RECENT = 12


def app_data_dir() -> Path:
    """Per-user config directory: ``%APPDATA%\\PaperSaver`` on Windows,
    ``~/Library/Application Support/PaperSaver`` on a Mac."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_projects_dir() -> Path:
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / APP_DISPLAY_NAME / "Papers"


def log_file() -> Path:
    return app_data_dir() / "paper_saver.log"


def dictionary_cache() -> Path:
    return app_data_dir() / "dictionary_cache.json"


class Settings:
    """Local user settings, loaded once and saved atomically on every change."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "settings.json")
        self._data: dict[str, Any] = {}
        if self.path.is_file():
            try:
                loaded = read_json(self.path)
                self._data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                log.warning("Could not read settings; starting with defaults", exc_info=True)

    def save(self) -> None:
        try:
            write_json_atomic(self.path, self._data)
        except OSError:
            log.warning("Could not save settings", exc_info=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    @property
    def projects_dir(self) -> Path:
        stored = self._data.get("projects_dir")
        return Path(stored) if stored else default_projects_dir()

    def recent_projects(self) -> list[Path]:
        entries = [Path(p) for p in self._data.get("recent_projects", [])]
        return [p for p in entries if (p / "paper.json").is_file()]

    def add_recent_project(self, root: Path) -> None:
        root = Path(root).resolve()
        others = [str(p) for p in self.recent_projects() if p.resolve() != root]
        self.set("recent_projects", [str(root), *others][:MAX_RECENT])

    @property
    def translation_enabled(self) -> bool:
        """Whether the reader agreed to send paper text to OpenAI (§5.3)."""
        return bool(self._data.get("translation_enabled", False))

    @translation_enabled.setter
    def translation_enabled(self, value: bool) -> None:
        self.set("translation_enabled", bool(value))
