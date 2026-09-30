"""Questions to GPT about a paper, and the captures they were asked about.

One conversation per paper, kept in the project folder so reopening the paper
brings it back::

    <project>/chat.json        the messages
    <project>/captures/        every region the reader captured, as PNG

A message names its images by path relative to the project, so the folder can
be moved or synced as a whole. Captures are written once and never rewritten;
clearing the conversation leaves them where they are, since a note may be
showing one of them.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic

log = logging.getLogger(__name__)

__all__ = ["CAPTURES_DIR", "ChatLog", "ChatMessage", "capture_name"]

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

    def add(self, role: str, text: str, *, images: list[str] | None = None, page: int | None = None) -> ChatMessage:
        message = ChatMessage(role, text, list(images or []), page, _now(), uuid.uuid4().hex[:10])
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
        return [(m.role, m.text) for m in self.messages]

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
