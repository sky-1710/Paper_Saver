"""Translating a paper: batching, running, retrying, saving (§5).

Batches never cross a page. A page is the unit the reader sees fill in, and
the unit that is saved, so a finished batch means a finished (part of a) page
on screen within a second. Pages are taken starting from the one the reader
is looking at, so the translation they are waiting for is the first to come.

Several batches run at once. Each Codex run spends most of its minute waiting
on the model, not on this machine, and three in flight finish a 12-page paper
in roughly a third of the time without being rude to the rate limit.

Nothing is lost to a failure. Every batch is written to ``translation.json``
the moment it is parsed; a sentence that did not come back is simply still
missing, and running the translation again asks only for what is missing.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from ..core.models import BlockKind, PaperLayout
from ..core.project import TranslationStore
from .engine import (
    EngineAuthError,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)
from .prompts import SentenceRequest, batch_prompt, parse_batch

log = logging.getLogger(__name__)

__all__ = ["Batch", "Progress", "pending_batches", "translate_paper"]

#: English characters per request. Large enough that the rules at the top of
#: the prompt are a small overhead, small enough that one failure costs little
#: and a page appears in pieces rather than all at the end.
MAX_BATCH_CHARS = 3200
DEFAULT_PARALLEL = 3

_KIND = {
    BlockKind.TITLE: "title",
    BlockKind.HEADING: "heading",
    BlockKind.CAPTION: "caption",
}


@dataclass(frozen=True, slots=True)
class Batch:
    page: int
    sentences: tuple[SentenceRequest, ...]

    @property
    def ids(self) -> list[str]:
        return [s.id for s in self.sentences]


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int
    #: The page a batch just finished on, for the UI to repaint.
    page: int | None = None
    message: str = ""


def pending_batches(
    layout: PaperLayout,
    store: TranslationStore,
    *,
    start_page: int = 0,
    pages: Sequence[int] | None = None,
    max_chars: int = MAX_BATCH_CHARS,
) -> list[Batch]:
    """Batches for every sentence not yet translated, current page first."""
    count = layout.page_count
    order = [(start_page + i) % count for i in range(count)] if count else []
    if pages is not None:
        wanted = set(pages)
        order = [n for n in order if n in wanted]

    by_number = {page.number: page for page in layout.pages}
    batches: list[Batch] = []
    for number in order:
        page = by_number.get(number)
        if page is None:
            continue
        current: list[SentenceRequest] = []
        size = 0
        for block in page.blocks:
            if not block.translatable:
                continue
            kind = _KIND.get(block.kind, "body")
            for sentence in block.sentences:
                if sentence.id in store:
                    continue
                if current and size + len(sentence.text) > max_chars:
                    batches.append(Batch(number, tuple(current)))
                    current, size = [], 0
                current.append(SentenceRequest(sentence.id, sentence.text, kind))
                size += len(sentence.text)
        if current:
            batches.append(Batch(number, tuple(current)))
    return batches


def _run_batch(
    batch: Batch, engine: TranslationEngine, title: str, cancel: threading.Event
) -> dict[str, str]:
    """One batch, with one retry for whatever the first answer left out."""
    answer = engine.translate(batch_prompt(batch.sentences, context=title), cancel=cancel)
    result = parse_batch(answer, batch.ids)
    missing = [s for s in batch.sentences if s.id not in result]
    if missing and not cancel.is_set():
        log.info("Page %s: %d of %d sentences missing; asking again", batch.page + 1, len(missing), len(batch.sentences))
        retry = engine.translate(batch_prompt(missing, context=title), cancel=cancel)
        result.update(parse_batch(retry, [s.id for s in missing]))
    return result


def translate_paper(
    layout: PaperLayout,
    store: TranslationStore,
    engine: TranslationEngine,
    *,
    title: str = "",
    start_page: int = 0,
    pages: Sequence[int] | None = None,
    parallel: int = DEFAULT_PARALLEL,
    cancel: threading.Event,
    on_progress: Callable[[Progress], None] = lambda _p: None,
) -> Progress:
    """Translate everything still missing. Returns the final progress.

    Raises :class:`TranslationError` subclasses for failures that stop the run
    (not signed in, usage limit). A single batch failing for another reason is
    logged and skipped; its sentences stay missing for the next run.
    """
    batches = pending_batches(layout, store, start_page=start_page, pages=pages)
    total = sum(len(b.sentences) for b in batches)
    done = 0
    on_progress(Progress(done, total, message="번역 준비 중…"))
    if not batches:
        return Progress(0, 0, message="번역할 문장이 없습니다.")

    model = getattr(engine, "model", "") or ""
    failures: list[str] = []
    queue = list(batches)
    running: dict[Future[dict[str, str]], Batch] = {}

    with ThreadPoolExecutor(max_workers=max(1, parallel), thread_name_prefix="translate") as pool:
        try:
            while queue or running:
                while queue and len(running) < max(1, parallel) and not cancel.is_set():
                    batch = queue.pop(0)
                    running[pool.submit(_run_batch, batch, engine, title, cancel)] = batch
                if not running:
                    break
                finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for future in finished:
                    batch = running.pop(future)
                    try:
                        result = future.result()
                    except (TranslationCancelled, EngineAuthError, EngineRateLimited):
                        raise
                    except TranslationError as exc:
                        log.warning("Batch on page %s failed: %s", batch.page + 1, exc)
                        failures.append(str(exc))
                        continue
                    if result:
                        store.update(result, engine=engine.key, model=model)
                    done += len(result)
                    on_progress(
                        Progress(done, total, page=batch.page, message=f"{batch.page + 1}쪽 번역됨")
                    )
                if cancel.is_set():
                    raise TranslationCancelled("번역을 멈췄습니다.")
        except BaseException:
            cancel.set()  # stop the batches still running before re-raising
            raise

    message = "번역 완료" if done >= total else f"{total - done}문장이 번역되지 않았습니다. 다시 실행하면 이어서 번역합니다."
    if failures and done == 0:
        raise TranslationError(failures[0])
    return Progress(done, total, message=message)
