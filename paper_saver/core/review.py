"""One-page reviews and Cornell notes of a paper, written by GPT (§4d).

    <project>/review.json     both documents, as Markdown, and the sentences they cite

A document is Markdown the reader may edit as freely as the note. What it says
rests on the paper sentence by sentence: every point ends in links naming the
sentences it was drawn from, one per page::

    - 순환 구조 없이 어텐션만으로 번역한다. [p.2](ref:p1b4s2,p1b4s3)

so clicking the point shows those sentences in the PDF, and the link is still
there, and still right, after the reader rewrites the point around it.

GPT is shown the paper with every sentence tagged ``[S12]`` and answers with
JSON held to a schema, each point carrying the numbers it rests on;
:func:`review_markdown` is the one gate an answer passes through. It checks
the shape, drops numbers the paper did not have, insists that most points
cite something - a review that cannot be clicked back to the paper is not
what was asked for - and writes the Markdown.

The links name sentence ids, which are positions and move when the layout is
re-extracted. The text of every sentence cited is kept beside the documents,
and :meth:`ReviewStore.bind` moves a link back onto the sentence it was made
for, the way notes and highlights are re-anchored (``Project.bind``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic
from .chat import resolve_citations
from .geometry import mostly_landscape
from .models import BlockKind, PaperLayout

log = logging.getLogger(__name__)

__all__ = [
    "KINDS",
    "MODES",
    "PAPER_CHARS",
    "ReviewDoc",
    "ReviewFormatError",
    "ReviewStore",
    "Segment",
    "cite",
    "looks_like_slides",
    "paper_text",
    "plain_citations",
    "refs_in",
    "review_markdown",
    "split_citations",
]

#: The documents there are, by key, as the reader sees them named.
KINDS = {"one_page": "한 장 요약", "cornell": "코넬 노트"}
#: How a document is written: for the exam, or as a reader's review of a paper.
MODES = {"lecture": "강의 모드", "paper": "논문 모드"}
#: The paper text sent, at most: a long paper without its reference list is
#: 60-150k characters.
PAPER_CHARS = 300_000
#: A point names at most this many sentences: past that it is not evidence.
MAX_REFS = 6
#: The share of points that must cite the paper for an answer to be taken.
MIN_CITED = 0.5


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _plain(text: str) -> str:
    return re.sub("⟦m\\d+⟧", "[수식]", text or "")


def _norm(text: str) -> str:
    return " ".join(text.split())


# -- what GPT is shown ---------------------------------------------------------------

_REFERENCES = re.compile(r"^\s*(?:[\dIVX]+\.?\s*)?(?:references|bibliography|참고\s*문헌)\s*$", re.IGNORECASE)
#: Under the reference list the layout files everything as verbatim, the
#: appendix's own headings too: "A Probability Change at the Critical Chunk",
#: "B.2 Prompts", "Appendix". A reference entry is longer, and has a year.
_APPENDIX = re.compile(r"^(?:(?i:appendix|appendices|supplementary)\b.*|[A-H](?:\.\d+)*\.?\s+[A-Z][^.]*)$")
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
#: A running head or foot that says only where it is: "12", "Page 3", "iv".
_PAGE_NUMBER = re.compile(r"^\s*(?:page\s*)?(?:\d+|[ivxlc]+)(?:\s*(?:/|of)\s*\d+)?\s*$", re.IGNORECASE)
#: How the text that has no sentences to cite is introduced.
GRAPHIC_LABEL = "(그림·표 속 글)"
STRAY_LABEL = "(따로 읽은 글)"
#: How what the professor said over a page is introduced, under that page.
SPOKEN_LABEL = "(강의 STT — 이 쪽을 설명한 부분)"


def looks_like_slides(layout: PaperLayout) -> bool:
    """Whether the PDF is a slide deck: most of its pages are landscape."""
    return mostly_landscape((page.width, page.height) for page in layout.pages)


def _appendix_heading(text: str) -> bool:
    line = _norm(text)
    return len(line) <= 80 and not _YEAR.search(line) and bool(_APPENDIX.match(line))


def paper_text(
    layout: PaperLayout,
    *,
    strays: Mapping[int, list[str]] | None = None,
    spoken: Mapping[int, str] | None = None,
    notes: Mapping[int, str] | None = None,
    until_references: bool = False,
    limit: int = PAPER_CHARS,
) -> tuple[str, dict[int, str]]:
    """The paper as GPT reads it for a review, and what each tag stands for.

    All of the text goes but page numbers and a paper's reference list. That
    is more than a paper's summary needs - and what a lecture's slides are
    made of: their tables, diagrams, code and the lines the layout calls
    verbatim (Korean on an English slide) are the content. ``strays`` adds,
    by page, the lines no block holds (``pdf.text.stray_lines``): a slide's
    text box the layout could not read is still the slide. ``spoken`` adds,
    by page, what the lecture's STT said over it (``core.lecture.
    transcript_by_page``), under :data:`SPOKEN_LABEL`; ``limit`` is for the
    document's own text and the STT comes on top of it. ``notes`` says, by
    page, what the lecture made of it - how long it stayed there and how
    often it came back - written after the page's mark:
    ``[p.24] (강의 5분 26초 · 6번 띄움)``.

    Every page is headed ``[p.N]`` and every sentence GPT may cite is tagged
    ``[S1]``, ``[S2]``… across the whole paper. Text inside figures has no
    sentences, so it goes untagged under :data:`GRAPHIC_LABEL`, and the stray
    lines under :data:`STRAY_LABEL`. With ``until_references`` the text stops
    at the reference list, leaving out the appendices - where the quoted
    prompts that make OpenAI refuse a paper usually are.
    """
    numbering: dict[int, str] = {}
    pages: list[str] = []
    in_references = False
    for page in layout.pages:
        paragraphs: list[str] = []
        figure: list[str] = []
        stop = False
        for block in page.blocks:
            # The layout files the "References" line as a heading or, as often,
            # as verbatim like the list under it; the next heading ends the list.
            if _REFERENCES.match(block.text):
                in_references = True
                if until_references:
                    stop = True
                    break
                continue
            if block.kind == BlockKind.HEADING or (in_references and _appendix_heading(block.text)):
                in_references = False
            if not block.text.strip() or (block.kind == BlockKind.FURNITURE and _PAGE_NUMBER.match(block.text)):
                continue
            if block.kind == BlockKind.VERBATIM and in_references:
                continue  # the reference list: a tenth of a paper, nothing to review
            if block.kind == BlockKind.GRAPHIC:
                figure.append(_plain(block.text))
                continue
            if figure:
                paragraphs.append(f"{GRAPHIC_LABEL}\n" + "\n".join(figure))
                figure = []
            if not block.translatable:
                paragraphs.append(_plain(block.text))
                continue
            parts = []
            for sentence in block.sentences:
                numbering[len(numbering) + 1] = sentence.id
                parts.append(f"[S{len(numbering)}] {_plain(sentence.text)}")
            paragraphs.append(" ".join(parts))
        if figure:
            paragraphs.append(f"{GRAPHIC_LABEL}\n" + "\n".join(figure))
        if strays and not stop and strays.get(page.number):
            paragraphs.append(f"{STRAY_LABEL}\n" + "\n".join(_plain(line) for line in strays[page.number]))
        if spoken and not stop and spoken.get(page.number, "").strip():
            paragraphs.append(f"{SPOKEN_LABEL}\n{spoken[page.number].strip()}")
        if paragraphs:
            note = (notes or {}).get(page.number)
            mark = f"[p.{page.number + 1}]" + (f" ({note})" if note else "")
            pages.append(f"{mark}\n" + "\n\n".join(paragraphs))
        if stop:
            break
    text = "\n\n".join(pages)
    limit += sum(len(SPOKEN_LABEL) + len(said) + 3 for said in (spoken or {}).values())
    if len(text) > limit:
        text = text[:limit] + " …(이하 생략)"
    return text, numbering


# -- citation links ------------------------------------------------------------------

#: ``[p.3](ref:p2b4s1,p2b4s2)``: a label, and the sentence ids it stands for.
_REF_LINK = re.compile(r"\[([^\]\n]*)\]\(ref:([^)\s]*)\)")
#: A run of them with the space before it, as one citation.
_REF_RUN = re.compile(r"(?:[ \t]*\[[^\]\n]*\]\(ref:[^)\s]*\))+")


def cite(ids: list[str], page_of: Callable[[str], int | None]) -> str:
    """Sentences as citation links, one per page, with a space before them:
    ``" [p.3](ref:a,b) [p.4](ref:c)"``. Ids on no page are left out."""
    pages: dict[int, list[str]] = {}
    for sentence_id in ids:
        page = page_of(sentence_id)
        if page is not None:
            pages.setdefault(page, []).append(sentence_id)
    return "".join(f" [p.{page + 1}](ref:{','.join(group)})" for page, group in pages.items())


@dataclass
class Segment:
    """A stretch of a line and the sentences cited at its end."""

    text: str
    refs: list[str] = field(default_factory=list)
    #: The citations' labels as written ("p.3").
    labels: list[str] = field(default_factory=list)


def split_citations(line: str) -> list[Segment]:
    """A line cut after each citation, so each part links to what it cites.

    ``"A. [p.1](ref:x) B. [p.2](ref:y)"`` is ``A.`` citing ``x`` and ``B.``
    citing ``y``. Links side by side cite together; text after the last link
    cites nothing.
    """
    segments: list[Segment] = []
    position = 0
    for match in _REF_LINK.finditer(line):
        before = line[position : match.start()]
        ids = [i for i in match.group(2).split(",") if i]
        if segments and segments[-1].refs and not before.strip():
            segments[-1].refs += [i for i in ids if i not in segments[-1].refs]
            segments[-1].labels.append(match.group(1))
        else:
            segments.append(Segment(before, ids, [match.group(1)]))
        position = match.end()
    tail = line[position:]
    if tail.strip() or not segments:
        segments.append(Segment(tail))
    return segments


def refs_in(text: str) -> list[str]:
    """Every sentence id cited in ``text``, in order, once each."""
    found: dict[str, None] = {}
    for match in _REF_LINK.finditer(text):
        for sentence_id in match.group(2).split(","):
            if sentence_id:
                found.setdefault(sentence_id)
    return list(found)


def plain_citations(text: str) -> str:
    """Citation links as plain text, ``" (p.3, p.4)"``: for the note and the
    clipboard, where a ``ref:`` link would lead nowhere."""

    def plain(match: re.Match[str]) -> str:
        labels = [label for label, _ in _REF_LINK.findall(match.group(0)) if label.strip()]
        return f" ({', '.join(labels)})" if labels else ""

    return _REF_RUN.sub(plain, text)


# -- GPT's answer, as Markdown ---------------------------------------------------------


class ReviewFormatError(ValueError):
    """An answer that is not what was asked for. ``str(err)`` says what is wrong."""


_NUMBER = re.compile(r"\d+")
_MARKERS = re.compile(r"\s*⟦r\d+⟧")


@dataclass
class _Point:
    text: str
    refs: list[str]


class _Reader:
    """Turns the answer's points into text and sentence ids, counting citations."""

    def __init__(self, numbering: Mapping[int, str], page_of: Callable[[str], int | None]) -> None:
        self.numbering = numbering
        self.page_of = page_of
        self.points = 0
        self.cited = 0

    def point(self, item: Any, where: str) -> _Point:
        if not isinstance(item, dict):
            raise ReviewFormatError(f"{where}: 객체가 아닙니다.")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ReviewFormatError(f"{where}: text가 비어 있습니다.")
        # [S12] written into the text instead of sources still counts.
        text, inline = resolve_citations(_norm(text), self.numbering)
        text = _MARKERS.sub("", text).strip()
        refs: list[str] = []
        sources = item.get("sources")
        for source in sources if isinstance(sources, list) else []:
            match = _NUMBER.search(str(source))
            sentence_id = self.numbering.get(int(match.group(0))) if match else None
            if sentence_id is not None and sentence_id not in refs:
                refs.append(sentence_id)
        refs += [i for i in inline if i not in refs]
        refs = [i for i in refs if self.page_of(i) is not None][:MAX_REFS]
        self.points += 1
        self.cited += bool(refs)
        return _Point(text, refs)

    def points_of(self, items: Any, where: str, *, least: int = 1) -> list[_Point]:
        if not isinstance(items, list) or len(items) < least:
            raise ReviewFormatError(f"{where}: 항목이 {least}개 이상 있어야 합니다.")
        return [self.point(item, f"{where} {n}") for n, item in enumerate(items, start=1)]

    def line(self, point: _Point) -> str:
        return point.text + cite(point.refs, self.page_of)

    def check(self) -> None:
        if self.points and self.cited / self.points < MIN_CITED:
            raise ReviewFormatError(
                f"근거 문장 번호(sources)가 달린 항목이 {self.points}개 중 {self.cited}개뿐입니다. "
                "항목마다 그 내용이 나온 [S번호]를 sources에 넣으세요."
            )


def _heading(item: Any, key: str, where: str) -> str:
    value = item.get(key) if isinstance(item, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ReviewFormatError(f"{where}: {key}가 비어 있습니다.")
    return _norm(value).lstrip("#").strip()


def review_markdown(
    kind: str,
    data: Any,
    *,
    numbering: Mapping[int, str],
    page_of: Callable[[str], int | None],
    title: str = "",
) -> str:
    """A checked answer as the Markdown document the reader sees and edits.

    ``numbering`` maps the tags the paper was sent with to sentence ids, and
    ``page_of`` says which page a sentence is on (``None``: not in this paper).
    Raises :class:`ReviewFormatError`.
    """
    if kind not in KINDS:
        raise ReviewFormatError(f"알 수 없는 문서 종류 '{kind}'.")
    if not isinstance(data, dict):
        raise ReviewFormatError("답이 JSON 객체가 아닙니다.")
    reader = _Reader(numbering, page_of)
    head = f"# {KINDS[kind]}" + (f" · {_norm(title)}" if title.strip() else "")
    lines = [head, ""]
    if kind == "one_page":
        one_liner = reader.point(data.get("one_liner"), "one_liner")
        sections = data.get("sections")
        if not isinstance(sections, list) or len(sections) < 3:
            raise ReviewFormatError("sections가 3개 이상 있어야 합니다.")
        lines += [f"> **한 줄 요약** {reader.line(one_liner)}", ""]
        for number, section in enumerate(sections, start=1):
            where = f"section {number}"
            heading = _heading(section, "heading", where)
            points = reader.points_of(section.get("points"), f"{where} points")
            lines += [f"## {heading}", *(f"- {reader.line(p)}" for p in points), ""]
    else:
        rows = data.get("rows")
        if not isinstance(rows, list) or len(rows) < 3:
            raise ReviewFormatError("rows(단서와 노트)가 3개 이상 있어야 합니다.")
        for number, row in enumerate(rows, start=1):
            where = f"row {number}"
            cue = _heading(row, "cue", where)
            notes = reader.points_of(row.get("notes"), f"{where} notes")
            lines += [f"### {cue}", *(f"- {reader.line(p)}" for p in notes), ""]
        summary = reader.points_of(data.get("summary"), "summary")
        lines += ["## 요약", " ".join(reader.line(p) for p in summary), ""]
    reader.check()
    return "\n".join(lines).rstrip() + "\n"


# -- the store -------------------------------------------------------------------------


@dataclass
class ReviewDoc:
    kind: str
    #: The Markdown, as last generated or edited.
    text: str
    generated: str = ""
    engine: str = ""
    model: str = ""
    #: When the reader last changed it; "" while it is as GPT wrote it.
    edited: str = ""
    #: The lecture STT files it was made with, by name.
    transcripts: list[str] = field(default_factory=list)
    #: The mode it was written in: "lecture" or "paper".
    mode: str = ""


class ReviewStore:
    """Both documents of a paper, saved whole and atomically on every change."""

    FILE = "review.json"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / self.FILE
        self.docs: dict[str, ReviewDoc] = {}
        #: Sentence id -> its text, for every sentence a document was made citing.
        self.anchors: dict[str, str] = {}
        #: The mode the reader chose for this paper; "" leaves it to the PDF.
        self.mode = ""
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = read_json(self.path)
            docs = data.get("documents") or {}
            for kind, item in docs.items():
                if kind in KINDS and isinstance(item, dict) and isinstance(item.get("text"), str):
                    fields = {k: str(item.get(k) or "") for k in ("generated", "engine", "model", "edited", "mode")}
                    heard = [str(name) for name in item.get("transcripts") or []]
                    self.docs[kind] = ReviewDoc(kind, item["text"], **fields, transcripts=heard)
            self.anchors = {str(k): str(v) for k, v in (data.get("anchors") or {}).items()}
            self.mode = str(data.get("mode") or "") if data.get("mode") in MODES else ""
        except (OSError, ValueError, AttributeError):
            backup = self.path.with_suffix(".json.damaged")
            log.exception("Could not read %s; keeping it as %s", self.path, backup.name)
            self.docs, self.anchors = {}, {}
            try:
                self.path.replace(backup)
            except OSError:
                pass

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "documents": {kind: {k: v for k, v in asdict(doc).items() if k != "kind"} for kind, doc in self.docs.items()},
                "anchors": self.anchors,
                "mode": self.mode,
            },
        )

    def get(self, kind: str) -> ReviewDoc | None:
        return self.docs.get(kind)

    def put(
        self,
        kind: str,
        text: str,
        *,
        anchors: Mapping[str, str],
        engine: str = "",
        model: str = "",
        transcripts: list[str] | None = None,
        mode: str = "",
    ) -> ReviewDoc:
        """A newly generated document, replacing the one before (edits and all)."""
        doc = ReviewDoc(
            kind, text, generated=_now(), engine=engine, model=model, transcripts=list(transcripts or []), mode=mode
        )
        self.docs[kind] = doc
        self.anchors.update({k: _norm(v) for k, v in anchors.items()})
        self.save()
        return doc

    def set_mode(self, mode: str) -> None:
        """The reader's choice of mode for this paper; "" leaves it to the PDF."""
        self.mode = mode if mode in MODES else ""
        self.save()

    def set_text(self, kind: str, text: str) -> bool:
        """The reader's edit. Returns False when nothing changed."""
        doc = self.docs.get(kind)
        if doc is None or doc.text == text:
            return False
        doc.text = text
        doc.edited = _now()
        self.save()
        return True

    def bind(self, layout: PaperLayout) -> bool:
        """Move each link whose sentence id now names other text back onto the
        sentence it was made for. Returns whether anything moved."""
        by_id = {s.id: _norm(s.text) for _, _, s in layout.sentences()}
        by_text: dict[str, str] = {}
        for _, _, sentence in layout.sentences():
            by_text.setdefault(_norm(sentence.text), sentence.id)
        moves = {
            old: new
            for old, text in self.anchors.items()
            if by_id.get(old) != text and (new := by_text.get(text)) is not None and new != old
        }
        if not moves:
            return False

        def move(match: re.Match[str]) -> str:
            ids = [moves.get(i, i) for i in match.group(2).split(",") if i]
            return f"[{match.group(1)}](ref:{','.join(ids)})"

        for doc in self.docs.values():
            doc.text = _REF_LINK.sub(move, doc.text)
        self.anchors = {moves.get(k, k): v for k, v in self.anchors.items()}
        self.save()
        log.info("Re-anchored %d review citations to the new layout", len(moves))
        return True
