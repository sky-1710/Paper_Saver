"""Build the source zip: ``python tools/make_zip.py`` -> ``dist/PaperSaver-<version>-source.zip``.

For running from source with the launchers (any OS with Python 3.10+). The
standalone applications are built by ``tools/build.py`` instead.

The zip holds source and launchers only. Nothing personal goes in it: no
virtual environment, no translated papers, no settings, no dictionary cache,
and above all no Codex sign-in - those all live outside this folder (in
Documents, in %APPDATA% or ~/Library, and in ~/.codex respectively), which is
what makes the zip safe to hand to someone else.

The macOS launcher is stored with its executable bit set, so Archive Utility
unpacks it as something Finder can run.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_saver import __version__  # noqa: E402

INCLUDE = [
    "paper_saver",
    "tests",
    "tools",
    "packaging",
    ".github",
    "run.py",
    "requirements.txt",
    "pyproject.toml",
    "README.md",
    "CLAUDE.md",
    "Paper Saver.bat",
    "Paper Saver.command",
]
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", "dist", "build", ".git"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log"}
EXECUTABLE = {"Paper Saver.command"}


def files() -> list[Path]:
    found: list[Path] = []
    for entry in INCLUDE:
        path = ROOT / entry
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found += [
                p for p in sorted(path.rglob("*"))
                if p.is_file() and not SKIP_PARTS.intersection(p.parts) and p.suffix not in SKIP_SUFFIXES
            ]
        else:
            raise SystemExit(f"missing: {entry}")
    return found


def main() -> int:
    top = f"PaperSaver-{__version__}"
    target = ROOT / "dist" / f"{top}-source.zip"
    target.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files():
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(f"{top}/{relative}")
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o755 if relative in EXECUTABLE else 0o644
            info.external_attr = (0o100000 | mode) << 16  # regular file + permissions
            info.create_system = 3  # "Unix", so the permissions are honoured
            archive.writestr(info, path.read_bytes())
    count = len(zipfile.ZipFile(target).namelist())
    print(f"{target}  ({count} files, {target.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
