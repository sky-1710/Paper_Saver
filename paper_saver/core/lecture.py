"""A lecture as the quiz sees it: slides, what was said over each, and how the
professor writes questions (§4c).

Three kinds of text come in, and each is cut down to what one slide's quiz
needs before anything is sent to Codex:

``SlideDeck``
    The open PDF, one page per slide. Page furniture - the lecturer's name
    and page number on every slide - is recognised by repetition and dropped,
    and the first line left is the slide's title.

``parse_transcript`` / ``align_transcript``
    The lecture's STT. A Lecture Saver export has a section per slide *shown
    on screen* ("## Slide 7 · Same as Slide 6"), and that numbering follows
    the video, not the PDF: going back to an earlier slide makes a new
    section. So sections are matched to PDF pages by the words they share,
    in lecture order (a dynamic programme that never moves backwards), and a
    "Same as Slide K" section goes wherever section K went. A transcript with
    no slide markers at all is cut into chunks and matched the same way. A
    lecture recorded in parts is joined into one first (``combine_transcripts``);
    a review of the whole lecture takes all of it, page by page
    (``transcript_by_page``).

``extract_quiz_examples``
    The professor's previous quizzes, reduced to their numbered statements,
    answers and explanations. Only the *style* is used; the prompt says so.

``EvidenceIndex``
    The way back: an answered question matched to the sentences it rests
    on, on its own slide and on the others it applies, so they can be lit.

Pure text processing: no Qt, no PyMuPDF, testable anywhere.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

__all__ = [
    "EvidenceIndex",
    "SlideDeck",
    "StyleExample",
    "TranscriptSection",
    "align_transcript",
    "combine_transcripts",
    "extract_quiz_examples",
    "format_duration",
    "format_quiz_examples",
    "parse_transcript",
    "repeated_terms",
    "seconds_by_page",
    "transcript_by_page",
    "transcript_excerpt",
    "visits_by_page",
]

#: Caps on what one prompt carries.
SLIDE_CHARS = 5000
TRANSCRIPT_CHARS = 7000
STYLE_CHARS = 6000
#: A transcript without slide markers is cut into pieces about this long.
CHUNK_CHARS = 1200

_BULLETS = "•◦▪▫‣∙·●○■□–-—*"
_HANGUL = re.compile(r"[가-힣]")
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?"


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _repeated_lines(pages: Sequence[Sequence[str]], share: float = 0.5) -> set[str]:
    """Lines on at least ``share`` of the pages: headers, footers, names."""
    if len(pages) < 4:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        counts.update({line for line in lines if line})
    return {line for line, n in counts.items() if n >= share * len(pages)}


# -- slides -----------------------------------------------------------------------


@dataclass
class SlideDeck:
    """The open PDF's pages as slide text, furniture removed."""

    pages: list[str]
    _lines: list[list[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        raw = [[_clean_line(line) for line in page.splitlines()] for page in self.pages]
        furniture = _repeated_lines(raw)
        self._lines = []
        for lines in raw:
            kept: list[str] = []
            for line in lines:
                if not line or line in furniture or line.isdigit():
                    continue
                if all(ch in _BULLETS for ch in line.replace(" ", "")):
                    continue
                kept.append(line)
            self._lines.append(kept)

    @property
    def count(self) -> int:
        return len(self.pages)

    def title(self, page: int) -> str:
        """The first line of the slide with some letters in it."""
        if not 0 <= page < len(self._lines):
            return ""
        for line in self._lines[page]:
            if len(line) <= 100 and len(re.findall(r"[A-Za-z가-힣]", line)) >= 2:
                return line.lstrip(_BULLETS + " ")
        return ""

    def content(self, page: int) -> str:
        if not 0 <= page < len(self._lines):
            return ""
        text = "\n".join(self._lines[page])
        return text[:SLIDE_CHARS] + (" …" if len(text) > SLIDE_CHARS else "")

    def titles(self) -> dict[int, str]:
        return {page: self.title(page) for page in range(self.count)}


# -- the transcript ---------------------------------------------------------------


@dataclass
class TranscriptSection:
    """One stretch of the lecture: a "Slide N" section, or a chunk of text."""

    label: str
    text: str
    number: int | None = None
    same_as: int | None = None
    start: str = ""
    end: str = ""

    @property
    def span(self) -> str:
        return f"{self.start}–{self.end}" if self.start and self.end else self.start


_HEADER = re.compile(
    r"^\s*(?:#{1,6}\s*)?\[?\s*(?:slide|슬라이드)\s*(\d{1,4})\s*\]?\s*(?:$|[·:|\-–—]\s*(.*)$)",
    re.IGNORECASE,
)
_SAME_AS = re.compile(r"same as slide\s*(\d{1,4})", re.IGNORECASE)
_HEADER_NOTE = re.compile(r"^(?:(?:before|with|after)\s+notes?|same as slide\s*\d+)(?:\s*[·|,]\s*.*)?$", re.IGNORECASE)
_RANGE = re.compile(rf"^`?\s*({_TIME})\s*(?:[–\-~]|-->)\s*({_TIME})\s*`?$")
_STAMP = re.compile(rf"^(?:\*\*)?\[?({_TIME})\]?(?:\*\*)?\s+")


def _spoken_lines(text: str) -> tuple[list[tuple[str, bool]], set[str]]:
    """Lines with a flag for "started with a timestamp", and furniture to drop."""
    lines: list[tuple[str, bool]] = []
    for raw in text.splitlines():
        line = _clean_line(raw)
        stamped = bool(_STAMP.match(line))
        lines.append((line, stamped))
    # A PDF export repeats its title and page number on every page; spoken
    # lines carry timestamps, so only unstamped lines are candidates.
    counts = Counter(line for line, stamped in lines if line and not stamped)
    furniture = {line for line, n in counts.items() if n >= 5 and not _HEADER.match(line) and not _RANGE.match(line)}
    return lines, furniture


def parse_transcript(text: str) -> list[TranscriptSection]:
    """The transcript as sections, spoken text only.

    Bilingual transcripts (Lecture Saver writes each line with its Korean
    translation) keep only the original language: the translation says the
    same thing again and would double the prompt.
    """
    lines, furniture = _spoken_lines(text)
    hangul_lines = sum(1 for line, _ in lines if _HANGUL.search(line))
    nonempty = sum(1 for line, _ in lines if line) or 1
    bilingual = 0.2 <= hangul_lines / nonempty <= 0.85

    sections: list[TranscriptSection] = []
    body: list[str] = []
    current: TranscriptSection | None = None
    first_stamp = last_stamp = ""

    def close() -> None:
        nonlocal body, first_stamp, last_stamp
        if current is not None:
            current.text = _clean_line(" ".join(body))
            current.start = current.start or first_stamp
            current.end = current.end or last_stamp
            sections.append(current)
        body, first_stamp, last_stamp = [], "", ""

    for line, _stamped in lines:
        if not line:
            continue
        header = _HEADER.match(line)
        if header:
            note = (header.group(2) or "").strip()
            if not note or _HEADER_NOTE.match(note):
                close()
                same = _SAME_AS.search(note)
                number = int(header.group(1))
                current = TranscriptSection(
                    label=f"Slide {number}", text="", number=number,
                    same_as=int(same.group(1)) if same else None,
                )
                continue
        timing = _RANGE.match(line)
        if timing:
            if current is not None and not current.start:
                current.start, current.end = timing.group(1), timing.group(2)
            continue
        if line in furniture or line.isdigit() or line.startswith("![") or line.startswith("#"):
            continue
        if line.startswith("*") and line.endswith("*") and not line.startswith("**"):
            continue  # "*Nothing was said while this slide was on screen.*"
        stamp = _STAMP.match(line)
        if stamp:
            first_stamp = first_stamp or stamp.group(1)
            last_stamp = stamp.group(1)
            line = line[stamp.end():]
        line = line.lstrip("> ").strip()
        if not line or (bilingual and _HANGUL.search(line)):
            continue
        body.append(line)
    if current is not None:
        close()

    if sections:
        return sections
    # No markers anywhere: cut the text into pieces of about CHUNK_CHARS.
    words = _clean_line(" ".join(body)).split(" ")
    chunks: list[TranscriptSection] = []
    piece: list[str] = []
    size = 0
    for word in words:
        piece.append(word)
        size += len(word) + 1
        if size >= CHUNK_CHARS:
            chunks.append(TranscriptSection(label=f"구간 {len(chunks) + 1}", text=" ".join(piece)))
            piece, size = [], 0
    if piece and any(piece):
        chunks.append(TranscriptSection(label=f"구간 {len(chunks) + 1}", text=" ".join(piece)))
    return chunks


# -- matching sections to slides --------------------------------------------------

_STOP = frozenset(
    """the and for are but not you all any can had her was one our out has have this that with from they
    will would there their what when which who how its it's into than then them these those some such
    also just like very more most only other about over after before because been being both each
    few many much same should could does did doing don't here where while were your yours okay yeah
    actually really right going know think see mean let now well kind thing things so um uh""".split()
)


def _tokens(text: str) -> list[str]:
    found = re.findall(r"[a-z][a-z0-9_]{2,}|[가-힣]{2,}", text.lower())
    return [t for t in found if t not in _STOP]


def _vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    counts = Counter(t for t in tokens if t in idf)
    vec = {t: (1 + math.log(n)) * idf[t] for t, n in counts.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def align_transcript(slides: Sequence[str], sections: Sequence[TranscriptSection]) -> dict[int, list[int]]:
    """Which sections belong to which slide: ``{page index: [section index]}``.

    Each section is placed on one page; pages never go backwards through the
    lecture, skipping pages costs a little, and a faint prior keeps sections
    spread in proportion when there is nothing lexical to go on. A section
    marked "Same as Slide K" follows section K.
    """
    if not slides or not sections:
        return {}
    page_tokens = [_tokens(s) for s in slides]
    df: Counter[str] = Counter()
    for tokens in page_tokens:
        df.update(set(tokens))
    idf = {t: math.log((1 + len(slides)) / (1 + n)) + 1 for t, n in df.items()}
    pages = [_vector(tokens, idf) for tokens in page_tokens]

    by_number = {s.number: i for i, s in enumerate(sections) if s.number is not None}
    free = [i for i, s in enumerate(sections) if not (s.same_as in by_number and by_number[s.same_as] < i)]
    count_p, count_s = len(slides), len(free)
    skip, prior = 0.015, 0.08

    vectors = [_vector(_tokens(sections[i].text), idf) for i in free]

    def score(index: int, page: int) -> float:
        vec = vectors[index]
        similarity = sum(w * pages[page].get(t, 0.0) for t, w in vec.items())
        expected = index / max(1, count_s - 1) if count_s > 1 else 0.0
        return similarity - prior * abs(expected - page / max(1, count_p - 1))

    best = [[-math.inf] * count_p for _ in range(count_s)]
    back = [[0] * count_p for _ in range(count_s)]
    for p in range(count_p):
        best[0][p] = score(0, p) - skip * p
    for s in range(1, count_s):
        scores = [score(s, p) for p in range(count_p)]
        for p in range(count_p):
            q_best, v_best = p, -math.inf
            for q in range(p + 1):
                value = best[s - 1][q] - skip * max(0, p - q - 1)
                if value > v_best:
                    q_best, v_best = q, value
            best[s][p] = v_best + scores[p]
            back[s][p] = q_best
    page = max(range(count_p), key=lambda p: best[-1][p]) if count_s else 0
    placed: dict[int, int] = {}
    for s in range(count_s - 1, -1, -1):
        placed[free[s]] = page
        page = back[s][page]

    for index, section in enumerate(sections):
        if index not in placed and section.same_as in by_number:
            placed[index] = placed.get(by_number[section.same_as], 0)

    result: dict[int, list[int]] = {}
    for index in sorted(placed):
        if sections[index].text.strip():
            result.setdefault(placed[index], []).append(index)
    return result


def combine_transcripts(parts: Sequence[Sequence[TranscriptSection]]) -> list[TranscriptSection]:
    """Several recordings' sections as one lecture, in the order given.

    A professor who records a lecture in parts leaves a file per part, each
    numbering its slides from 1 again. Numbers are moved up past the parts
    before, so a "Same as Slide 3" in the second part follows its own Slide 3,
    and each label says which part it is from ("영상 2 · Slide 3"). A single
    recording is returned as it is.
    """
    if len(parts) == 1:
        return list(parts[0])
    combined: list[TranscriptSection] = []
    offset = 0
    for index, sections in enumerate(parts, start=1):
        highest = 0
        for section in sections:
            combined.append(replace(
                section,
                label=f"영상 {index} · {section.label}",
                number=None if section.number is None else section.number + offset,
                same_as=None if section.same_as is None else section.same_as + offset,
            ))
            highest = max(highest, section.number or 0, section.same_as or 0)
        offset += highest
    return combined


def _seconds(stamp: str) -> int | None:
    """ "01:08:31", "08:31" or "08:31.5" as seconds."""
    whole = re.split(r"[.,]", stamp.strip())[0]
    try:
        numbers = [int(part) for part in whole.split(":")]
    except ValueError:
        return None
    total = 0
    for number in numbers:
        total = total * 60 + number
    return total


def seconds_by_page(
    slides: Sequence[str], sections: Sequence[TranscriptSection], mapping: Mapping[int, list[int]] | None = None
) -> dict[int, int]:
    """How long the lecture stayed on each page: ``{page index: seconds}``.

    Where a professor spends nine minutes is where the exam is - a better
    sign than anything they say (measured on OS08: no "important" or "exam"
    anywhere, while the slides that took longest were the ones its quiz
    asked about). Empty when the transcript carries no times. ``mapping`` is
    :func:`align_transcript`'s answer, when it has been asked already.
    """
    out: dict[int, int] = {}
    mapping = align_transcript(slides, sections) if mapping is None else mapping
    for page, indices in mapping.items():
        total = 0
        for index in indices:
            start, end = _seconds(sections[index].start), _seconds(sections[index].end)
            if start is not None and end is not None and end > start:
                total += end - start
        if total:
            out[page] = total
    return out


def visits_by_page(sections: Sequence[TranscriptSection], mapping: Mapping[int, list[int]]) -> dict[int, int]:
    """How many times each page was put on screen: ``{page index: times}``.

    A recording makes a section each time the screen changes, so a page the
    professor came back to has a section per return (OS08's most-shown pages,
    five and six times, were what its quiz asked about). Only a transcript
    with slide markers says this; one cut into chunks gives ``{}``.
    """
    if not any(section.number is not None for section in sections):
        return {}
    return {
        page: shown
        for page, indices in mapping.items()
        if (shown := sum(1 for i in indices if sections[i].number is not None))
    }


#: Words that begin or end no term: function words, and the verbs and nouns
#: any lecture says all the time.
_COMMON = _STOP | frozenset(
    """a an as at by if in is it of on or to we us no not do does did has have had its our i he she they
    be are was were am can cannot must may might will use used uses using call called access accessed
    accesses load loaded loads first second new old same case cases example examples value values number
    numbers problem problems operation operations system systems data time times size sizes space process
    processes way part type make made need needs get gets give show look done other each every all more most
    less much many some only just very well good high low large small one two three four zero important
    simple general possible different following based current""".split()
)
_TERM_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[가-힣]+")


def _term_candidates(text: str, acronyms: set[str]) -> list[str]:
    """Phrases of two or three words, and the slides' acronyms, in ``text``."""
    words = [word.lower() for word in _TERM_WORD.findall(text)]
    out = []
    for size in (1, 2, 3):
        for start in range(len(words) - size + 1):
            gram = words[start : start + size]
            if gram[0] in _COMMON or gram[-1] in _COMMON or len(gram[0]) < 2 or len(gram[-1]) < 2:
                continue
            if size == 1 and gram[0] not in acronyms:
                continue
            out.append(" ".join(gram))
    return out


def repeated_terms(
    slides: Sequence[str],
    sections: Sequence[TranscriptSection],
    mapping: Mapping[int, list[int]] | None = None,
    *,
    limit: int = 20,
) -> list[tuple[str, int, int]]:
    """The slides' terms the professor kept saying, most first: (term, times
    said, pages it was said over).

    A term is a phrase of two or three words on the slides ("reference bit",
    "history register") or an acronym never written in lower case there
    ("LRU"). Single ordinary words were too noisy to count - "accessed",
    "must" and "example" topped the list. A term on a quarter of the slides
    or more is the subject itself and says nothing; the rest are weighed by
    how few slides carry them, as a search engine weighs a rare word.
    """
    mapping = align_transcript(slides, sections) if mapping is None else mapping
    page_of = {i: page for page, indices in mapping.items() for i in indices}
    upper: Counter[str] = Counter()
    lower: Counter[str] = Counter()
    for page in slides:
        for word in _TERM_WORD.findall(page):
            (upper if word.isupper() and 2 <= len(word) <= 6 else lower)[word.lower()] += 1
    acronyms = {word for word in upper if word not in lower}
    on_slides: Counter[str] = Counter()
    for page in slides:
        on_slides.update(set(_term_candidates(page, acronyms)))
    said: Counter[str] = Counter()
    pages: dict[str, set[int]] = {}
    for index, section in enumerate(sections):
        for term in _term_candidates(section.text, acronyms):
            if 0 < on_slides[term] <= len(slides) / 4:
                said[term] += 1
                if index in page_of:
                    pages.setdefault(term, set()).add(page_of[index])
    weight = {term: n * math.log(len(slides) / on_slides[term]) for term, n in said.items() if n >= 4}
    ranked = sorted(weight, key=lambda term: (-weight[term], term))
    # "reference bit" said forty times makes "reference bits" no news.
    kept = [
        term for term in ranked
        if not any(other != term and f" {term} " in f" {other} " and said[other] >= 0.6 * said[term]
                   for other in ranked[:80])
    ]
    return [
        (term.upper() if term in acronyms else term, said[term], len(pages.get(term, ())))
        for term in kept[:limit]
    ]


def format_duration(seconds: int) -> str:
    """ "9분 17초", "45초"."""
    minutes, rest = divmod(max(0, int(seconds)), 60)
    return f"{minutes}분 {rest}초" if minutes else f"{rest}초"


#: The STT a review of the whole lecture carries, at most.
REVIEW_TRANSCRIPT_CHARS = 200_000


def transcript_by_page(
    slides: Sequence[str],
    sections: Sequence[TranscriptSection],
    budget: int = REVIEW_TRANSCRIPT_CHARS,
    mapping: Mapping[int, list[int]] | None = None,
) -> dict[int, str]:
    """All of the STT, each part under the page it explains: ``{page index: text}``.

    For a review of the whole lecture rather than one slide's quiz: every
    section is placed somewhere, and a lecture that says more than ``budget``
    characters has each page's share cut in proportion.
    """
    mapping = align_transcript(slides, sections) if mapping is None else mapping
    total = sum(len(sections[i].text) for indices in mapping.values() for i in indices)
    scale = min(1.0, budget / total) if total else 1.0
    out: dict[int, str] = {}
    for page, indices in mapping.items():
        size = sum(len(sections[i].text) for i in indices)
        # Room for each section's header ("[영상 2 · Slide 7 · 00:03:20–00:05:10]")
        # and, when nothing is to be cut, for transcript_excerpt's slack too.
        room = 120 if scale >= 1 else 40
        text, _ = transcript_excerpt(sections, indices, limit=int(size * scale) + room * len(indices))
        if text:
            out[page] = text
    return out


def transcript_excerpt(
    sections: Sequence[TranscriptSection], indices: Sequence[int], limit: int = TRANSCRIPT_CHARS
) -> tuple[str, str]:
    """The text of those sections for the prompt, and a label saying which they were."""
    parts: list[str] = []
    labels: list[str] = []
    used = 0
    for index in indices:
        section = sections[index]
        head = f"[{section.label}{' · ' + section.span if section.span else ''}]"
        room = limit - used - len(head) - 1
        if room <= 80:
            break
        text = section.text if len(section.text) <= room else section.text[:room] + " …"
        parts.append(f"{head}\n{text}")
        labels.append(section.label + (f" ({section.span})" if section.span else ""))
        used += len(head) + len(text) + 2
    return "\n\n".join(parts), ", ".join(labels)


# -- what a question rests on -------------------------------------------------------

#: Words that join a statement without naming anything in it: "even though"
#: on two slides is no link between them.
_FILLER = frozenset(
    """even though although too may might can cannot still need needs without within enough must able unless
    whether either neither since therefore thus however every using used use via""".split()
)


def _terms(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _FILLER]


class EvidenceIndex:
    """The lecture's sentences, searched for what a quiz question rests on (§4c.5).

    A question is written from one slide, but a hard one applies what other
    slides say: "stride scheduling picks the lowest pass value", asked on the
    slide comparing it with lottery, rests on the slide before. The question
    and its explanation are matched against every sentence by the words and
    two-word phrases they share, each word weighed by how few pages carry it,
    as :func:`align_transcript` weighs them - a word on every slide links
    nothing. The question's own slide is always where it starts; another page
    is taken only when it shares a phrase and matches nearly as well as that
    slide does - and at least as well as two words found on no other page
    would, so a question its slide hardly matches does not light stray
    pages. Nothing is sent anywhere.
    """

    def __init__(self, sentences: Iterable[tuple[int, str, str]]) -> None:
        """``sentences``: (page index, sentence id, text), in reading order."""
        self._rows: list[tuple[int, str, set[str], set[tuple[str, str]]]] = []
        on_page: dict[int, set[str]] = {}
        for page, sentence_id, text in sentences:
            terms = _terms(text)
            self._rows.append((page, sentence_id, set(terms), set(zip(terms, terms[1:]))))
            on_page.setdefault(page, set()).update(terms)
        df: Counter[str] = Counter()
        for terms in on_page.values():
            df.update(terms)
        count = max(1, len(on_page))
        self._idf = {t: math.log((1 + count) / (1 + n)) for t, n in df.items()}
        self._floor = 2 * math.log((1 + count) / 2)

    def evidence(self, text: str, home: int, *, pages: int = 3, per_page: int = 3) -> dict[int, list[str]]:
        """The sentences ``text`` rests on, ``{page index: [sentence id]}`` in
        page order: those of page ``home`` and of at most ``pages`` others,
        at most ``per_page`` on each, best first."""
        terms = _terms(text)
        words, phrases = set(terms), set(zip(terms, terms[1:]))
        idf = self._idf
        # (score, shares a phrase, page, id)
        scored: list[tuple[float, bool, int, str]] = []
        for page, sentence_id, own_words, own_phrases in self._rows:
            joined = phrases & own_phrases
            score = sum(idf[t] for t in words & own_words) + sum((idf[a] + idf[b]) / 2 for a, b in joined)
            if score > 0:
                scored.append((score, bool(joined), page, sentence_id))
        home_best = max((s for s, _, page, _ in scored if page == home), default=0.0)
        linked: dict[int, float] = {}
        for score, joined, page, _ in scored:
            if joined and page != home:
                linked[page] = max(linked.get(page, 0.0), score)
        bar = max(0.6 * home_best, self._floor)
        others = sorted((p for p, best in linked.items() if best >= bar), key=lambda p: -linked[p])[:pages]
        out: dict[int, list[str]] = {}
        for page, best in ((home, home_best), *((p, linked[p]) for p in others)):
            rows = sorted(
                (row for row in scored if row[2] == page and (page == home or row[1]) and row[0] >= 0.6 * best),
                key=lambda row: -row[0],
            )
            if rows:
                out[page] = [sentence_id for *_, sentence_id in rows[:per_page]]
        return dict(sorted(out.items()))


# -- the professor's quizzes -------------------------------------------------------


@dataclass(frozen=True)
class StyleExample:
    statement: str
    answer: bool
    explanation: str = ""


_NUMBER = re.compile(r"(?:^|\s)(\d{1,2})\.\s+")
_VERDICT = re.compile(r"\(\s*([OoXx])\s*\)")
#: Labels of a diagram that the PDF's text runs into an explanation: P1 P2 STCF SJF.
_LABEL_RUN = re.compile(r"(?:\s+[A-Z0-9]{1,5}(?=\s|$)){4,}.*$", re.DOTALL)


def extract_quiz_examples(pages: Sequence[str]) -> list[StyleExample]:
    """Numbered "statement (O)" / "(X)" items, with a "↳" explanation if any.

    Items are taken in sequence - 1., 2., 3. … - so a number inside an
    explanation ("(7+10+4)/3 = 7. P1 runs") is not mistaken for the next item.
    """
    raw = [[_clean_line(line) for line in page.splitlines()] for page in pages]
    furniture = _repeated_lines(raw, share=0.5)
    text = " ".join(
        line for lines in raw for line in lines if line and line not in furniture and not line.isdigit()
    )
    starts: list[tuple[int, int]] = []  # (where the item's text begins, where its number began)
    expected = 1
    for match in _NUMBER.finditer(text):
        number = int(match.group(1))
        if number == expected or (number == 1 and expected > 1):
            starts.append((match.end(), match.start()))
            expected = number + 1
    found: list[StyleExample] = []
    for index, (begin, _) in enumerate(starts):
        stop = starts[index + 1][1] if index + 1 < len(starts) else len(text)
        item = text[begin:stop]
        verdict = _VERDICT.search(item)
        if verdict is None:
            continue
        statement = _clean_line(item[: verdict.start()])
        if len(statement) < 12:
            continue
        explanation = ""
        tail = item[verdict.end():]
        if "↳" in tail:
            explanation = tail.split("↳", 1)[1].split("•", 1)[0]
            explanation = _clean_line(_LABEL_RUN.sub("", explanation))
            if len(explanation) > 360:
                explanation = explanation[:360].rsplit(" ", 1)[0] + " …"
        found.append(StyleExample(statement, verdict.group(1).upper() == "O", explanation))
    return found


def format_quiz_examples(examples: Sequence[StyleExample], fallback: str = "", limit: int = STYLE_CHARS) -> str:
    """The examples as the prompt shows them; ``fallback`` when none were found."""
    if not examples:
        return _clean_line(fallback)[: limit // 2]
    lines: list[str] = []
    used = 0
    for example in examples:
        entry = f"- {example.statement} ({'O' if example.answer else 'X'})"
        if example.explanation:
            entry += f"\n  ↳ {example.explanation}"
        if used + len(entry) > limit:
            break
        lines.append(entry)
        used += len(entry) + 1
    return "\n".join(lines)
