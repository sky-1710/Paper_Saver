"""백지 복습: blank-page recall, checked against the PDF (§4e).

    <project>/blank.json     the keyword sheet, its answer key, every attempt

The blank-page method: given only keywords, write down everything remembered,
then hold it against the material to see what was missing. Paper Saver gives
the keywords, keeps what the reader writes, and has GPT mark it.

The sheet is written by GPT from the PDF itself - its text, and the lecture's
STT only to judge what was stressed - never from the one-page review or the
Cornell notes, which are a summary of it and would make a summary the
standard. Each topic carries:

* keywords, the only thing shown while writing: terms, no explanations;
* the answer key, hidden until marked: the statements a full answer holds,
  each citing the sentences of the PDF it comes from.

Marking is against that fixed key - recalled, partly, or missed, point by
point - so one attempt and the next are counted in the same units, and
"3/8 → 7/8" means what it says. Statements the reader wrote that the PDF
contradicts are listed as well, with the correction and its sentences.

Figures and tables are part of the PDF too. Their text goes with the rest,
and :func:`figure_regions` picks the pictures worth seeing - the regions the
layout keeps as pixels (§1.2), less a slide template's logo - so they can be
cut out and attached to the request for the sheet: a table pasted in as an
image has no text to send otherwise. The sheet names them in its keywords
("표: …"), to be drawn again from memory.

Like a review's links, the key's citations are sentence ids, and the text of
each sentence cited is kept so :meth:`BlankStore.bind` can move them when the
layout is re-extracted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic
from .chat import resolve_citations
from .geometry import Rect
from .models import PaperLayout
from .review import ReviewFormatError, _Reader, cite

log = logging.getLogger(__name__)

__all__ = [
    "KEYWORD_LINE",
    "MARKS",
    "Attempt",
    "BlankStore",
    "KeyPoint",
    "Sheet",
    "Topic",
    "Wrong",
    "answer_text",
    "attempt_template",
    "figure_regions",
    "grade_from_answer",
    "result_markdown",
    "sheet_from_answer",
    "wrote_anything",
]

#: How each point of the key was found in what the reader wrote.
MARKS = {"recalled": "기억함", "partial": "일부만", "missed": "빠뜨림"}
_WORTH = {"recalled": 1.0, "partial": 0.5, "missed": 0.0}
#: The line under each heading of the writing sheet that lists its keywords.
#: Printed on the sheet, so it is not part of the answer.
KEYWORD_LINE = "> 키워드:"
#: A keyword longer than this is an explanation, which gives the answer away.
KEYWORD_CHARS = 48


#: Pictures attached to the request for a sheet, at most: one per page that
#: has figures or tables. Measured on six OS lectures (15-61 slides): 10-37
#: such pages, so a lecture goes whole. Picked by size instead, one picture
#: per figure and 24 at most, OS08 lost its hit-rate graphs, its thrashing
#: curve and its clock diagram - what its quiz asks about - to code listings.
FIGURE_LIMIT = 40
#: A region smaller than this share of its page is a logo or an icon.
FIGURE_MIN_SHARE = 0.025
#: ... or narrower or shorter than this, in points.
FIGURE_MIN_SIDE = 40.0
#: A region in the same place on this share of the pages is the template's.
TEMPLATE_SHARE = 0.3


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm(text: str) -> str:
    return " ".join((text or "").split())


@dataclass
class KeyPoint:
    """One statement a full answer holds."""

    #: Numbered across the sheet, from 1.
    number: int
    text: str
    refs: list[str] = field(default_factory=list)
    #: Stressed in the lecture (time on the slide, shown again, said often),
    #: or a paper's main claim.
    important: bool = False


@dataclass
class Topic:
    #: From 1, in the order of the PDF.
    number: int
    heading: str
    keywords: list[str]
    points: list[KeyPoint]

    @property
    def refs(self) -> list[str]:
        return list(dict.fromkeys(i for p in self.points for i in p.refs))


@dataclass
class Sheet:
    topics: list[Topic]
    generated: str = ""
    engine: str = ""
    model: str = ""
    #: "lecture" or "paper", as reviews are written.
    mode: str = ""
    #: The lecture STT files it was made with, by name.
    transcripts: list[str] = field(default_factory=list)
    #: How many figures and tables went with it as pictures.
    figures: int = 0

    def topic(self, number: int) -> Topic | None:
        return next((t for t in self.topics if t.number == number), None)

    def points(self, topics: Iterable[int] | None = None) -> list[KeyPoint]:
        wanted = None if topics is None else set(topics)
        return [p for t in self.topics if wanted is None or t.number in wanted for p in t.points]


@dataclass
class Wrong:
    """Something the reader wrote that the PDF says otherwise."""

    topic: int
    wrote: str
    correct: str
    refs: list[str] = field(default_factory=list)


@dataclass
class Attempt:
    id: str
    started: str
    #: The topics written on, by number.
    topics: list[int]
    #: What the reader wrote, Markdown, saved as typed.
    text: str = ""
    #: When it was marked; "" while it is being written.
    checked: str = ""
    #: Point number -> "recalled", "partial" or "missed".
    marks: dict[int, str] = field(default_factory=dict)
    #: Point number -> what a partly recalled point left out.
    missing: dict[int, str] = field(default_factory=dict)
    wrong: list[Wrong] = field(default_factory=list)
    overall: str = ""
    model: str = ""

    def score(self, topics: Iterable[int] | None = None, sheet: Sheet | None = None) -> tuple[float, int]:
        """(points earned, points asked): a partly recalled point is half."""
        numbers: Iterable[int] = self.marks
        if topics is not None and sheet is not None:
            numbers = [p.number for p in sheet.points(topics) if p.number in self.marks]
        numbers = list(numbers)
        return sum(_WORTH.get(self.marks[n], 0.0) for n in numbers), len(numbers)


# -- the writing sheet ---------------------------------------------------------------


def attempt_template(sheet: Sheet, topics: Iterable[int]) -> str:
    """The blank sheet: each topic's heading and keywords, room under each."""
    wanted = set(topics)
    parts = []
    for topic in sheet.topics:
        if topic.number in wanted:
            parts.append(f"## {topic.number}. {topic.heading}\n{KEYWORD_LINE} {' · '.join(topic.keywords)}\n\n")
    return "\n".join(parts)


def answer_text(text: str) -> str:
    """What the reader wrote, without the keyword lines the sheet printed."""
    lines = [line for line in (text or "").splitlines() if not line.strip().startswith(KEYWORD_LINE)]
    return "\n".join(lines).strip()


_HEADING_ONLY = re.compile(r"^\s*#{1,6}\s+.*$")


def wrote_anything(text: str) -> bool:
    """Whether the sheet holds an answer, not just its printed headings."""
    return any(line.strip() and not _HEADING_ONLY.match(line) for line in answer_text(text).splitlines())


# -- figures and tables --------------------------------------------------------------


def _same_place(a: Rect, b: Rect, slack: float = 6.0) -> bool:
    return all(abs(x - y) <= slack for x, y in zip((a.x0, a.y0, a.x1, a.y1), (b.x0, b.y0, b.x1, b.y1)))


def figure_regions(layout: PaperLayout, limit: int = FIGURE_LIMIT) -> list[tuple[int, Rect]]:
    """The figures and tables of the PDF worth showing GPT: (page, region),
    one region per page - around all of that page's - in reading order.

    The regions are those the layout keeps as pixels. Left out: anything
    too small to hold a table or a diagram, and anything that sits in the
    same place on many pages - a slide template's logo or banner. With more
    than ``limit`` pages, those with the most figure on them are kept.
    """
    found: list[tuple[int, Rect]] = []
    for page in layout.pages:
        page_area = max(1.0, page.width * page.height)
        for rect in page.protected:
            if rect.width < FIGURE_MIN_SIDE or rect.height < FIGURE_MIN_SIDE:
                continue
            if rect.area < page_area * FIGURE_MIN_SHARE:
                continue
            found.append((page.number, rect))
    pages = max(1, len(layout.pages))
    if pages >= 4:
        repeated = []
        for number, rect in found:
            on = {other for other, there in found if _same_place(rect, there)}
            if len(on) >= max(3, pages * TEMPLATE_SHARE):
                repeated.append((number, rect))
        found = [item for item in found if item not in repeated]
    # Two graphs side by side, a diagram and its legend: one picture of the page's figures.
    by_page: dict[int, Rect] = {}
    for number, rect in found:
        by_page[number] = by_page[number].union(rect) if number in by_page else rect
    kept = sorted(by_page.items(), key=lambda item: -item[1].area)[: max(0, limit)]
    return sorted(kept, key=lambda item: item[0])


# -- GPT's answers -------------------------------------------------------------------


def _keywords(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReviewFormatError(f"{where}: keywords가 비어 있습니다.")
    words = list(dict.fromkeys(_norm(str(word)) for word in value if _norm(str(word))))
    long = [word for word in words if len(word) > KEYWORD_CHARS]
    if long:
        raise ReviewFormatError(
            f"{where}: 키워드는 설명 없이 용어만 짧게 써야 합니다 ({KEYWORD_CHARS}자 이하). 너무 긴 것: '{long[0]}'"
        )
    if not words:
        raise ReviewFormatError(f"{where}: keywords가 비어 있습니다.")
    return words


def sheet_from_answer(
    data: Any, *, numbering: Mapping[int, str], page_of: Callable[[str], int | None]
) -> Sheet:
    """A checked keyword sheet. Raises :class:`ReviewFormatError`.

    Every topic needs a heading, keywords short enough not to be answers,
    and points; most points must cite the PDF.
    """
    if not isinstance(data, dict):
        raise ReviewFormatError("답이 JSON 객체가 아닙니다.")
    items = data.get("topics")
    if not isinstance(items, list) or not items:
        raise ReviewFormatError("topics가 비어 있습니다.")
    reader = _Reader(numbering, page_of)
    topics: list[Topic] = []
    count = 0
    for index, item in enumerate(items, start=1):
        where = f"topic {index}"
        if not isinstance(item, dict):
            raise ReviewFormatError(f"{where}: 객체가 아닙니다.")
        heading = _norm(str(item.get("heading") or "")).lstrip("#").strip()
        if not heading:
            raise ReviewFormatError(f"{where}: heading이 비어 있습니다.")
        keywords = _keywords(item.get("keywords"), where)
        raw = item.get("points")
        points = []
        for number, (point, entry) in enumerate(
            zip(reader.points_of(raw, f"{where} points"), raw), start=count + 1
        ):
            points.append(KeyPoint(number, point.text, point.refs, bool(entry.get("important"))))
        count += len(points)
        topics.append(Topic(index, heading, keywords, points))
    reader.check()
    return Sheet(topics)


def grade_from_answer(
    data: Any,
    sheet: Sheet,
    topics: Iterable[int],
    *,
    numbering: Mapping[int, str],
    page_of: Callable[[str], int | None],
) -> tuple[dict[int, str], dict[int, str], list[Wrong], str]:
    """A checked marking: (marks, missing, wrong, overall).

    Every point of the topics written on must be marked; anything else is
    sent back. Raises :class:`ReviewFormatError`.
    """
    if not isinstance(data, dict):
        raise ReviewFormatError("답이 JSON 객체가 아닙니다.")
    scope = list(topics)
    asked = {p.number for p in sheet.points(scope)}
    marks: dict[int, str] = {}
    missing: dict[int, str] = {}
    for item in data.get("marks") or []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("point"))
        except (TypeError, ValueError):
            continue
        status = str(item.get("status") or "")
        if number not in asked or status not in MARKS:
            continue
        marks[number] = status
        gap = _norm(str(item.get("missing") or ""))
        if status == "partial" and gap:
            missing[number] = gap
    unmarked = sorted(asked - set(marks))
    if unmarked:
        shown = ", ".join(str(n) for n in unmarked[:20])
        raise ReviewFormatError(f"채점하지 않은 기준 항목이 있습니다: {shown}. marks에 항목마다 하나씩 넣으세요.")
    wrong: list[Wrong] = []
    for item in data.get("wrong") or []:
        if not isinstance(item, dict):
            continue
        wrote, correct = _norm(str(item.get("wrote") or "")), _norm(str(item.get("correct") or ""))
        if not wrote or not correct:
            continue
        correct, inline = resolve_citations(correct, numbering)
        correct = re.sub(r"\s*⟦r\d+⟧", "", correct).strip()
        refs: list[str] = []
        for source in item.get("sources") or []:
            match = re.search(r"\d+", str(source))
            sentence_id = numbering.get(int(match.group(0))) if match else None
            if sentence_id is not None and sentence_id not in refs:
                refs.append(sentence_id)
        refs += [i for i in inline if i not in refs]
        refs = [i for i in refs if page_of(i) is not None][:6]
        try:
            topic = int(item.get("topic"))
        except (TypeError, ValueError):
            topic = 0
        wrong.append(Wrong(topic if topic in scope else 0, wrote, correct, refs))
    overall = _norm(str(data.get("overall") or ""))
    return marks, missing, wrong, overall


# -- the marked attempt, to read -----------------------------------------------------


def _when(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).strftime("%m/%d %H:%M")
    except ValueError:
        return stamp


def _fraction(got: float, total: int) -> str:
    return f"{got:g}/{total}"


def result_markdown(
    sheet: Sheet, attempt: Attempt, page_of: Callable[[str], int | None], *, before: Sequence[Attempt] = ()
) -> str:
    """A marked attempt as Markdown with citation links, topic by topic:
    what was written wrong, what was missed (★ where it was stressed), what
    was partly there, and what was recalled. ``before`` are earlier marked
    attempts, newest first, for each topic's last score."""
    got, total = attempt.score()
    counts = {status: sum(1 for m in attempt.marks.values() if m == status) for status in MARKS}
    percent = round(100 * got / total) if total else 0
    lines = [f"# 백지 채점 · {_when(attempt.checked or attempt.started)}", ""]
    summary = (
        f"> **{_fraction(got, total)} ({percent}%)** · 기억 {counts['recalled']} · 일부 {counts['partial']} · "
        f"빠뜨림 {counts['missed']} · 틀림 {len(attempt.wrong)}"
    )
    lines.append(summary)
    if attempt.overall:
        lines.append(f"> {attempt.overall}")
    lines.append("")
    for number in attempt.topics:
        topic = sheet.topic(number)
        if topic is None:
            continue
        mine, asked = attempt.score([number], sheet)
        head = f"## {topic.number}. {topic.heading} — {_fraction(mine, asked)}"
        last = next((a for a in before if number in a.topics and a.marks), None)
        if last is not None:
            earlier, of = last.score([number], sheet)
            head += f" (지난번 {_fraction(earlier, of)})"
        lines += [head, f"키워드: {' · '.join(topic.keywords)}", ""]
        wrong = [w for w in attempt.wrong if w.topic == number]
        groups: dict[str, list[KeyPoint]] = {status: [] for status in MARKS}
        for point in topic.points:
            if point.number in attempt.marks:
                groups[attempt.marks[point.number]].append(point)
        if wrong:
            lines.append("**틀리게 쓴 것**")
            lines += [f"- ✗ “{w.wrote}” → {w.correct}{cite(w.refs, page_of)}" for w in wrong]
            lines.append("")
        if groups["missed"]:
            lines.append("**빠뜨린 것**")
            lines += [f"- {'★ ' if p.important else ''}{p.text}{cite(p.refs, page_of)}" for p in groups["missed"]]
            lines.append("")
        if groups["partial"]:
            lines.append("**일부만 쓴 것**")
            for p in groups["partial"]:
                gap = f" — 빠진 부분: {attempt.missing[p.number]}" if attempt.missing.get(p.number) else ""
                lines.append(f"- △ {'★ ' if p.important else ''}{p.text}{gap}{cite(p.refs, page_of)}")
            lines.append("")
        if groups["recalled"]:
            lines.append("**기억한 것**")
            lines += [f"- ✓ {p.text}{cite(p.refs, page_of)}" for p in groups["recalled"]]
            lines.append("")
    stray = [w for w in attempt.wrong if w.topic not in attempt.topics]
    if stray:
        lines += ["## 그 밖에 틀리게 쓴 것", *(f"- ✗ “{w.wrote}” → {w.correct}{cite(w.refs, page_of)}" for w in stray), ""]
    return "\n".join(lines).rstrip() + "\n"


# -- the store -----------------------------------------------------------------------


def _point_dict(point: KeyPoint) -> dict[str, Any]:
    return {"number": point.number, "text": point.text, "refs": point.refs, "important": point.important}


class BlankStore:
    """The sheet and the attempts, saved whole and atomically on every change."""

    FILE = "blank.json"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / self.FILE
        self.sheet: Sheet | None = None
        self.attempts: list[Attempt] = []
        #: Sentence id -> its text, for every sentence the sheet or a marking cites.
        self.anchors: dict[str, str] = {}
        #: Attach the PDF's figures and tables, as pictures, when the sheet is made.
        self.send_figures = True
        #: Every attempt id handed out, so a removed one is not handed out again.
        self._issued: set[str] = set()
        self._load()

    # -- disk ------------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = read_json(self.path)
            sheet = data.get("sheet")
            if isinstance(sheet, dict):
                topics = []
                for item in sheet.get("topics") or []:
                    points = [
                        KeyPoint(int(p["number"]), str(p["text"]), [str(i) for i in p.get("refs") or []], bool(p.get("important")))
                        for p in item.get("points") or []
                    ]
                    topics.append(Topic(int(item["number"]), str(item["heading"]), [str(k) for k in item.get("keywords") or []], points))
                self.sheet = Sheet(
                    topics,
                    **{k: str(sheet.get(k) or "") for k in ("generated", "engine", "model", "mode")},
                    transcripts=[str(name) for name in sheet.get("transcripts") or []],
                    figures=int(sheet.get("figures") or 0),
                )
            for item in data.get("attempts") or []:
                self.attempts.append(
                    Attempt(
                        str(item["id"]), str(item.get("started") or ""), [int(n) for n in item.get("topics") or []],
                        text=str(item.get("text") or ""), checked=str(item.get("checked") or ""),
                        marks={int(k): str(v) for k, v in (item.get("marks") or {}).items() if v in MARKS},
                        missing={int(k): str(v) for k, v in (item.get("missing") or {}).items()},
                        wrong=[
                            Wrong(int(w.get("topic") or 0), str(w["wrote"]), str(w["correct"]), [str(i) for i in w.get("refs") or []])
                            for w in item.get("wrong") or []
                        ],
                        overall=str(item.get("overall") or ""), model=str(item.get("model") or ""),
                    )
                )
            self.anchors = {str(k): str(v) for k, v in (data.get("anchors") or {}).items()}
            self.send_figures = bool(data.get("send_figures", True))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            backup = self.path.with_suffix(".json.damaged")
            log.exception("Could not read %s; keeping it as %s", self.path, backup.name)
            self.sheet, self.attempts, self.anchors = None, [], {}
            try:
                self.path.replace(backup)
            except OSError:
                pass

    def save(self) -> None:
        sheet = None
        if self.sheet is not None:
            sheet = {
                "topics": [
                    {"number": t.number, "heading": t.heading, "keywords": t.keywords, "points": [_point_dict(p) for p in t.points]}
                    for t in self.sheet.topics
                ],
                "generated": self.sheet.generated, "engine": self.sheet.engine, "model": self.sheet.model,
                "mode": self.sheet.mode, "transcripts": self.sheet.transcripts, "figures": self.sheet.figures,
            }
        attempts = [
            {
                "id": a.id, "started": a.started, "topics": a.topics, "text": a.text, "checked": a.checked,
                "marks": {str(k): v for k, v in a.marks.items()}, "missing": {str(k): v for k, v in a.missing.items()},
                "wrong": [{"topic": w.topic, "wrote": w.wrote, "correct": w.correct, "refs": w.refs} for w in a.wrong],
                "overall": a.overall, "model": a.model,
            }
            for a in self.attempts
        ]
        write_json_atomic(
            self.path,
            {"sheet": sheet, "attempts": attempts, "anchors": self.anchors, "send_figures": self.send_figures},
        )

    def set_send_figures(self, on: bool) -> None:
        self.send_figures = bool(on)
        self.save()

    # -- the sheet -------------------------------------------------------------------

    def put_sheet(self, sheet: Sheet, *, anchors: Mapping[str, str]) -> None:
        """A new sheet. Attempts were marked against the old key, so they go."""
        sheet.generated = sheet.generated or _now()
        self.sheet = sheet
        self.attempts = []
        self.anchors = {k: _norm(v) for k, v in anchors.items()}
        self.save()

    # -- attempts --------------------------------------------------------------------

    def get(self, attempt_id: str) -> Attempt | None:
        return next((a for a in self.attempts if a.id == attempt_id), None)

    def open_attempt(self) -> Attempt | None:
        """The attempt being written, if one was left unmarked."""
        return next((a for a in reversed(self.attempts) if not a.checked), None)

    def marked(self) -> list[Attempt]:
        """Marked attempts, newest first."""
        return [a for a in reversed(self.attempts) if a.checked]

    def start(self, topics: Iterable[int]) -> Attempt:
        """A new attempt on these topics, its sheet printed. One left
        unmarked and empty is replaced; one with writing in it is kept."""
        if self.sheet is None:
            raise ValueError("no sheet")
        scope = [t.number for t in self.sheet.topics if t.number in set(topics)]
        stale = self.open_attempt()
        if stale is not None and not wrote_anything(stale.text):
            self.attempts.remove(stale)
        # Never an id an attempt had before, even one since removed: two
        # starts can fall in one tick of the clock.
        taken = {a.id for a in self.attempts} | self._issued
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        attempt_id, extra = f"a{stamp}", 1
        while attempt_id in taken:
            attempt_id, extra = f"a{stamp}-{extra}", extra + 1
        self._issued.add(attempt_id)
        attempt = Attempt(attempt_id, _now(), scope, text=attempt_template(self.sheet, scope))
        self.attempts.append(attempt)
        self.save()
        return attempt

    def set_text(self, attempt_id: str, text: str) -> bool:
        attempt = self.get(attempt_id)
        if attempt is None or attempt.text == text:
            return False
        attempt.text = text
        self.save()
        return True

    def grade(
        self,
        attempt_id: str,
        marks: Mapping[int, str],
        missing: Mapping[int, str],
        wrong: list[Wrong],
        overall: str,
        *,
        anchors: Mapping[str, str],
        model: str = "",
    ) -> Attempt | None:
        attempt = self.get(attempt_id)
        if attempt is None:
            return None
        attempt.marks, attempt.missing = dict(marks), dict(missing)
        attempt.wrong, attempt.overall, attempt.model = list(wrong), overall, model
        attempt.checked = _now()
        self.anchors.update({k: _norm(v) for k, v in anchors.items()})
        self.save()
        return attempt

    def remove(self, attempt_id: str) -> None:
        attempt = self.get(attempt_id)
        if attempt is not None:
            self.attempts.remove(attempt)
            self.save()

    def last_score(self, topic: int) -> tuple[float, int] | None:
        """The newest marked score of one topic."""
        if self.sheet is None:
            return None
        attempt = next((a for a in self.marked() if topic in a.topics), None)
        return attempt.score([topic], self.sheet) if attempt is not None else None

    # -- re-extracted layouts --------------------------------------------------------

    def bind(self, layout: PaperLayout) -> bool:
        """Move each citation whose sentence id now names other text back onto
        the sentence it was made for. Returns whether anything moved."""
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
        for point in self.sheet.points() if self.sheet is not None else []:
            point.refs = [moves.get(i, i) for i in point.refs]
        for attempt in self.attempts:
            for wrong in attempt.wrong:
                wrong.refs = [moves.get(i, i) for i in wrong.refs]
        self.anchors = {moves.get(k, k): v for k, v in self.anchors.items()}
        self.save()
        log.info("Re-anchored %d blank-recall citations to the new layout", len(moves))
        return True
