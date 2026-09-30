"""Word lookup in context, cached for good (§3.1, §3.2).

A lookup is keyed by the word *and* the sentence it was clicked in, because
the sentence is the point: "attention" in a transformer paper and "attention"
in a psychology paper are different entries. The cache is per user, not per
paper - a word looked up in one paper is instant in the next one that uses it
the same way.

When the exact sentence has not been seen but the word has, the earlier entry
is offered straight away, marked as coming from another sentence, while the
contextual lookup runs. The reader is almost never left staring at a spinner.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any

from ..core.atomic_io import read_json, write_json_atomic
from .engine import TranslationEngine, TranslationError
from .prompts import lookup_prompt, parse_lookup

log = logging.getLogger(__name__)

__all__ = ["Dictionary", "clean_word"]

#: Keep the cache from growing without bound; the oldest entries go first.
MAX_ENTRIES = 20000


def clean_word(raw: str) -> str:
    """The dictionary form of a clicked token: no punctuation, no citations.

    "networks," -> "networks", "(RGNN)" -> "RGNN", "model's" -> "model".
    Returns "" for things that are not words - formula placeholders, numbers,
    citation brackets - so the caller can ignore the click.
    """
    if "⟦" in raw:
        return ""
    word = re.sub(r"^[^\w]+|[^\w]+$", "", raw)
    word = re.sub(r"['’]s$", "", word)
    word = word.replace("­", "")
    if not word or word.isdigit() or not re.search(r"[A-Za-z]", word):
        return ""
    return word


def _key(word: str, sentence: str) -> str:
    digest = hashlib.sha1(" ".join(sentence.split()).encode("utf-8")).hexdigest()[:12]
    return f"{word.lower()}|{digest}"


class Dictionary:
    def __init__(self, cache_path: Path) -> None:
        self.cache_path = cache_path
        self._lock = threading.Lock()
        try:
            data = read_json(cache_path) if cache_path.is_file() else {}
        except (OSError, ValueError):
            log.warning("Dictionary cache unreadable; starting empty", exc_info=True)
            data = {}
        self._entries: dict[str, dict[str, Any]] = dict(data.get("entries", {}))

    def cached(self, word: str, sentence: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(_key(word, sentence))
            return dict(entry) if entry else None

    def related(self, word: str) -> dict[str, Any] | None:
        """The most recent entry for this word from any sentence."""
        prefix = f"{word.lower()}|"
        with self._lock:
            matches = [e for k, e in self._entries.items() if k.startswith(prefix)]
        return dict(matches[-1]) if matches else None

    def lookup(
        self, word: str, sentence: str, engine: TranslationEngine, *, cancel: threading.Event
    ) -> dict[str, Any]:
        """The contextual entry, from cache or from GPT. Raises TranslationError."""
        found = self.cached(word, sentence)
        if found is not None:
            return found
        answer = engine.translate(lookup_prompt(word, sentence), cancel=cancel)
        entry = parse_lookup(answer)
        if entry is None:
            raise TranslationError("사전 응답을 읽지 못했습니다. 다시 시도해 주세요.")
        entry["word"] = word
        self._store(_key(word, sentence), entry)
        return entry

    def _store(self, key: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > MAX_ENTRIES:
                self._entries.pop(next(iter(self._entries)))
            snapshot = {"entries": dict(self._entries)}
        try:
            write_json_atomic(self.cache_path, snapshot, indent=0)
        except OSError:
            log.warning("Could not save the dictionary cache", exc_info=True)
