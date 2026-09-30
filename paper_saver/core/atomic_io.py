"""Crash-safe JSON persistence (§4.3).

The write sequence is: serialise fully in memory, write a temporary file *beside*
the target, flush + fsync it, then ``os.replace``. On NTFS ``os.replace`` is
atomic, but only within a single volume - which is exactly why the temporary
file goes next to the target rather than into ``%TEMP%``.

Serialising before touching the disk matters too: if the data cannot be encoded,
the existing file is left completely untouched instead of being truncated.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["write_json_atomic", "read_json", "write_text_atomic"]


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, data: Any, *, indent: int = 2) -> None:
    """Serialise ``data`` to JSON and replace ``path`` atomically.

    ``ensure_ascii=False`` keeps Korean text readable in the on-disk files.
    """
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    write_text_atomic(path, text)


def read_json(path: Path) -> Any:
    """Read UTF-8 JSON. Raises ``FileNotFoundError`` / ``json.JSONDecodeError``."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
