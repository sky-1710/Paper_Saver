"""Registering a looked-up word in the reader's Obsidian vocabulary vault.

The vault keeps one Markdown file per first letter, in two parallel folders:

    <vault>/dictionary/P words.md          - the English side
    <vault>/dictionary_ko/P words_ko.md    - the Korean side

and each registered word appears in both, linked to the other by block id:

    dictionary/P words.md                  dictionary_ko/P words_ko.md
    - probe ^probe                         - probe ^probeko
        - [[P words_ko#^probeko|ko]]           - [[P words#^probe|eng]]
                                                 verb
                                               1. 캐묻다, 조사하다
                                                   He didn't like the media **probing** ...
                                                   그는 ...
                                               2. ...

A word that already has a line in either file keeps it: the English line gains
the ``|ko`` link (and a block id if it had none), a bare Korean stub gains the
meanings, and an entry registered before gains only the meanings it does not
already list, numbered on from its last one. Entries are inserted in alphabetical order among the file's
top-level ``- word`` lines. Writes are atomic, so Obsidian or OneDrive never
sees a half-written note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .atomic_io import write_text_atomic
from .models import MATH_TOKEN_END, MATH_TOKEN_START

__all__ = ["ObsidianError", "RegisterResult", "register_word", "is_vocab_vault"]

ENGLISH_DIR = "dictionary"
KOREAN_DIR = "dictionary_ko"

#: The lookup answers the part of speech in Korean; the vault writes it in English.
_POS = {
    "명사": "noun",
    "동사": "verb",
    "형용사": "adjective",
    "부사": "adverb",
    "전치사": "preposition",
    "접속사": "conjunction",
    "대명사": "pronoun",
    "감탄사": "exclamation",
    "한정사": "determiner",
    "관사": "article",
    "조동사": "auxiliary verb",
}

_TOP = re.compile(r"^- +(?P<word>[A-Za-z][A-Za-z'’\-]*)")
_BLOCK_ID = re.compile(r"\s\^(?P<id>[A-Za-z0-9\-]+)\s*$")
_NUMBERED = re.compile(r"^[\t ]*(\d+)\.\s+(.*\S)")
#: Strips a part-of-speech line ("\t- Noun ^c11da7") down to the word.
_POS_LINE = re.compile(r"^[\t ]*(?:-\s+)?|(?:\s+\^[A-Za-z0-9\-]+)*\s*$")
_MATH = re.compile(f"{re.escape(MATH_TOKEN_START)}m\\d+{re.escape(MATH_TOKEN_END)}")


class ObsidianError(Exception):
    """The vault is missing or the word cannot be filed."""


@dataclass
class RegisterResult:
    word: str
    english_file: Path
    korean_file: Path
    #: True when the word was already fully registered and nothing was written.
    already: bool = False
    #: Meanings added to an entry that existed before (0 for a new entry).
    appended: int = 0
    #: A new example was put under a meaning the entry already listed.
    example_added: bool = False
    changed: list[Path] = field(default_factory=list)


def is_vocab_vault(folder: Path) -> bool:
    return (Path(folder) / ENGLISH_DIR).is_dir()


def headword(entry: dict[str, Any]) -> str:
    """The form filed in the vault: the lemma, lower-cased unless an acronym."""
    word = str(entry.get("lemma") or entry.get("word") or "").strip()
    return word if word.isupper() and len(word) > 1 else word.lower()


def register_word(
    vault: Path,
    entry: dict[str, Any],
    *,
    example: str = "",
    example_ko: str = "",
    surface: str = "",
) -> RegisterResult:
    """File ``entry`` (a dictionary lookup) in both halves of the vault.

    ``example`` / ``example_ko`` go directly under the first meaning when given;
    ``surface`` is the form met in the sentence, bolded in the example.
    """
    vault = Path(vault)
    if not is_vocab_vault(vault):
        raise ObsidianError(f"'{vault}' 안에 {ENGLISH_DIR} 폴더가 없습니다.")
    word = headword(entry)
    if not word or not word[0].isascii() or not word[0].isalpha():
        raise ObsidianError("영어 단어만 등록할 수 있습니다.")
    letter = word[0].upper()
    en_name, ko_name = f"{letter} words", f"{letter} words_ko"
    en_path = vault / ENGLISH_DIR / f"{en_name}.md"
    ko_path = vault / KOREAN_DIR / f"{ko_name}.md"
    result = RegisterResult(word, en_path, ko_path)

    en = _Note.load(en_path)
    ko = _Note.load(ko_path)

    # -- the English side: find or create the line, make sure it links to ko --
    en_at = en.find(word)
    if en_at is None:
        en_id = _safe_id(word)
        ko_id = f"{en_id}ko"
        en.insert(word, [f"- {word} ^{en_id}", f"\t- [[{ko_name}#^{ko_id}|ko]]"])
    else:
        en_id = en.block_id(en_at)
        if en_id is None:
            en_id = _safe_id(word)
            en.lines[en_at] = f"{en.lines[en_at].rstrip()} ^{en_id}"
            en.dirty = True
        linked = en.ko_link(en_at, ko_name)
        ko_id = linked or f"{en_id}ko"
        if linked is None:
            en.lines.insert(en_at + 1, f"\t- [[{ko_name}#^{ko_id}|ko]]")
            en.dirty = True

    # -- the Korean side ------------------------------------------------------
    surface = surface or str(entry.get("word", ""))
    body = _korean_body(entry, en_name, en_id, example, example_ko, surface)
    ko_at = ko.find(word)
    if ko_at is None:
        ko.insert(word, [f"- {word} ^{ko_id}", *body])
    elif ko.block_id(ko_at) == ko_id or ko.eng_link(ko_at, en_name):
        # Registered before: a new example goes under the meaning it already
        # lists, and only the meanings it does not list yet are added.
        result.example_added = ko.add_example(ko_at, entry, example, example_ko, surface)
        result.appended = ko.append_meanings(ko_at, entry, example, example_ko, surface)
        if not result.appended and not result.example_added and not en.dirty:
            result.already = True
            return result
    else:
        # A stub copied over from the English file: give it the Korean id and
        # put the meanings first, above whatever it already held.
        head = _BLOCK_ID.sub("", ko.lines[ko_at]).rstrip()
        ko.lines[ko_at] = f"{head} ^{ko_id}"
        ko.lines[ko_at + 1 : ko_at + 1] = body
        ko.dirty = True

    for note in (en, ko):
        if note.dirty:
            note.save()
            result.changed.append(note.path)
    return result


def _korean_body(
    entry: dict[str, Any], en_name: str, en_id: str, example: str, example_ko: str, surface: str
) -> list[str]:
    lines = [f"\t- [[{en_name}#^{en_id}|eng]]"]
    pos = _pos(entry)
    if pos:
        lines.append(f"\t  {pos}")
    return lines + _numbered(_meanings(entry), 1, entry, example, example_ko, surface)


def _pos(entry: dict[str, Any]) -> str:
    pos = str(entry.get("pos") or "").strip()
    return _POS.get(pos, pos)


def _meanings(entry: dict[str, Any]) -> list[str]:
    """The in-sentence meaning first, then the general senses, without repeats."""
    meanings: list[str] = []
    for text in [entry.get("in_context"), *(entry.get("senses") or [])]:
        text = " ".join(str(text or "").split())
        if text and _norm(text) not in {_norm(m) for m in meanings}:
            meanings.append(text)
    return meanings


def _numbered(
    meanings: list[str], start: int, entry: dict[str, Any], example: str, example_ko: str, surface: str
) -> list[str]:
    """``1. meaning`` lines; the example goes under the in-sentence meaning."""
    context = _norm(str(entry.get("in_context") or ""))
    lines: list[str] = []
    for number, meaning in enumerate(meanings, start=start):
        lines.append(f"\t{number}. {meaning}")
        if example.strip() and _norm(meaning) == context:
            lines.append(f"\t\t{_bold(_clean(example), surface)}")
            if example_ko.strip():
                lines.append(f"\t\t{_clean(example_ko)}")
    return lines


def _norm(text: str) -> str:
    """A meaning reduced to what decides "is it the same meaning?"."""
    return re.sub(r"\*\*|\\|[\s.·,;:~]+", "", text).lower()


def _clean(text: str) -> str:
    return " ".join(_MATH.sub("(수식)", text).split())


def _bold(sentence: str, surface: str) -> str:
    if not surface:
        return sentence
    pattern = re.compile(rf"(?<![A-Za-z]){re.escape(surface)}(?![A-Za-z])", re.IGNORECASE)
    return pattern.sub(lambda m: f"**{m.group(0)}**", sentence, count=1)


def _width(line: str) -> int:
    """Indentation width, a tab counting as four spaces as Obsidian draws it."""
    stripped = line[: len(line) - len(line.lstrip())]
    return len(stripped.expandtabs(4))


def _safe_id(word: str) -> str:
    return re.sub(r"[^a-z0-9\-]", "", word.lower().replace("'", "").replace("’", "")) or "word"


class _Note:
    """One vocabulary file as lines, remembering its newline style."""

    def __init__(self, path: Path, lines: list[str], newline: str, bom: bool, trailing: bool) -> None:
        self.path = path
        self.lines = lines
        self.newline = newline
        self.bom = bom
        self.trailing = trailing
        self.dirty = False

    @classmethod
    def load(cls, path: Path) -> _Note:
        if not path.is_file():
            return cls(path, [], "\n", False, True)
        raw = path.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        trailing = text.endswith("\n")
        lines = text.split("\n")
        if trailing:
            lines.pop()
        return cls(path, lines, newline, bom, trailing)

    def save(self) -> None:
        text = self.newline.join(self.lines) + (self.newline if self.trailing else "")
        write_text_atomic(self.path, ("﻿" if self.bom else "") + text)

    def _tops(self) -> list[tuple[int, str]]:
        found = []
        for index, line in enumerate(self.lines):
            match = _TOP.match(line)
            if match:
                found.append((index, match.group("word").lower()))
        return found

    def find(self, word: str) -> int | None:
        key = word.lower()
        return next((index for index, top in self._tops() if top == key), None)

    def _block(self, at: int) -> list[str]:
        end = next((i for i, _ in self._tops() if i > at), len(self.lines))
        return self.lines[at + 1 : end]

    def block_id(self, at: int) -> str | None:
        match = _BLOCK_ID.search(self.lines[at])
        return match.group("id") if match else None

    def ko_link(self, at: int, ko_name: str) -> str | None:
        """The ko id this English entry already links to, if any."""
        pattern = re.compile(rf"\[\[{re.escape(ko_name)}#\^([A-Za-z0-9\-]+)\|ko\]\]")
        for line in self._block(at):
            match = pattern.search(line)
            if match:
                return match.group(1)
        return None

    def eng_link(self, at: int, en_name: str) -> bool:
        needle = f"[[{en_name}#^"
        return any(needle in line and "|eng]]" in line for line in self._block(at))

    def add_example(self, at: int, entry: dict[str, Any], example: str, example_ko: str, surface: str) -> bool:
        """Put the example under the in-sentence meaning this block already lists.

        It goes after that meaning's existing examples, and not at all when
        the block already holds the same sentence.
        """
        if not example.strip():
            return False
        end = next((i for i, _ in self._tops() if i > at), len(self.lines))
        context = _norm(str(entry.get("in_context") or ""))
        if any(_norm(_clean(example)) == _norm(line) for line in self.lines[at + 1 : end]):
            return False
        meaning = next(
            (i for i in range(at + 1, end) if (m := _NUMBERED.match(self.lines[i])) and _norm(m.group(2)) == context),
            None,
        )
        if meaning is None:
            return False
        indent = self.lines[meaning][: len(self.lines[meaning]) - len(self.lines[meaning].lstrip())]
        # The meaning's own lines are the ones indented deeper than it.
        stop = meaning + 1
        while stop < end and (not self.lines[stop].strip() or _width(self.lines[stop]) > _width(indent)):
            stop += 1
        while stop > meaning + 1 and not self.lines[stop - 1].strip():
            stop -= 1
        lines = [f"{indent}\t{_bold(_clean(example), surface)}"]
        if example_ko.strip():
            lines.append(f"{indent}\t{_clean(example_ko)}")
        self.lines[stop:stop] = lines
        self.dirty = True
        return True

    def append_meanings(
        self, at: int, entry: dict[str, Any], example: str, example_ko: str, surface: str
    ) -> int:
        """Add the meanings this entry's block does not list yet, at its end."""
        end = next((i for i, _ in self._tops() if i > at), len(self.lines))
        block = self.lines[at + 1 : end]
        numbered = [m for m in (_NUMBERED.match(line) for line in block) if m]
        have = {_norm(m.group(2)) for m in numbered}
        new = [m for m in _meanings(entry) if _norm(m) not in have]
        if not new:
            return 0
        lines: list[str] = []
        pos = _pos(entry)
        listed = {_POS_LINE.sub("", line).lower() for line in block}
        if pos and pos.lower() not in listed:
            lines.append(f"\t  {pos}")
        start = int(numbered[-1].group(1)) + 1 if numbered else 1
        lines += _numbered(new, start, entry, example, example_ko, surface)
        # Right after the block's last real line, leaving its blank lines below.
        while end > at + 1 and not self.lines[end - 1].strip():
            end -= 1
        self.lines[end:end] = lines
        self.dirty = True
        return len(new)

    def insert(self, word: str, block: list[str]) -> None:
        """Put ``block`` before the first top-level entry that sorts after ``word``."""
        key = word.lower()
        at = next((index for index, top in self._tops() if top > key), None)
        if at is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines.extend(block)
        else:
            self.lines[at:at] = block
        self.dirty = True
