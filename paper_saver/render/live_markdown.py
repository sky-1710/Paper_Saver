"""Markdown read line by line, for an editor that formats as it is typed.

The note stays Markdown source: what the reader types is what the file holds,
character for character. The editor only *draws* it formatted - a heading
large, ``**bold**`` bold with its asterisks hidden, an image line as the
picture - and shows the markers again on the line being edited. This module
says what is where; ``ui.live_markdown`` decides how it looks.

Qt's own Markdown round trip was not used because it rewrites the file:
``**굵게**는`` loses its asterisks (CommonMark's flanking rule, which Korean
particles trip constantly), ``\\frac`` doubles its backslash, long lines are
wrapped at 80 columns. Here nothing is rewritten, and the inline rules are
deliberately looser than CommonMark so that Korean bold works.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from ..core.notebook import image_refs
from .markdown_display import MATH_FONTS, latex_to_html

__all__ = [
    "CODE", "MATH", "NORMAL", "Line", "Span", "inline_html", "is_separator", "math_html", "parse_line",
    "table_alignments", "table_cells",
]

#: Block state carried from one line to the next.
NORMAL, CODE, MATH = 0, 1, 2


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    #: ``marker`` (hidden off the edited line), ``bold``, ``italic``,
    #: ``strike``, ``code``, ``math``, ``link``, ``dim``.
    role: str


@dataclass
class Line:
    #: ``text``, ``heading``, ``bullet``, ``ordered``, ``task``, ``quote``,
    #: ``rule``, ``fence``, ``code``, ``math``, ``image``, ``table``.
    kind: str
    state: int = NORMAL
    level: int = 0
    #: The block marker (``# ``, ``- ``, ``> ``, ``- [ ] ``) as a range.
    marker: tuple[int, int] | None = None
    #: Where the ``[ ]`` / ``[x]`` box of a task is, and whether it is ticked.
    box: int = -1
    checked: bool = False
    #: An image line: the link target and its alt text.
    target: str = ""
    alt: str = ""
    wiki: bool = False
    spans: list[Span] = field(default_factory=list)
    #: Link targets by span start, for Ctrl+click.
    links: dict[int, str] = field(default_factory=dict)
    #: Inline formulas: (start, end) of ``$...$`` and the LaTeX inside.
    maths: list[tuple[int, int, str]] = field(default_factory=list)


_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})(?:\s+|$)")
_RULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_TASK = re.compile(r"^(\s*)[-*+]\s+\[([ xX])\](?:\s+|$)")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_ORDERED = re.compile(r"^(\s*)\d{1,9}[.)]\s+")
_QUOTE = re.compile(r"^\s{0,3}(?:>\s?)+")

_ESCAPE = re.compile(r"\\[\\`*_{}\[\]()#+\-.!~$|<>]")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_INLINE_DISPLAY = re.compile(r"(?<!\\)\$\$(?=[^$\n]*\S)([^$\n]+?)\$\$")
_INLINE_MATH = re.compile(r"(?<![\\$])\$(?=[^\s$])([^$\n]*?[^\s\\$])?\$(?![$\d])")
_LINK = re.compile(r"(!?)\[([^\]\n]*)\]\((<[^>\n]*>|[^)\s]*)(?:\s+\"[^\"\n]*\")?\)")
_URL = re.compile(r"https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"]")
_BOLD = re.compile(r"(\*\*|__)(?=[^\s\x00])(.+?)(?<=[^\s\x00])\1")
_STAR_ITALIC = re.compile(r"(?<!\*)\*(?=[^\s*\x00])(.+?)(?<=[^\s*\x00])\*(?!\*)")
_UNDER_ITALIC = re.compile(r"(?<![\w_])_(?=[^\s_\x00])(.+?)(?<=[^\s_\x00])_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def parse_line(text: str, state: int = NORMAL) -> Line:
    """What one line of a note is, given the state the line before left."""
    if state == CODE:
        if _FENCE.match(text):
            return Line("fence", NORMAL, spans=[Span(0, len(text), "dim")])
        return Line("code", CODE)
    stripped = text.strip()
    if state == MATH:
        if stripped.endswith("$$"):
            return Line("math", NORMAL, spans=[Span(0, len(text), "math")])
        return Line("math", MATH, spans=[Span(0, len(text), "math")])
    if _FENCE.match(text):
        return Line("fence", CODE, spans=[Span(0, len(text), "dim")])
    if stripped.startswith("$$"):
        closed = len(stripped) > 2 and stripped.endswith("$$")
        return Line("math", NORMAL if closed else MATH, spans=[Span(0, len(text), "math")])
    image = _image_line(text)
    if image is not None:
        return image
    if stripped.startswith("|"):
        return Line("table", spans=[Span(0, len(text), "table")])

    line = Line("text")
    m = _HEADING.match(text)
    if m:
        line = Line("heading", level=len(m.group(1)), marker=(0, m.end()))
    elif _RULE.match(text):
        return Line("rule", marker=(0, len(text)))
    elif m := _TASK.match(text):
        box = text.index("[", len(m.group(1)))
        line = Line("task", level=len(m.group(1)), marker=(len(m.group(1)), m.end()), box=box,
                    checked=m.group(2) in "xX")
    elif m := _BULLET.match(text):
        line = Line("bullet", level=len(m.group(1)), marker=(len(m.group(1)), m.end()))
    elif m := _ORDERED.match(text):
        line = Line("ordered", level=len(m.group(1)), marker=(len(m.group(1)), m.end()))
    elif m := _QUOTE.match(text):
        line = Line("quote", level=m.group(0).count(">"), marker=(0, m.end()))
    start = line.marker[1] if line.marker else 0
    line.spans, line.links, line.maths = _inline(text, start)
    return line


def _image_line(text: str) -> Line | None:
    """A line that is nothing but one image link is drawn as the image."""
    refs = image_refs(text)
    if len(refs) != 1:
        return None
    ref = refs[0]
    if text[: ref.start].strip() or text[ref.end :].strip():
        return None
    return Line("image", target=ref.target, alt=ref.alt, wiki=ref.wiki, spans=[Span(0, len(text), "dim")])


def _inline(text: str, offset: int) -> tuple[list[Span], dict[int, str], list[tuple[int, int, str]]]:
    """Inline spans in ``text[offset:]``, in positions of the whole line."""
    spans: list[Span] = []
    links: dict[int, str] = {}
    # A working copy in which consumed characters are blanked out (\0), so
    # that what is inside a code span or a URL is not read as emphasis.
    work = list(text)

    def take(start: int, end: int) -> None:
        work[start:end] = "\x00" * (end - start)

    def scan(pattern: re.Pattern[str]) -> list[re.Match[str]]:
        return list(pattern.finditer("".join(work), offset))

    for m in scan(_ESCAPE):
        spans.append(Span(m.start(), m.start() + 1, "marker"))
        take(m.start(), m.end())
    for m in scan(_CODE_SPAN):
        tick = len(m.group(1))
        spans += [Span(m.start(), m.start() + tick, "marker"), Span(m.start() + tick, m.end() - tick, "code"),
                  Span(m.end() - tick, m.end(), "marker")]
        take(m.start(), m.end())
    maths: list[tuple[int, int, str]] = []
    # ``$$...$$`` inside a sentence is a formula like ``$...$``.
    for pattern, width in ((_INLINE_DISPLAY, 2), (_INLINE_MATH, 1)):
        for m in scan(pattern):
            spans += [Span(m.start(), m.start() + width, "dim"), Span(m.start() + width, m.end() - width, "math"),
                      Span(m.end() - width, m.end(), "dim")]
            maths.append((m.start(), m.end(), text[m.start() + width : m.end() - width].strip()))
            take(m.start(), m.end())
    for m in scan(_LINK):
        target = m.group(3).strip("<>")
        if m.group(1):  # an image inside a paragraph: shown as its source
            spans.append(Span(m.start(), m.end(), "dim"))
        else:
            label = m.start(2)
            spans += [Span(m.start(), label, "marker"), Span(label, m.end(2), "link"),
                      Span(m.end(2), m.end(), "marker")]
            links[label] = target
        take(m.start(), m.end())
    for m in scan(_URL):
        spans.append(Span(m.start(), m.end(), "link"))
        links[m.start()] = m.group(0)
        take(m.start(), m.end())
    for pattern, role, width in ((_BOLD, "bold", 2), (_STRIKE, "strike", 2),
                                 (_STAR_ITALIC, "italic", 1), (_UNDER_ITALIC, "italic", 1)):
        for m in scan(pattern):
            spans += [Span(m.start(), m.start() + width, "marker"), Span(m.start() + width, m.end() - width, role),
                      Span(m.end() - width, m.end(), "marker")]
            # Only the markers are used up: emphasis nests (***both***).
            take(m.start(), m.start() + width)
            take(m.end() - width, m.end())
    return spans, links, sorted(maths)


# -- tables and formulas, as HTML for drawing --------------------------------------

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def table_cells(text: str) -> list[str]:
    """The cells of one table row; ``\\|`` is a pipe inside a cell."""
    row = text.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def is_separator(text: str) -> bool:
    return "-" in text and bool(_SEPARATOR.match(text))


def table_alignments(text: str) -> list[str]:
    """``left``, ``center`` or ``right`` for each column of a separator row."""
    out = []
    for cell in table_cells(text):
        if cell.startswith(":") and cell.endswith(":"):
            out.append("center")
        elif cell.endswith(":"):
            out.append("right")
        else:
            out.append("left")
    return out


def math_html(expression: str) -> str:
    """A LaTeX formula as HTML in a maths face; display lines one per paragraph."""
    lines = [line.strip() for line in latex_to_html(expression).split("<br>")]
    return "<br>".join(f'<span style="font-family: {MATH_FONTS}">{line}</span>' for line in lines if line)


def inline_html(text: str) -> str:
    """One line of Markdown (a table cell) as HTML, markers dropped."""
    spans, _, _ = _inline(text, 0)
    roles: list[set[str]] = [set() for _ in text]
    for span in spans:
        for i in range(span.start, min(span.end, len(text))):
            roles[i].add(span.role)
    tags = {"bold": "b", "italic": "i", "strike": "s", "code": "code"}
    out: list[str] = []
    start = 0
    for i in range(1, len(text) + 1):
        if i < len(text) and roles[i] == roles[start]:
            continue
        run, kinds = text[start:i], roles[start]
        start = i
        if "marker" in kinds or ("dim" in kinds and run.strip("$") == ""):
            continue
        if "math" in kinds:
            out.append(math_html(run))
            continue
        piece = html.escape(run)
        for role, tag in tags.items():
            if role in kinds:
                piece = f"<{tag}>{piece}</{tag}>"
        if "link" in kinds:
            piece = f'<span style="color:#1a73e8; text-decoration: underline">{piece}</span>'
        out.append(piece)
    return "".join(out)
