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
    no slide markers at all is cut into chunks and matched the same way.

``extract_quiz_examples``
    The professor's previous quizzes, reduced to their numbered statements,
    answers and explanations. Only the *style* is used; the prompt says so.

Pure text processing: no Qt, no PyMuPDF, testable anywhere.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "SlideDeck",
    "StyleExample",
    "TranscriptSection",
    "align_transcript",
    "extract_quiz_examples",
    "format_quiz_examples",
    "parse_transcript",
    "transcript_excerpt",
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
