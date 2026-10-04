"""Questions to GPT about a paper, and the captures they were asked about.

One conversation per paper, kept in the project folder so reopening the paper
brings it back::

    <project>/chat.json        the messages, and the files attached to the conversation
    <project>/captures/        every region the reader captured, as PNG
    <project>/chat_files/      attached files, copied in, each with its text beside it

An attached file (a PDF, notes, a transcript - or another paper the reader has
opened before) stays attached until the reader takes it off: every Codex run
starts fresh, so its text goes with every question, the way a follow-up
re-sends the latest capture.

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
import shutil
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic, write_text_atomic
from .chat_context import SessionState

log = logging.getLogger(__name__)

__all__ = [
    "CAPTURES_DIR", "FILES_DIR", "ChatFile", "ChatLog", "ChatMessage", "capture_name", "render_citations",
    "resolve_citations",
]

CAPTURES_DIR = "captures"
FILES_DIR = "chat_files"


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
    #: Names of the files that went with a question.
    files: list[str] = field(default_factory=list)

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
            files=[str(f) for f in data.get("files") or []],
        )


@dataclass
class ChatFile:
    """A file attached to the conversation."""

    #: What the reader sees: the file's own name, or another paper's title.
    name: str
    #: Its text, relative to the project.
    text: str
    #: The copy of the file, relative to the project ("" for another paper).
    file: str = ""
    #: "file", or "paper" for another Paper Saver project.
    kind: str = "file"
    #: Where it came from: the picked path, or the other project's folder.
    source: str = ""
    added: str = ""
    id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatFile:
        return cls(
            name=str(data.get("name") or "자료"),
            text=str(data.get("text") or ""),
            file=str(data.get("file") or ""),
            kind=str(data.get("kind") or "file"),
            source=str(data.get("source") or ""),
            added=str(data.get("added") or ""),
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
        )


def _safe_file_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:100] or "file"


class ChatLog:
    """The conversation, saved whole and atomically after every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.messages: list[ChatMessage] = []
        #: Files attached to the conversation, in the order attached.
        self.files: list[ChatFile] = []
        #: The Codex session the conversation runs in, and what it was sent.
        self.session: SessionState | None = None
        #: Told when a session ends, so whatever it left on disk can go.
        self.on_session_end: Callable[[SessionState], None] | None = None
        if self.path.is_file():
            try:
                data = read_json(self.path)
                self.messages = [ChatMessage.from_dict(m) for m in data.get("messages", []) if isinstance(m, dict)]
                self.files = [ChatFile.from_dict(f) for f in data.get("files", []) if isinstance(f, dict)]
                if isinstance(data.get("session"), dict) and data["session"].get("id"):
                    self.session = SessionState.from_dict(data["session"])
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
            data = {
                "messages": [asdict(m) for m in self.messages],
                "files": [asdict(f) for f in self.files],
                "session": self.session.to_dict() if self.session else None,
            }
        write_json_atomic(self.path, data)

    def add(
        self,
        role: str,
        text: str,
        *,
        images: list[str] | None = None,
        page: int | None = None,
        refs: list[str] | None = None,
        files: list[str] | None = None,
    ) -> ChatMessage:
        message = ChatMessage(
            role, text, list(images or []), page, _now(), uuid.uuid4().hex[:10], list(refs or []), list(files or [])
        )
        with self._lock:
            self.messages.append(message)
        self.save()
        return message

    def remove(self, message_id: str) -> None:
        with self._lock:
            self.messages = [m for m in self.messages if m.id != message_id]
        self.save()

    def clear(self) -> SessionState | None:
        """Forget the messages, and end the session (returned, so its file
        can be deleted). Attached files stay: they are taken off one by one,
        so a new conversation can start from the same material."""
        with self._lock:
            self.messages = []
            ended, self.session = self.session, None
        self.save()
        self._ended(ended)
        return ended

    def _ended(self, state: SessionState | None) -> None:
        if state is not None and self.on_session_end is not None:
            self.on_session_end(state)

    def set_session(self, state: SessionState) -> None:
        with self._lock:
            self.session = state
        self.save()

    def end_session(self) -> SessionState | None:
        """Start the next question in a new session; the old one is returned."""
        with self._lock:
            ended, self.session = self.session, None
        self.save()
        self._ended(ended)
        return ended

    # -- attached files --------------------------------------------------------------

    @property
    def files_dir(self) -> Path:
        return self.root / FILES_DIR

    def _free_name(self, name: str) -> Path:
        folder = self.files_dir
        folder.mkdir(parents=True, exist_ok=True)
        base = _safe_file_name(name)
        target, counter = folder / base, 2
        while target.exists() or target.with_name(target.name + ".txt").exists():
            target = folder / f"{Path(base).stem} ({counter}){Path(base).suffix}"
            counter += 1
        return target

    def import_file(self, source: Path, text: str) -> ChatFile:
        """Copy a file in and write its text beside it; the log is unchanged.

        Touches only new files, so a worker may do it; :meth:`attach_file`
        then takes it in.
        """
        target = self._free_name(Path(source).name)
        shutil.copy2(source, target)
        text_path = target.with_name(target.name + ".txt")
        write_text_atomic(text_path, text)
        return ChatFile(
            name=Path(source).name, text=self.relative(text_path), file=self.relative(target), kind="file",
            source=str(source), added=_now(), id=uuid.uuid4().hex[:10],
        )

    def import_paper(self, title: str, text: str, source: Path) -> ChatFile:
        """Another paper's text, kept as it was when attached."""
        text_path = self._free_name(f"{title}.txt")
        write_text_atomic(text_path, text)
        return ChatFile(
            name=title, text=self.relative(text_path), kind="paper", source=str(source), added=_now(),
            id=uuid.uuid4().hex[:10],
        )

    def attach_file(self, item: ChatFile) -> None:
        with self._lock:
            self.files.append(item)
        self.save()

    def has_source(self, source: Path | str) -> bool:
        return any(Path(f.source) == Path(source) for f in self.files)

    def detach_file(self, file_id: str) -> SessionState | None:
        """Take a file off the conversation and delete its copies.

        A session that was sent the file still holds it, so the session ends
        too (returned, so its file can be deleted): the next question starts
        afresh without it.
        """
        with self._lock:
            gone = [f for f in self.files if f.id == file_id]
            self.files = [f for f in self.files if f.id != file_id]
            ended = None
            if self.session is not None and any(f"file:{f.id}" in self.session.sent for f in gone):
                ended, self.session = self.session, None
        self.save()
        for item in gone:
            for relative in (item.file, item.text):
                if relative:
                    try:
                        self.absolute(relative).unlink(missing_ok=True)
                    except OSError:
                        log.warning("Could not remove %s", relative, exc_info=True)
        self._ended(ended)
        return ended

    def file_text(self, item: ChatFile) -> str:
        try:
            return self.absolute(item.text).read_text(encoding="utf-8")
        except OSError:
            log.warning("Attached file's text is missing: %s", item.text)
            return ""

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
