"""One paper, one folder (§6).

    <Projects>/<title>/
        paper.json        what the paper is and where it came from
        paper.pdf         the PDF itself, copied in
        layout.json       cached extraction (thrown away when the extractor changes)
        translation.json  sentence id -> Korean
        notes.json        free memo + sentence-anchored notes
        wordbook.json     starred words

The PDF is copied rather than referenced. Lecture Saver references its videos
because they are gigabytes; a paper is a megabyte or two, and a project that
still opens after the Downloads folder is cleaned out is worth more than the
disk space.

Opening the same PDF twice finds the existing project by content hash, so a
paper's translation, notes and words are never split across two folders just
because it was downloaded twice.

Every store writes atomically and every write is a whole file. The files are
small - a long paper's translation is a few hundred kilobytes - and a whole
file is the only kind of write that cannot leave a half-written note behind.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic
from .models import PaperLayout

log = logging.getLogger(__name__)

__all__ = ["AnchoredNote", "HIGHLIGHT_COLORS", "Highlight", "Highlights", "Notes", "Stroke", "Project", "TranslationStore", "WordEntry", "Wordbook"]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _folder_name(title: str) -> str:
    """A folder name Windows accepts, recognisably the paper's title."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned[:70].rstrip(" .") or "Untitled paper")


def _read(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return read_json(path)
    except (OSError, ValueError):
        # A damaged file is set aside, not overwritten: whatever is in it may
        # still be recoverable by hand.
        backup = path.with_suffix(path.suffix + f".damaged-{uuid.uuid4().hex[:6]}")
        log.exception("Could not read %s; keeping it as %s", path, backup.name)
        try:
            path.replace(backup)
        except OSError:
            pass
        return default


# -- translation ------------------------------------------------------------------


class TranslationStore:
    """Korean for each sentence, saved after every batch (§5.4).

    Stored by a hash of the *English text*, not by sentence id. Ids are
    positions ("page 0, block 12, sentence 3") and positions move whenever the
    extractor improves; keyed by id, a re-extracted paper showed last week's
    translation of one sentence under a different one. Keyed by text, a
    translation belongs to the sentence it translates, survives re-extraction,
    and a sentence that repeats (a caption quoted in the text) is translated
    once.

    The public API still speaks sentence ids: :meth:`bind` gives the store the
    paper's layout, and ids are turned into text keys on the way in and out.
    An id the layout does not know is used as its own key.

    Written from the translation worker and read from the UI, hence the lock.
    """

    FORMAT = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        data = _read(path, {})
        if data and data.get("format") != self.FORMAT and data.get("sentences"):
            # The first format was keyed by id, which cannot be trusted across
            # re-extraction (see above). Kept aside rather than deleted.
            legacy = path.with_suffix(".v1.json")
            log.info("Translation file is in the old id-keyed format; moved to %s", legacy.name)
            try:
                path.replace(legacy)
            except OSError:
                pass
            data = {}
        self._items: dict[str, str] = dict(data.get("sentences", {}))
        #: Sentences OpenAI refused to translate even on their own (a grading
        #: prompt quoted in an appendix). Left in English and not asked for
        #: again, so opening the paper does not fail on them every time.
        self._refused: set[str] = set(data.get("refused", []))
        self.engine: str = data.get("engine", "")
        self.model: str = data.get("model", "")
        self._keys: dict[str, str] = {}

    @staticmethod
    def text_key(text: str) -> str:
        return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:16]

    def bind(self, layout: PaperLayout) -> None:
        """Learn which text each sentence id stands for."""
        keys = {s.id: self.text_key(s.text) for _, _, s in layout.sentences()}
        with self._lock:
            self._keys = keys

    def _key(self, sentence_id: str) -> str:
        return self._keys.get(sentence_id, sentence_id)

    def get(self, sentence_id: str) -> str | None:
        with self._lock:
            return self._items.get(self._key(sentence_id))

    def snapshot(self) -> dict[str, str]:
        """Sentence id -> Korean for every bound sentence that has one."""
        with self._lock:
            result = {sid: self._items[key] for sid, key in self._keys.items() if key in self._items}
            if not self._keys:
                result = dict(self._items)
            return result

    def __contains__(self, sentence_id: object) -> bool:
        with self._lock:
            return isinstance(sentence_id, str) and self._key(sentence_id) in self._items

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def refused(self, sentence_id: str) -> bool:
        with self._lock:
            return self._key(sentence_id) in self._refused

    def settled(self, sentence_id: str) -> bool:
        """Translated, or refused and left as it is: nothing more to ask."""
        with self._lock:
            key = self._key(sentence_id)
            return key in self._items or key in self._refused

    def refuse(self, sentence_ids: list[str]) -> None:
        with self._lock:
            self._refused.update(self._key(i) for i in sentence_ids)
        self.update({})

    def update(self, items: dict[str, str], *, engine: str = "", model: str = "") -> None:
        with self._lock:
            for sentence_id, text in items.items():
                self._items[self._key(sentence_id)] = text
            if engine:
                self.engine = engine
            if model:
                self.model = model
            data = {
                "format": self.FORMAT,
                "engine": self.engine,
                "model": self.model,
                "updated": _now(),
                "sentences": dict(self._items),
                "refused": sorted(self._refused),
            }
        write_json_atomic(self.path, data)

    def set(self, sentence_id: str, text: str) -> None:
        """A hand correction of one sentence."""
        self.update({sentence_id: text})


# -- notes ----------------------------------------------------------------------------


@dataclass
class AnchoredNote:
    """A note attached to one sentence (§4.2)."""

    id: str
    sentence_id: str
    page: int
    text: str
    #: The sentence as it read when the note was made, so the note still makes
    #: sense in an export even if the paper is later re-extracted.
    quote: str = ""
    color: str = "#f4b400"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)


class Notes:
    """The free memo and the anchored notes of one paper, in ``notes.json``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        data = _read(path, {})
        self.memo: str = data.get("memo", "")
        self.anchored: list[AnchoredNote] = []
        for item in data.get("anchored", []):
            try:
                self.anchored.append(AnchoredNote(**item))
            except TypeError:
                log.warning("Skipping a malformed note: %r", item)

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {"memo": self.memo, "anchored": [asdict(note) for note in self.anchored]},
        )

    def set_memo(self, text: str) -> None:
        if text != self.memo:
            self.memo = text
            self.save()

    def add(self, sentence_id: str, page: int, text: str, quote: str = "") -> AnchoredNote:
        note = AnchoredNote(
            id=uuid.uuid4().hex[:10], sentence_id=sentence_id, page=page, text=text, quote=quote
        )
        self.anchored.append(note)
        self.save()
        return note

    def edit(self, note_id: str, text: str) -> None:
        for note in self.anchored:
            if note.id == note_id:
                note.text = text
                note.updated = _now()
        self.save()

    def remove(self, note_id: str) -> None:
        self.anchored = [note for note in self.anchored if note.id != note_id]
        self.save()

    def for_sentence(self, sentence_id: str) -> list[AnchoredNote]:
        return [note for note in self.anchored if note.sentence_id == sentence_id]

    def sentence_ids(self) -> set[str]:
        return {note.sentence_id for note in self.anchored}


# -- highlights -----------------------------------------------------------------------

#: The reader's pens, in menu order: name shown, colour stored.
HIGHLIGHT_COLORS: dict[str, str] = {
    "노랑": "#ffd400",
    "초록": "#5fd35f",
    "파랑": "#4aa3ff",
    "분홍": "#ff6fb0",
    "주황": "#ff9a2e",
}


@dataclass
class Highlight:
    """A sentence the reader marked with a colour."""

    sentence_id: str
    page: int
    color: str
    #: The sentence's text, which is how it is found again after re-extraction.
    quote: str = ""
    created: str = field(default_factory=_now)


@dataclass
class Stroke:
    """A stretch of highlighter the reader drew by dragging.

    Stored as rectangles in page points, on the side it was drawn on: a stroke
    across half a sentence has no counterpart on the other page, because half
    an English sentence does not correspond to any particular half of its
    Korean translation.
    """

    id: str
    #: "source" or "translated".
    side: str
    page: int
    rects: list[list[float]]
    text: str
    color: str
    created: str = field(default_factory=_now)
    #: The sentence the stroke starts in, if it starts in one: where the
    #: bookmark list jumps to and whose notes it shows.
    sentence_id: str = ""


class Highlights:
    """Highlights of one paper, in ``highlights.json``.

    Two kinds: whole sentences (from the sentence menu, painted on both pages)
    and free strokes (dragged, painted where they were drawn). A sentence has
    one colour - highlighting it again changes the colour, as on paper.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[str, Highlight] = {}
        self.strokes: list[Stroke] = []
        data = _read(path, {})
        for item in data.get("highlights", []):
            try:
                mark = Highlight(**item)
            except TypeError:
                log.warning("Skipping a malformed highlight: %r", item)
                continue
            self.items[mark.sentence_id] = mark
        for item in data.get("strokes", []):
            try:
                self.strokes.append(Stroke(**item))
            except TypeError:
                log.warning("Skipping a malformed stroke: %r", item)

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "highlights": [asdict(h) for h in self.items.values()],
                "strokes": [asdict(s) for s in self.strokes],
            },
        )

    def add_stroke(
        self, side: str, page: int, rects: list[list[float]], text: str, color: str, sentence_id: str = ""
    ) -> Stroke:
        stroke = Stroke(uuid.uuid4().hex[:10], side, page, rects, text, color, sentence_id=sentence_id)
        self.strokes.append(stroke)
        self.save()
        return stroke

    def recolor_stroke(self, stroke_id: str, color: str) -> None:
        for stroke in self.strokes:
            if stroke.id == stroke_id:
                stroke.color = color
        self.save()

    def remove_stroke(self, stroke_id: str) -> None:
        self.strokes = [s for s in self.strokes if s.id != stroke_id]
        self.save()

    def set(self, sentence_id: str, page: int, color: str, quote: str = "") -> None:
        self.items[sentence_id] = Highlight(sentence_id, page, color, quote)
        self.save()

    def remove(self, sentence_id: str) -> None:
        if self.items.pop(sentence_id, None) is not None:
            self.save()

    def color_of(self, sentence_id: str) -> str | None:
        mark = self.items.get(sentence_id)
        return mark.color if mark else None

    def colors(self) -> dict[str, str]:
        return {sid: mark.color for sid, mark in self.items.items()}


# -- wordbook -------------------------------------------------------------------------


@dataclass
class WordEntry:
    word: str
    lemma: str = ""
    pos: str = ""
    ipa: str = ""
    in_context: str = ""
    senses: list[str] = field(default_factory=list)
    note: str = ""
    #: The sentence it was met in - the example that makes a flashcard stick.
    sentence: str = ""
    sentence_id: str = ""
    page: int = 0
    added: str = field(default_factory=_now)

    @property
    def key(self) -> str:
        return (self.lemma or self.word).lower()


class Wordbook:
    """Starred words (§3.3), newest first."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[WordEntry] = []
        for item in _read(path, {}).get("words", []):
            try:
                self.entries.append(WordEntry(**item))
            except TypeError:
                log.warning("Skipping a malformed word: %r", item)

    def save(self) -> None:
        write_json_atomic(self.path, {"words": [asdict(entry) for entry in self.entries]})

    def contains(self, word: str) -> bool:
        key = word.lower()
        return any(entry.key == key or entry.word.lower() == key for entry in self.entries)

    def add(self, entry: WordEntry) -> None:
        self.entries = [e for e in self.entries if e.key != entry.key]
        self.entries.insert(0, entry)
        self.save()

    def remove(self, word: str) -> None:
        key = word.lower()
        self.entries = [e for e in self.entries if e.key != key and e.word.lower() != key]
        self.save()

    def to_tsv(self) -> str:
        """Front / back / example, the three columns Anki imports without setup."""
        rows = ["word\tmeaning\texample"]
        for entry in self.entries:
            meaning = entry.in_context + (f" ({'; '.join(entry.senses)})" if entry.senses else "")
            cells = [entry.lemma or entry.word, meaning, entry.sentence]
            rows.append("\t".join(cell.replace("\t", " ").replace("\n", " ") for cell in cells))
        return "\n".join(rows) + "\n"


# -- the project ----------------------------------------------------------------------


class Project:
    """A paper's folder and everything in it."""

    META = "paper.json"
    PDF = "paper.pdf"
    LAYOUT = "layout.json"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        meta = _read(self.root / self.META, None)
        if not isinstance(meta, dict):
            raise FileNotFoundError(f"{self.root} is not a Paper Saver project")
        self.meta: dict[str, Any] = meta
        self.translations = TranslationStore(self.root / "translation.json")
        self.notes = Notes(self.root / "notes.json")
        self.wordbook = Wordbook(self.root / "wordbook.json")
        self.highlights = Highlights(self.root / "highlights.json")

    # -- creation ----------------------------------------------------------------

    @classmethod
    def find_or_create(cls, pdf: Path, projects_dir: Path, title: str) -> Project:
        """The project for this PDF, reusing one made from the same file."""
        digest = file_hash(pdf)
        existing = cls.find_by_hash(projects_dir, digest)
        if existing is not None:
            return existing

        projects_dir.mkdir(parents=True, exist_ok=True)
        base = _folder_name(title or pdf.stem)
        root = projects_dir / base
        suffix = 2
        while root.exists():
            root = projects_dir / f"{base} ({suffix})"
            suffix += 1
        root.mkdir(parents=True)
        shutil.copy2(pdf, root / cls.PDF)
        write_json_atomic(
            root / cls.META,
            {
                "app": "paper-saver",
                "version": 1,
                "title": title or pdf.stem,
                "source": str(pdf),
                "sha1": digest,
                "created": _now(),
            },
        )
        return cls(root)

    @classmethod
    def find_by_hash(cls, projects_dir: Path, digest: str) -> Project | None:
        if not projects_dir.is_dir():
            return None
        for meta_path in projects_dir.glob(f"*/{cls.META}"):
            meta = _read(meta_path, {})
            if isinstance(meta, dict) and meta.get("sha1") == digest:
                try:
                    return cls(meta_path.parent)
                except FileNotFoundError:
                    continue
        return None

    # -- contents ----------------------------------------------------------------

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.root.name)

    @property
    def pdf_path(self) -> Path:
        return self.root / self.PDF

    @property
    def exports_dir(self) -> Path:
        path = self.root / "exports"
        path.mkdir(exist_ok=True)
        return path

    def bind(self, layout: PaperLayout) -> None:
        """Attach the layout the stores should interpret sentence ids against.

        Also moves any note whose sentence id now points at different text back
        onto the sentence it was written about, found by its quote.
        """
        self.translations.bind(layout)
        by_text = {" ".join(s.text.split()): (page.number, s.id) for page, _, s in layout.sentences()}
        by_id = {s.id: " ".join(s.text.split()) for _, _, s in layout.sentences()}
        moved = False
        for note in self.notes.anchored:
            quote = " ".join(note.quote.split())
            if not quote or by_id.get(note.sentence_id) == quote:
                continue
            found = by_text.get(quote)
            if found is not None:
                note.page, note.sentence_id = found
                moved = True
        if moved:
            self.notes.save()

        # Highlights are re-anchored the same way, by the sentence they quote.
        rekeyed: dict[str, Highlight] = {}
        for mark in self.highlights.items.values():
            quote = " ".join(mark.quote.split())
            if quote and by_id.get(mark.sentence_id) != quote and quote in by_text:
                mark.page, mark.sentence_id = by_text[quote]
            rekeyed[mark.sentence_id] = mark
        if rekeyed.keys() != self.highlights.items.keys():
            self.highlights.items = rekeyed
            self.highlights.save()

    def load_layout(self) -> PaperLayout | None:
        """The cached layout, or ``None`` if missing or made by an older extractor."""
        data = _read(self.root / self.LAYOUT, None)
        if not isinstance(data, dict):
            return None
        layout = PaperLayout.from_dict(data)
        if layout.version != PaperLayout.VERSION:
            log.info("Layout cache is version %s; re-reading the PDF", layout.version)
            return None
        return layout

    def save_layout(self, layout: PaperLayout) -> None:
        write_json_atomic(self.root / self.LAYOUT, layout.to_dict(), indent=0)

    def set_meta(self, **values: Any) -> None:
        # The start screen files the paper into a folder by writing paper.json
        # while the paper may be open here; start from what is on disk so
        # that is not written back over.
        on_disk = _read(self.root / self.META, None)
        if isinstance(on_disk, dict):
            self.meta = on_disk
        self.meta.update(values)
        write_json_atomic(self.root / self.META, self.meta)
