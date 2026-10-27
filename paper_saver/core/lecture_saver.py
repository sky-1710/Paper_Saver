"""A Lecture Saver project, read where it lies (§4c.2).

Lecture Saver keeps each lecture in a folder of its own::

    <lecture>/project.json      name, video, when it was last changed
    <lecture>/transcript.json   segments: start, end, original_text, edited_text
    <lecture>/slides.json       slide appearances: start, end, slide_id

Linking one instead of uploading its exported PDF means the STT is read from
there every time, so a line corrected in Lecture Saver is corrected here too.
It is written out as the Markdown Lecture Saver exports - a "## Slide N"
section per slide appearance, its time range, each line under its timestamp -
so :func:`paper_saver.core.lecture.parse_transcript` reads it as it reads an
uploaded export, slide visits and all.

Only the transcript is read: the translation Lecture Saver may hold would say
the same thing again and double the prompt.

Pure file reading: no Qt, testable anywhere.
"""

from __future__ import annotations

import bisect
import logging
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .atomic_io import read_json

log = logging.getLogger(__name__)

__all__ = ["LectureProject", "LectureSaverError", "find_projects", "same_lecture", "transcript_markdown"]


class LectureSaverError(RuntimeError):
    """The project cannot be read. Safe to show a user."""


@dataclass(frozen=True)
class LectureProject:
    root: Path
    name: str
    #: When Lecture Saver last changed it, ISO 8601; "" if unknown.
    updated: str = ""

    @property
    def key(self) -> str:
        return str(self.root)


def _nfc(text: str) -> str:
    """Hangul as whole syllables: names from macOS archives come in loose jamo."""
    return unicodedata.normalize("NFC", text)


def _is_project(folder: Path) -> bool:
    return (folder / "project.json").is_file() and (folder / "transcript.json").is_file()


def _natural(name: str) -> list[Any]:
    """"part 2" before "part 10"."""
    return [int(piece) if piece.isdigit() else piece for piece in re.split(r"(\d+)", name.lower())]


def find_projects(places: Iterable[Path]) -> list[LectureProject]:
    """Every lecture with a transcript in ``places``, by name.

    A place is a project folder itself or a folder of them (Lecture Saver's
    "Projects"). A folder that cannot be read is passed over.
    """
    found: dict[str, LectureProject] = {}
    for place in places:
        try:
            place = Path(place).resolve()
            folders = [place] if _is_project(place) else [f for f in place.iterdir() if f.is_dir() and _is_project(f)]
        except OSError:
            continue
        for folder in folders:
            if str(folder) in found:
                continue
            try:
                meta = read_json(folder / "project.json")
            except (OSError, ValueError):
                log.warning("Skipping an unreadable Lecture Saver project: %s", folder)
                continue
            meta = meta if isinstance(meta, dict) else {}
            name = _nfc(str(meta.get("name") or folder.name))
            found[str(folder)] = LectureProject(folder, name, str(meta.get("updated_at") or ""))
    return sorted(found.values(), key=lambda p: _natural(p.name))


def _words(name: str) -> list[str]:
    name = re.sub(r"\.pdf$", "", _nfc(name).strip(), flags=re.IGNORECASE)
    name = re.sub(r"\s*\(\d+\)$", "", name)  # "OS02 (1).pdf", a second download
    return re.findall(r"[a-z0-9가-힣]+", name.lower())


def same_lecture(pdf_name: str, project_name: str) -> bool:
    """Whether a lecture's slides and a recording are the same lecture by name.

    One name, word by word, starts the other: "OS08-PageReplacement.pdf" and
    the recordings "OS08-PageReplacement-1" and "-2"; "3_EDA.pdf" and
    "3_EDA_statistics". A name of one short word ("3") matches nothing.
    """
    a, b = _words(pdf_name), _words(project_name)
    short, long = sorted((a, b), key=len)
    if not short or (len(short) == 1 and len(short[0]) < 3):
        return False
    return long[: len(short)] == short


# -- the transcript ----------------------------------------------------------------


def _hms(seconds: float) -> str:
    whole = int(max(0.0, seconds))
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise LectureSaverError(f"'{path.parent.name}'의 {path.name}을(를) 읽지 못했습니다: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _spoken(transcript: dict[str, Any]) -> list[tuple[float, float, str]]:
    """(start, end, text) of each line said, the reader's edits in place."""
    lines = []
    for item in transcript.get("segments") or []:
        if not isinstance(item, dict):
            continue
        edited = item.get("edited_text")
        text = str(item.get("original_text") or "") if edited is None else str(edited)
        text = _nfc(" ".join(text.split()))
        if text:
            start = float(item.get("start") or 0.0)
            lines.append((start, max(start, float(item.get("end") or start)), text))
    return sorted(lines, key=lambda line: line[0])


def _appearances(slides: dict[str, Any]) -> list[tuple[int, int, float, float]]:
    """(number shown, slide id, start, end) of each time a slide was on screen."""
    shown = []
    for item in slides.get("appearances") or []:
        if isinstance(item, dict) and item.get("appearance_id") is not None:
            number = int(item["appearance_id"])
            slide = item.get("slide_id")
            shown.append((
                number, int(slide) if slide is not None else -number,
                float(item.get("start") or 0.0), float(item.get("end") or 0.0),
            ))
    return sorted(shown, key=lambda a: a[2])


def _assign(lines: list[tuple[float, float, str]], shown: list[tuple[int, int, float, float]]) -> list[list[str]]:
    """Each line to the appearance it overlaps most, or the last one begun."""
    starts = [a[2] for a in shown]
    groups: list[list[str]] = [[] for _ in shown]
    for start, end, text in lines:
        best, overlap = -1, 0.0
        for index, (_, _, a_start, a_end) in enumerate(shown):
            if a_start >= end:
                break
            covered = min(a_end, end) - max(a_start, start)
            if covered > overlap:
                best, overlap = index, covered
        if best < 0:
            best = max(0, bisect.bisect_right(starts, start) - 1)
        groups[best].append(f"**[{_hms(start)}]** {text}")
    return groups


def transcript_markdown(root: Path) -> str:
    """The lecture's transcript as Lecture Saver would export it in Markdown."""
    root = Path(root)
    if not _is_project(root):
        raise LectureSaverError(f"Lecture Saver 프로젝트를 찾지 못했습니다: {root}")
    meta = _read(root / "project.json")
    lines = _spoken(_read(root / "transcript.json"))
    shown = _appearances(_read(root / "slides.json"))
    out = [f"# {_nfc(str(meta.get('name') or root.name))}", ""]
    if not shown:
        for start, _end, text in lines:
            out += [f"**[{_hms(start)}]** {text}", ""]
        return "\n".join(out).rstrip() + "\n"
    first_shown: dict[int, int] = {}
    for (number, slide_id, start, end), said in zip(shown, _assign(lines, shown)):
        earlier = first_shown.setdefault(slide_id, number)
        heading = f"## Slide {number}" + (f" · Same as Slide {earlier}" if earlier != number else "")
        out += [heading, "", f"`{_hms(start)} – {_hms(end)}`", ""]
        for line in said:
            out += [line, ""]
    return "\n".join(out).rstrip() + "\n"
