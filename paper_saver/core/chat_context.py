"""What text goes with a question to GPT, and what has gone already.

A conversation is one Codex session (``codex exec resume``): the model sees
everything sent earlier in it again on every turn, and OpenAI's prompt cache
serves that repeated part cheaply. So text is sent *once per session* and a
later question carries only what is new: the page just turned to, a passage
of another paper the question touches, a file attached since. What was sent
is recorded in :class:`SessionState`, which lives in ``chat.json``.

Three scopes, as the reader picks them:

``page``
    The page in view.
``paper``
    This whole paper - a summary needs all of it.
``tabs``
    Every paper open in a tab - but only the page in view, each paper's
    abstract, and the paragraphs that match the question. Several whole
    papers on every comparison would be hundreds of thousands of tokens.

Sentence tags (``[S12]``) are numbered across the whole session, so a tag
in text sent three questions ago still names the same sentence. A sentence
of another paper is named ``<paper key>|<sentence id>``.

Matching is lexical (BM25) over each paragraph's English *and* its Korean
translation: the reader asks in Korean about English papers, and Hangul is
indexed by character pairs so "어텐션은" still matches "어텐션".
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .models import BlockKind, PaperLayout

__all__ = [
    "ABSTRACT_CHARS", "RELATED_CHARS", "ContextBuilder", "PaperSource", "SessionState", "rank", "tokens",
]

#: The opening of each other paper, sent once, so a comparison has both sides.
ABSTRACT_CHARS = 1500
#: Matching paragraphs per question across all open papers, before sharing.
RELATED_CHARS = 36_000
#: A ref separator: another paper's key, then the sentence id.
REF_SEPARATOR = "|"

_MATH = re.compile("⟦m\\d+⟧")


def _plain(text: str) -> str:
    return _MATH.sub("[수식]", text or "")


# -- session state -------------------------------------------------------------------


@dataclass
class SessionState:
    """One Codex session: its id, what has been sent, and the tag numbers."""

    id: str = ""
    engine: str = ""
    sent: list[str] = field(default_factory=list)
    #: Tag number -> sentence ref ("p0b1s1", or "<key>|p0b1s1").
    numbering: dict[int, str] = field(default_factory=dict)

    def copy(self) -> SessionState:
        return SessionState(self.id, self.engine, list(self.sent), dict(self.numbering))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "engine": self.engine, "sent": self.sent,
                "numbering": {str(k): v for k, v in self.numbering.items()}}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        numbering = {}
        for key, value in (data.get("numbering") or {}).items():
            if str(key).isdigit():
                numbering[int(key)] = str(value)
        return cls(str(data.get("id") or ""), str(data.get("engine") or ""),
                   [str(s) for s in data.get("sent") or []], numbering)


@dataclass
class PaperSource:
    """An open paper, as the context builder needs it."""

    #: "" for the paper the conversation belongs to; another paper's key otherwise.
    key: str
    title: str
    layout: PaperLayout
    #: Sentence id -> its Korean translation, if there is one yet.
    translation: Callable[[str], str | None] = lambda _id: None


# -- matching --------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9\-]*|\d+(?:\.\d+)?|[가-힣]+")
_STOP = frozenset(
    """a an the of in on for to and or is are was were be been by with as at from that this these those
    it its we our they their which what how why when where who whom can could would should may might
    than then there here also into using use used such via not no do does did has have had""".split()
)
#: Korean question words and endings that say nothing about the content.
_KOREAN_STOP = frozenset("무엇 뭐야 뭔가 어떻게 어떤 왜 설명 설명해 알려 해줘 주세요 하는 있는 이것 그것 이 그 저 논문".split())


def tokens(text: str) -> list[str]:
    """Words for matching: English lower-cased and lightly stemmed, Hangul
    as character pairs (Korean glues particles to words)."""
    out: list[str] = []
    for word in _WORD.findall(text):
        if "가" <= word[0] <= "힣":
            if word in _KOREAN_STOP:
                continue
            out += [word[i : i + 2] for i in range(len(word) - 1)] if len(word) > 1 else [word]
            continue
        word = word.lower()
        if word in _STOP or len(word) < 2:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


def rank(query: str, passages: Sequence[str], *, k1: float = 1.4, b: float = 0.75) -> list[float]:
    """A BM25 score for each passage against ``query`` (0 for no match)."""
    wanted = set(tokens(query))
    docs = [Counter(tokens(p)) for p in passages]
    if not wanted or not docs:
        return [0.0] * len(passages)
    average = sum(sum(d.values()) for d in docs) / len(docs) or 1.0
    frequency = Counter(t for d in docs for t in set(d) if t in wanted)
    scores = []
    for doc in docs:
        length = sum(doc.values())
        score = 0.0
        for term in wanted:
            count = doc.get(term, 0)
            if not count:
                continue
            idf = math.log(1 + (len(docs) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * count * (k1 + 1) / (count + k1 * (1 - b + b * length / average))
        scores.append(score)
    return scores


# -- building ---------------------------------------------------------------------------


class ContextBuilder:
    """Turns pages, papers, passages and files into prompt text, once each."""

    def __init__(self, state: SessionState) -> None:
        self.state = state
        self._sent = set(state.sent)
        self._number_of = {ref: number for number, ref in state.numbering.items()}

    # -- bookkeeping ---------------------------------------------------------------

    def _fresh(self, key: str) -> bool:
        return key not in self._sent

    def _mark(self, key: str) -> None:
        if key not in self._sent:
            self._sent.add(key)
            self.state.sent.append(key)

    def _tag(self, ref: str) -> str:
        number = self._number_of.get(ref)
        if number is None:
            number = max(self.state.numbering, default=0) + 1
            self.state.numbering[number] = ref
            self._number_of[ref] = number
        return f"[S{number}]"

    @staticmethod
    def _ref(paper: PaperSource, sentence_id: str) -> str:
        return f"{paper.key}{REF_SEPARATOR}{sentence_id}" if paper.key else sentence_id

    def _block_text(self, paper: PaperSource, block: Any) -> str:
        if not (block.translatable and block.sentences):
            return _plain(block.text)
        return " ".join(f"{self._tag(self._ref(paper, s.id))} {_plain(s.text)}" for s in block.sentences)

    def _page_body(self, paper: PaperSource, number: int) -> str:
        blocks = [b for b in paper.layout.pages[number].blocks if b.text.strip()]
        return "\n\n".join(self._block_text(paper, b) for b in blocks)

    # -- pieces ----------------------------------------------------------------------

    def page(self, paper: PaperSource, number: int, label: str = "") -> str | None:
        """The page in view, unless it (or the whole paper) went already."""
        pid = paper.key or "self"
        if not 0 <= number < paper.layout.page_count:
            return None
        if not self._fresh(f"paper:{pid}") or not self._fresh(f"page:{pid}:{number}"):
            return None
        self._mark(f"page:{pid}:{number}")
        body = self._page_body(paper, number)
        return f"{label}독자가 보고 있는 쪽(p.{number + 1})의 본문:\n<<<\n{body}\n>>>" if body.strip() else None

    def paper(self, paper: PaperSource, label: str = "", limit: int = 400_000) -> str | None:
        """Every page of a paper, once per session."""
        pid = paper.key or "self"
        if not self._fresh(f"paper:{pid}"):
            return None
        self._mark(f"paper:{pid}")
        pages = []
        for number in range(paper.layout.page_count):
            body = self._page_body(paper, number)
            if body.strip():
                pages.append(f"[p.{number + 1}]\n{body}")
        text = "\n\n".join(pages)
        if len(text) > limit:
            text = text[:limit] + " …(이하 생략)"
        return f"{label}논문 전체 본문 (쪽마다 [p.번호]로 시작합니다):\n<<<\n{text}\n>>>"

    def related(self, papers: Sequence[PaperSource], query: str, labels: Sequence[str],
                budget: int = RELATED_CHARS) -> list[str]:
        """For each paper: its abstract (once), then the paragraphs that match
        ``query`` best and have not been sent, within an equal share."""
        share = budget // max(1, len(papers))
        out = []
        for paper, label in zip(papers, labels):
            pid = paper.key or "self"
            if not self._fresh(f"paper:{pid}"):
                continue  # all of it went already
            parts = []
            if self._fresh(f"abstract:{pid}"):
                self._mark(f"abstract:{pid}")
                opening = self._abstract(paper, pid)
                if opening:
                    parts.append(f"(앞부분)\n{opening}")
            candidates = list(self._passages(paper))
            scores = rank(query, [text for _, _, _, text in candidates])
            used = 0
            chosen = []
            for score, (order, page, block, _) in sorted(zip(scores, candidates), key=lambda x: -x[0]):
                if score <= 0 or used >= share:
                    break
                key = f"block:{pid}:{block.id}"
                if not self._fresh(key):
                    continue
                text = self._block_text(paper, block)
                if used + len(text) > share and chosen:
                    continue
                self._mark(key)
                used += len(text)
                chosen.append((order, page, text))
            # Reading order, each with its page, so GPT can say where.
            for _, page, text in sorted(chosen):
                parts.append(f"(p.{page + 1}) {text}")
            matched = any(score > 0 for score in scores)
            if not matched and self._fresh(f"outline:{pid}"):
                # Nothing matched - typically a paper not translated yet, asked
                # about in Korean. Its outline is a map GPT can still use.
                self._mark(f"outline:{pid}")
                outline = self._outline(paper, candidates, share)
                if outline:
                    parts.append(f"(질문과 겹치는 단어가 없어 각 문단의 첫 문장으로 된 개요를 보냅니다)\n{outline}")
            if parts:
                out.append(f"{label}질문과 관련된 부분:\n<<<\n" + "\n\n".join(parts) + "\n>>>")
        return out

    def document(self, file_id: str, header: str, text: str, limit: int) -> str | None:
        """An attached file, once per session."""
        if not self._fresh(f"file:{file_id}"):
            return None
        self._mark(f"file:{file_id}")
        text = text.strip()
        if len(text) > limit:
            text = text[:limit] + " …(이하 생략)"
        return f"{header}\n<<<\n{text or '(읽을 수 있는 글자가 없습니다)'}\n>>>"

    # -- helpers ------------------------------------------------------------------------

    def _abstract(self, paper: PaperSource, pid: str) -> str:
        """The first body paragraphs of the first page, tagged; recorded as
        sent paragraph by paragraph, so a match later does not repeat them."""
        if not paper.layout.pages:
            return ""
        texts, used = [], 0
        for block in paper.layout.pages[0].blocks:
            if block.kind != BlockKind.BODY or not block.sentences:
                continue
            self._mark(f"block:{pid}:{block.id}")
            text = self._block_text(paper, block)
            texts.append(text)
            used += len(text)
            if used >= ABSTRACT_CHARS:
                break
        return "\n\n".join(texts)

    def _outline(self, paper: PaperSource, candidates: Sequence[tuple[int, int, Any, str]], limit: int) -> str:
        """Section headings and each paragraph's first sentence, tagged."""
        lines, used, heading = [], 0, None
        for _, page, block, text in candidates:
            section = text.split("\n", 1)[0]
            if section and section != heading:
                heading = section
                lines.append(f"## {_plain(section)}")
            first = block.sentences[0]
            line = f"(p.{page + 1}) {self._tag(self._ref(paper, first.id))} {_plain(first.text)}"
            if used + len(line) > limit:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    @staticmethod
    def _passages(paper: PaperSource) -> Iterable[tuple[int, int, Any, str]]:
        """Each translatable paragraph in reading order, with its page and what
        it is matched by: its section heading, its English and its Korean."""
        heading = ""
        order = 0
        for page in paper.layout.pages:
            for block in page.blocks:
                if block.kind == BlockKind.HEADING:
                    heading = block.text
                    continue  # matched as part of the paragraphs under it
                if not (block.translatable and block.sentences) or block.kind == BlockKind.TITLE:
                    continue
                korean = " ".join(t for s in block.sentences if (t := paper.translation(s.id)))
                order += 1
                yield order, page.number, block, f"{heading}\n{block.text}\n{korean}"
