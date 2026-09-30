"""Questions to GPT about a paper, and the captures they were asked about.

One conversation per paper, kept in the project folder so reopening the paper
brings it back::

    <project>/chat.json        the messages
    <project>/captures/        every region the reader captured, as PNG

A message names its images by path relative to the project, so the folder can
be moved or synced as a whole. Captures are written once and never rewritten;
clearing the conversation leaves them where they are, since a note may be
showing one of them.

An answer can point at the sentences it rests on. GPT sees the page with every
sentence tagged ``[S1]``, ``[S2]``…; those tags are page-local, so on arrival
they are turned into the sentences' ids (:attr:`ChatMessage.refs`) and the
text keeps only a marker ``⟦r1⟧`` naming a position in that list.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic

log = logging.getLogger(__name__)

__all__ = ["CAPTURES_DIR", "ChatLog", "ChatMessage", "capture_name", "render_citations", "resolve_citations"]

CAPTURES_DIR = "captures"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def capture_name(page: int | None = None) -> str:
    """A file name for a new capture: sortable by time, unique, ASCII only.

    ASCII matters: the name ends up in Markdown links, in Notion uploads and
    on a Codex command line, and none of those should need escaping.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    where = f"p{page + 1}-" if page is not None and page >= 0 else ""
    return f"capture-{where}{stamp}-{uuid.uuid4().hex[:4]}.png"


@dataclass
class ChatMessage:
    #: "user" or "assistant".
    role: str
    text: str
    #: Attached captures, relative to the project folder.
    images: list[str] = field(default_factory=list)
    #: The page the question was asked from (0-based), when there was one.
    page: int | None = None
    time: str = ""
    id: str = ""
    #: Sentence ids an answer cites, in order; its text marks them ``⟦rN⟧``.
    refs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        page = data.get("page")
        return cls(
            role=str(data.get("role") or "user"),
            text=str(data.get("text") or ""),
            images=[str(i) for i in data.get("images") or []],
            page=int(page) if isinstance(page, int) else None,
            time=str(data.get("time") or ""),
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            refs=[str(r) for r in data.get("refs") or []],
        )


class ChatLog:
    """The conversation, saved whole and atomically after every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.messages: list[ChatMessage] = []
        if self.path.is_file():
            try:
                data = read_json(self.path)
                self.messages = [ChatMessage.from_dict(m) for m in data.get("messages", []) if isinstance(m, dict)]
            except (OSError, ValueError, AttributeError):
                log.warning("Chat log unreadable; starting a new conversation", exc_info=True)

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def captures_dir(self) -> Path:
        return self.root / CAPTURES_DIR

    def new_capture_path(self, page: int | None = None) -> Path:
        folder = self.captures_dir
        folder.mkdir(parents=True, exist_ok=True)
        return folder / capture_name(page)

    def relative(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def absolute(self, image: str) -> Path:
        path = Path(image)
        return path if path.is_absolute() else self.root / path

    def save(self) -> None:
        with self._lock:
            data = {"messages": [asdict(m) for m in self.messages]}
        write_json_atomic(self.path, data)

    def add(
        self,
        role: str,
        text: str,
        *,
        images: list[str] | None = None,
        page: int | None = None,
        refs: list[str] | None = None,
    ) -> ChatMessage:
        message = ChatMessage(role, text, list(images or []), page, _now(), uuid.uuid4().hex[:10], list(refs or []))
        with self._lock:
            self.messages.append(message)
        self.save()
        return message

    def remove(self, message_id: str) -> None:
        with self._lock:
            self.messages = [m for m in self.messages if m.id != message_id]
        self.save()

    def clear(self) -> None:
        with self._lock:
            self.messages = []
        self.save()

    def history(self) -> list[tuple[str, str]]:
        """The turns as GPT is shown them again: without citation markers,
        whose sentence tags meant something only on the page asked from."""
        return [(m.role, render_citations(m.text, m.refs, lambda _ids: "")) for m in self.messages]

    def question_for(self, answer_id: str) -> ChatMessage | None:
        """The user message an assistant message answered."""
        for index, message in enumerate(self.messages):
            if message.id == answer_id:
                return next((m for m in reversed(self.messages[:index]) if m.role == "user"), None)
        return None

    def recent_images(self, limit: int = 3) -> list[str]:
        """Images from the latest questions that had any, newest last.

        A follow-up ("and the y axis?") is about the picture asked about a
        moment ago, and every Codex run starts fresh, so those are sent again.
        """
        found: list[str] = []
        for message in reversed(self.messages):
            if message.role != "user" or not message.images:
                continue
            found = [*message.images, *found]
            if len(found) >= limit:
                break
        return found[-limit:]


# -- citations ---------------------------------------------------------------------

#: A tag GPT was asked to write: [S3], also [S3, S5] and [S3-S5].
_TAG = re.compile(r"\[\s*S\d+(?:\s*[-–~,，]\s*S?\d+)*\s*\]")
_TAG_PART = re.compile(r"S?(\d+)(?:\s*[-–~]\s*S?(\d+))?")
#: Consecutive markers in a stored answer, with the space before them.
_MARKERS = re.compile(r"(?:\s*⟦r\d+⟧)+")
_MARKER = re.compile(r"⟦r(\d+)⟧")
#: A "range" wider than this is a typo, not a citation.
_MAX_RANGE = 8


def resolve_citations(answer: str, numbering: Mapping[int, str]) -> tuple[str, list[str]]:
    """GPT's ``[S3]`` tags turned into markers, and the sentence ids they name.

    ``numbering`` maps a tag number to the sentence id it stood for in the
    prompt. A tag with no such number is dropped: pointing at the wrong
    sentence would be worse than pointing at none.
    """
    refs: list[str] = []

    def marker(match: re.Match[str]) -> str:
        out: list[str] = []
        for part in _TAG_PART.finditer(match.group(0)):
            first = int(part.group(1))
            last = int(part.group(2)) if part.group(2) else first
            if not 0 <= last - first <= _MAX_RANGE:
                last = first
            for number in range(first, last + 1):
                sentence_id = numbering.get(number)
                if sentence_id is None:
                    continue
                if sentence_id not in refs:
                    refs.append(sentence_id)
                out.append(f"⟦r{refs.index(sentence_id) + 1}⟧")
        return "".join(dict.fromkeys(out))

    return _TAG.sub(marker, answer), refs


def render_citations(text: str, refs: list[str], link: Callable[[list[str]], str]) -> str:
    """Each run of markers replaced by ``link(sentence ids)``.

    The run takes the space before it along, so ``link`` decides the spacing:
    ``" (p.3)"`` for a note, ``""`` to drop citations altogether.
    """

    def group(match: re.Match[str]) -> str:
        ids = [refs[int(n) - 1] for n in _MARKER.findall(match.group(0)) if 0 < int(n) <= len(refs)]
        ids = list(dict.fromkeys(ids))
        return link(ids) if ids else ""

    return _MARKERS.sub(group, text)
