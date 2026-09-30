"""Reading a paper's layout off its PDF: spans to lines to paragraphs (§2.1).

PyMuPDF's own "blocks" cannot be used for this. On a LaTeX paper they fracture
around every inline formula and their rectangles overlap each other, so a
paragraph arrives as five overlapping fragments with the words in the wrong
order and the spaces missing. Measured on a real IEEE paper, one paragraph came
back as::

    [  1] 'where D denotes the diagonal degree matrix of A,i.e., Dii=P'
    [  2] 'j Aij. The normalized adjacency matrixD-1'
    [  3] '2 AD-12 prevents H from growing overly large. If weignore ...'

Three "blocks" with overlapping bounding boxes, one real paragraph, and the
formulas shredded across all three. Whiting out those rectangles would erase
each other's text, and translating them would produce three fragments.

So the page is rebuilt from the bottom up, from the one thing PyMuPDF reports
exactly: where each character is drawn.

    characters -> spans -> visual lines (per column) -> paragraphs

A *visual line* is every span sharing a baseline within one column, sorted by x,
with spaces inserted where the gap between characters is wide enough to be one.
That recovers the sentence above as the single line it is on the page, formulas
included. A *paragraph* is a run of visual lines that belong together, judged by
line spacing, indentation, font and - for justified text, which most papers are
- the tell that a line which stops short of the right margin is a last line.

The inline formulas become placeholder tokens (``⟦m1⟧``) whose pixels are kept
and painted back later, so a translated paragraph keeps its mathematics exactly
as the author drew it.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..core.geometry import Rect, union_all
from ..core.models import Block, BlockKind, PageLayout, PaperLayout, Sentence, Word, math_token
from ..core.sentences import split_sentences
from .columns import assign_column, detect_columns
from .layout_model import TEXT, Region, model_regions
from .regions import protected_regions

log = logging.getLogger(__name__)

__all__ = ["extract_page", "extract_paper"]

# -- what counts as mathematics ----------------------------------------------

#: Font families that only ever draw mathematics. Computer Modern's text faces
#: (cmr, cmbx, cmti) are deliberately absent: in a pure-LaTeX paper they are the
#: body text, and treating them as formulas would protect the whole paper from
#: translation.
MATH_FAMILIES = frozenset(
    {
        "cmmi", "cmsy", "cmex", "cmbsy", "msam", "msbm", "eufm", "eusm", "eurm",
        "rsfs", "wasy", "stixmath", "xitsmath", "xitsmathitalic", "lmmath",
        "lmmathitalic", "lmmathsymbols", "mtsy", "mtmi", "mtex", "esint",
        "symbol", "standardsymbolsl", "standardsymbol", "mathematicalpi",
        "cmmib", "cmbsyten", "txsy", "txmi", "rsfs10",
    }
)

_MATH_CHARS = set(
    "αβγδεζηθικλμνξοπρστυφχψωϑϕϖςΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
    "+−–—×÷=≠≈≃≅≡∼∝<>≤≥≪≫±∓∈∉∋⊂⊃⊆⊇∪∩∅∀∃∄¬∧∨⊕⊗⊙⊥∠∥"
    "∑∏∫∬∮∂∇√∞ℓℝℕℤℚℂ→←↔⇒⇐⇔↦⊤⊢⊨·⋅∘∗⋆†‡′″‴|‖⟨⟩⌊⌋⌈⌉^_~ˆ˜¯"
)


def _family(font: str) -> str:
    """Normalised font family: ``ABCDEF+URWPalladioL-Roma`` -> ``urwpalladiol``."""
    name = font.split("+", 1)[-1]
    name = re.split(r"[-,_]", name, maxsplit=1)[0]
    return re.sub(r"[^a-z]", "", name.lower())


def _math_char_ratio(text: str) -> float:
    letters = [c for c in text if not c.isspace()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c in _MATH_CHARS) / len(letters)


# -- the intermediate shapes --------------------------------------------------


@dataclass(slots=True)
class _Char:
    c: str
    rect: Rect


@dataclass(slots=True)
class _Span:
    text: str
    chars: list[_Char]
    rect: Rect
    size: float
    font: str
    flags: int
    color: int
    baseline: float
    math: bool = False

    @property
    def bold(self) -> bool:
        return bool(self.flags & 16)

    @property
    def italic(self) -> bool:
        return bool(self.flags & 2)

    @property
    def superscript(self) -> bool:
        return bool(self.flags & 1)

    @property
    def serif(self) -> bool:
        return bool(self.flags & 4)


@dataclass(slots=True)
class _Line:
    spans: list[_Span]
    rect: Rect
    baseline: float
    size: float
    column: int
    equation: bool = False

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)

    @property
    def bold(self) -> bool:
        weighted = sum(len(s.text) for s in self.spans if s.bold)
        return weighted * 2 > sum(len(s.text) for s in self.spans)


@dataclass(slots=True)
class _PageContext:
    number: int
    rect: Rect
    body_size: float
    body_font: str
    columns: list[tuple[float, float]]
    protected: list[Rect]
    first_page: bool


# -- characters and spans -----------------------------------------------------


def _span_rect(chars: Sequence[_Char], fallback: Any) -> Rect:
    merged = union_all(char.rect for char in chars)
    return merged if merged is not None else Rect.from_list(fallback)


def _page_spans(page: Any) -> list[_Span]:
    """Every span of the page with its characters, ignoring PyMuPDF's blocks.

    Vertical and rotated lines are dropped: nothing in a paper that is set
    sideways is prose, and laying Korean into a rotated rectangle would be the
    one place the geometry could not be preserved.
    """
    raw = page.get_text("rawdict")
    spans: list[_Span] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            direction = tuple(line.get("dir", (1, 0)))
            if abs(direction[0]) < 0.99:
                continue
            for span in line.get("spans", []):
                chars = [
                    _Char(entry["c"], Rect.from_list(entry["bbox"]))
                    for entry in span.get("chars", [])
                    if entry.get("c")
                ]
                if not chars:
                    continue
                text = "".join(char.c for char in chars)
                if not text.strip():
                    continue
                origin = span.get("origin") or (0.0, 0.0)
                spans.append(
                    _Span(
                        text=text,
                        chars=chars,
                        rect=_span_rect(chars, span["bbox"]),
                        size=float(span.get("size", 10.0)),
                        font=str(span.get("font", "")),
                        flags=int(span.get("flags", 0)),
                        color=int(span.get("color", 0)),
                        baseline=float(origin[1]),
                    )
                )
    return spans


def _dominant_body(spans: Sequence[_Span]) -> tuple[str, float]:
    """The font and size most of the page's characters are set in.

    Measured by characters rather than spans so that a hundred one-character
    formula spans cannot outvote the paragraphs they sit in.
    """
    by_key: dict[tuple[str, float], int] = {}
    for span in spans:
        key = (_family(span.font), round(span.size, 1))
        by_key[key] = by_key.get(key, 0) + len(span.text.strip())
    if not by_key:
        return "", 10.0
    (font, size), _ = max(by_key.items(), key=lambda item: item[1])
    return font, size


def _mark_math(spans: Iterable[_Span], body_font: str, body_size: float) -> None:
    for span in spans:
        span.math = _span_is_math(span, body_font, body_size)


def _span_is_math(span: _Span, body_font: str, body_size: float) -> bool:
    family = _family(span.font)
    if family in MATH_FAMILIES:
        return True
    text = span.text.strip()
    if not text:
        return False
    ratio = _math_char_ratio(text)
    if ratio >= 0.5:
        return True

    # Prose set in the page's own body face, at the page's own body size, is
    # prose. This guard has to come before any flag-based rule because PyMuPDF
    # decides "superscript" by comparing a span's baseline with the line it put
    # the span in - and on a line that contains a tall formula it puts them in
    # the wrong line, so it reports ordinary words as superscripts. Measured on
    # a real IEEE paper: "The normalized adjacency matrix" arrived flagged
    # superscript and was swallowed into a formula, losing four words of the
    # paragraph.
    if family == body_font and abs(span.size - body_size) <= body_size * 0.08:
        return False

    if family != body_font:
        if len(text) <= 24 and ratio > 0.12:
            return True
        # A sub- or superscript set in a font other than the body's is an index
        # or an exponent: "H^{l+1}", "D^{-1/2}", "x_i".
        if span.size < body_size * 0.82 and len(text) <= 14:
            return True
    return bool(span.superscript) and len(text) <= 6 and span.size < body_size * 0.95


def _absorb_math_neighbours(spans: list[_Span], body_font: str) -> None:
    """Pull lone variable letters into the formula beside them.

    ``D`` in "D_ii = Σ_j A_ij" is drawn in a maths *text* face, so none of the
    rules above call it a formula, and without this pass the paragraph reads
    "D ⟦m1⟧ = ⟦m2⟧ A ⟦m3⟧" - four fragments where the author wrote one
    expression. Every fragment is a separate crop to place and a separate
    chance to look wrong, so a short span in a face that is not the body's,
    sitting right next to a formula, joins it.
    """
    ordered = sorted(spans, key=lambda span: span.rect.x0)
    for index, span in enumerate(ordered):
        if span.math or _family(span.font) == body_font or len(span.text.strip()) > 3:
            continue
        reach = max(span.size, 1.0) * 1.2
        before = ordered[index - 1] if index else None
        after = ordered[index + 1] if index + 1 < len(ordered) else None
        if before is not None and before.math and span.rect.x0 - before.rect.x1 < reach:
            span.math = True
        elif after is not None and after.math and after.rect.x0 - span.rect.x1 < reach:
            span.math = True


# -- visual lines -------------------------------------------------------------


def _split_row(row: list[_Span], context: _PageContext) -> list[tuple[int, list[_Span]]]:
    """Cut a baseline row where it crosses a gutter, or keep it whole.

    Rows are clustered across the whole page first, because a line and the
    formula fragments that decorate it must be found together and they can sit
    either side of a column boundary. A row then either crosses the gutter with
    a span of its own - the "Abstract—..." paragraph of a two-column paper, a
    running head, a wide caption - in which case it really is one full-width
    line, or it does not, in which case it is two lines that merely share a
    baseline and must be separated before a word of one lands in the other.
    """
    if len(context.columns) <= 1:
        return [(0, row)]

    assigned = [(assign_column(span.rect, context.columns), span) for span in row]
    if any(column < 0 for column, _ in assigned):
        return [(-1, row)]
    # A short centred title line ("Reasoning Models") falls entirely inside one
    # column; left there it becomes a separate one-line "title" and is
    # translated on its own, cut off from the first half of its sentence.
    if context.first_page and all(span.size >= context.body_size * 1.25 for span in row):
        centre = (row[0].rect.x0 + max(span.rect.x1 for span in row)) / 2
        if abs(centre - context.rect.cx) < context.body_size * 2:
            return [(-1, row)]

    by_column: dict[int, list[_Span]] = {}
    for column, span in assigned:
        by_column.setdefault(column, []).append(span)
    return sorted(by_column.items())


def _split_drop_caps(
    spans: Sequence[_Span], context: _PageContext
) -> tuple[list[_Span], list[_Span]]:
    """Separate initial capitals from the text, to re-place them by hand.

    A drop capital cannot be found by baseline: a two-line initial sits on the
    *second* line's baseline, 1pt away from it, so no clustering tolerance can
    tell it from an ordinary word there. Left in place it reads as the paper
    beginning "MOTION recognition focuses on the recognition of hu- Eman
    emotions" - the "E" of "Emotion" landing between "hu-" and "man".
    """
    caps: list[_Span] = []
    rest: list[_Span] = []
    for span in spans:
        column = assign_column(span.rect, context.columns)
        left = context.columns[column][0] if 0 <= column < len(context.columns) else context.rect.x0
        if (
            span.size > context.body_size * 1.6
            and len(span.text.strip()) <= 2
            and span.text.strip().isalpha()
            and span.rect.x0 <= left + context.body_size * 0.6
        ):
            caps.append(span)
        else:
            rest.append(span)
    return rest, caps


def _place_drop_caps(
    caps: Sequence[_Span], per_column: dict[int, list[list[_Span]]], context: _PageContext
) -> None:
    """Put each initial at the front of the first line it stands beside."""
    for cap in caps:
        column = assign_column(cap.rect, context.columns)
        rows = per_column.setdefault(column, [])
        for row in rows:
            box = union_all(span.rect for span in row)
            if box is None or box.x0 < cap.rect.x1 - 1.0:
                continue
            if min(box.y1, cap.rect.y1) > max(box.y0, cap.rect.y0):
                row.append(cap)
                break
        else:
            rows.append([cap])


def _group_lines(spans: Sequence[_Span], context: _PageContext) -> list[_Line]:
    """Cluster spans into the lines a reader sees, column by column."""
    ordinary, caps = _split_drop_caps(spans, context)
    per_column: dict[int, list[list[_Span]]] = {}
    for row in _baseline_rows(ordinary, context):
        for column, piece in _split_row(row, context):
            per_column.setdefault(column, []).append(piece)
    _place_drop_caps(caps, per_column, context)

    lines: list[_Line] = []
    for column, rows in per_column.items():
        lines.extend(_lines_in_column(rows, column, context))
    lines.sort(key=lambda line: (line.baseline, line.rect.x0))
    _attach_short_last_lines(lines, context)
    return lines


def _attach_short_last_lines(lines: list[_Line], context: _PageContext) -> None:
    """Give a full-width paragraph back its last line.

    The final line of a full-width abstract is usually short, so it does not
    reach across the gutter and was filed under the left column - leaving the
    abstract a word short and a one-word "paragraph" beneath it. A line that
    starts where a full-width line directly above it starts, one line-space
    below, with nothing of its own column in between, is that paragraph's.
    """
    if len(context.columns) <= 1:
        return
    for index, line in enumerate(lines):
        if line.column < 0:
            continue
        for above in reversed(lines[:index]):
            if above.baseline >= line.baseline:
                continue
            if above.column == line.column:
                break
            if above.column != -1:
                continue
            gap = line.baseline - above.baseline
            if 0 < gap <= above.size * 1.6 and abs(line.rect.x0 - above.rect.x0) < context.body_size * 0.6:
                line.column = -1
            break


def _baseline_rows(spans: list[_Span], context: _PageContext) -> list[list[_Span]]:
    """Spans that sit on the same baseline, within a fraction of a body size."""
    ordered = sorted(spans, key=lambda span: (span.baseline, span.rect.x0))
    rows: list[list[_Span]] = []
    for span in ordered:
        if rows:
            reference = statistics.median([s.baseline for s in rows[-1]])
            tolerance = max(context.body_size * 0.3, span.size * 0.22)
            if abs(span.baseline - reference) <= tolerance:
                rows[-1].append(span)
                continue
        rows.append([span])
    return rows


#: Words that look like prose but are read aloud as part of a formula. Without
#: this list "z = max(0, x)" counts as a sentence with two words in it.
_FUNCTION_WORDS = frozenset(
    """
    log ln exp sin cos tan sinh cosh tanh max min sup inf lim arg argmax argmin
    softmax sigmoid relu diag det tr trace dim rank var cov std mean sum prod
    sgn sign mod gcd lcm and s.t. subject otherwise
    """.split()
)


def _prose_words(row: Sequence[_Span]) -> int:
    """How many real words a row has, ignoring formulas and function names.

    Three letters is the threshold: "The", "via" and "are" are words, while
    "AD", "(4)", "ii" and "σ" are pieces of mathematics.
    """
    count = 0
    for span in row:
        if span.math:
            continue
        for piece in re.split(r"[^A-Za-z가-힣]+", span.text):
            if len(piece) >= 3 and piece.lower() not in _FUNCTION_WORDS:
                count += 1
    return count


def _has_prose(row: Sequence[_Span]) -> bool:
    return _prose_words(row) >= 1


def _absorb_decorations(
    rows: list[list[_Span]], context: _PageContext
) -> list[list[_Span]]:
    """Fold formula fragments into the text line they decorate.

    A tall inline formula - a summation with limits, a stacked fraction - draws
    parts of itself well above and below the line's baseline, and each part
    arrives here as a row of its own. Left alone they become separate
    "equation lines" that split the paragraph into three, which is exactly the
    failure this whole module exists to avoid: measured on a real paper, one
    sentence came out as three paragraphs and two of them were untranslatable
    fragments.

    A fragment belongs to the text line it *overlaps vertically*, not to the
    nearest baseline. The summation sign in "Dii = Σj Aij" has a baseline 4pt
    above the line it belongs to and 7pt below the line it does not, so
    baseline distance alone picks the wrong one every time; its box, though,
    lies entirely within its own line's box.
    """
    boxes = [union_all(span.rect for span in row) for row in rows]
    prose = [index for index, row in enumerate(rows) if _has_prose(row)]
    if not prose:
        return rows

    merged: dict[int, list[_Span]] = {index: list(rows[index]) for index in prose}
    separate: list[list[_Span]] = []
    ceiling = context.body_size * 2.6

    for index, row in enumerate(rows):
        if index in merged:
            continue
        box = boxes[index]
        if box is None or box.height > ceiling:
            separate.append(row)
            continue
        baseline = statistics.median([span.baseline for span in row])

        best: int | None = None
        best_overlap = 0.0
        for target in prose:
            other = boxes[target]
            if other is None:
                continue
            overlap = min(box.y1, other.y1) - max(box.y0, other.y0)
            fraction = overlap / box.height if box.height > 0 else 0.0
            if fraction > best_overlap:
                best, best_overlap = target, fraction

        if best is not None and best_overlap > 0.3:
            merged[best].extend(row)
            continue

        nearest = min(
            prose,
            key=lambda target: abs(
                baseline - statistics.median([s.baseline for s in rows[target]])
            ),
        )
        distance = abs(baseline - statistics.median([s.baseline for s in rows[nearest]]))
        if distance < context.body_size * 0.9:
            merged[nearest].extend(row)
        else:
            separate.append(row)

    return [merged[index] for index in sorted(merged)] + separate


def _lines_in_column(
    rows: list[list[_Span]], column: int, context: _PageContext
) -> list[_Line]:
    """Assemble one column's rows into lines, decorations folded in."""
    groups = _absorb_decorations(rows, context)

    lines: list[_Line] = []
    for group in groups:
        group.sort(key=lambda span: span.rect.x0)
        _absorb_math_neighbours(group, context.body_font)
        rect = union_all(span.rect for span in group)
        if rect is None:
            continue
        sizes = [span.size for span in group for _ in span.text.strip()]
        line = _Line(
            spans=group,
            rect=rect,
            baseline=statistics.median([span.baseline for span in group]),
            size=statistics.median(sizes) if sizes else context.body_size,
            column=column,
        )
        line.equation = _line_is_equation(line, context)
        lines.append(line)
    return lines


def _line_is_equation(line: _Line, context: _PageContext) -> bool:
    """Whether a whole line is a display formula rather than prose.

    Display formulas are kept as pixels, so this decision has to be made per
    line: a paragraph is often a run of text lines interrupted by one centred
    equation line, and only the equation line must be protected.
    """
    total = sum(len(span.text.strip()) for span in line.spans)
    if not total:
        return False
    # Two real words are enough to make a line prose however much mathematics
    # it carries. "where S = D−1/2AD−1/2 and W = W0W1...WL−1. Essen-" is 60%
    # formula by character count and is still a sentence; calling it an equation
    # cost a real paper a whole paragraph of translation, and broke the
    # hyphenated word that continued onto the next line.
    if _prose_words(line.spans) >= 2:
        return False
    math_chars = sum(len(span.text.strip()) for span in line.spans if span.math)
    if math_chars / total > 0.45:
        return True
    foreign = sum(
        len(span.text.strip())
        for span in line.spans
        if _family(span.font) != context.body_font
    )
    return foreign / total > 0.8 and _math_char_ratio(line.text) > 0.2


# -- paragraphs ---------------------------------------------------------------


@dataclass(slots=True)
class _ColumnStyle:
    left: float
    right: float
    leading: float
    justified: bool


def _common_edge(values: Sequence[float], fallback: float, *, prefer_high: bool) -> float:
    """The edge most lines share, robust to the one line that overhangs it.

    Taking the minimum or maximum is not: on a real first page the "Index
    Terms" line overhangs the left column by 25pt, and with ``max`` as the
    right margin every ordinary line of the column looked short, the column
    looked ragged, and the paragraph rules fell apart for the whole page.
    """
    if not values:
        return fallback
    counts: dict[int, int] = {}
    for value in values:
        key = round(value)
        counts[key] = counts.get(key, 0) + 1
    edge, frequency = max(counts.items(), key=lambda item: (item[1], item[0] if prefer_high else -item[0]))
    if frequency >= max(2, len(values) * 0.25):
        return float(edge)
    ordered = sorted(values)
    index = int(len(ordered) * (0.9 if prefer_high else 0.1))
    return ordered[min(index, len(ordered) - 1)]


def _column_style(lines: Sequence[_Line], bounds: tuple[float, float], body: float) -> _ColumnStyle:
    left = _common_edge([line.rect.x0 for line in lines], bounds[0], prefer_high=False)
    right = _common_edge([line.rect.x1 for line in lines], bounds[1], prefer_high=True)
    deltas = [
        b.baseline - a.baseline
        for a, b in zip(lines, lines[1:])
        if 0 < b.baseline - a.baseline < body * 3
    ]
    leading = statistics.median(deltas) if deltas else body * 1.2
    flush = sum(1 for line in lines if line.rect.x1 >= right - 2.0)
    return _ColumnStyle(
        left=left,
        right=right,
        leading=leading,
        justified=bool(lines) and flush / len(lines) > 0.55,
    )


def _paragraph_break(previous: _Line, current: _Line, style: _ColumnStyle, body: float) -> bool:
    gap = current.baseline - previous.baseline
    if gap > style.leading * 1.75:
        return True
    if previous.equation != current.equation:
        return True
    # A multi-line title is centred, so every line but the last looks "short"
    # and every line looks "indented" - but it is one sentence and must be
    # translated as one.
    large = body * 1.25
    if previous.size >= large and abs(previous.size - current.size) < 0.3 and previous.bold == current.bold:
        return False
    if max(previous.size, current.size) > min(previous.size, current.size) * 1.12:
        return True
    if previous.bold != current.bold:
        return True
    # In justified text only the final line of a paragraph stops short of the
    # right margin, which makes it the most reliable paragraph signal there is.
    if style.justified and not previous.equation:
        if previous.rect.x1 < style.right - max(3.0, body * 0.8):
            return True
    # An indented line starts a paragraph - but only where the short-line rule
    # above cannot be used, because indentation has two other causes. A drop
    # capital pushes the two lines beside it inwards, and text set around a
    # figure is indented for as long as the figure lasts. In justified text the
    # short line has already caught every real paragraph start, so the indent
    # test is pure risk there.
    indented = current.rect.x0 > style.left + body * 0.6
    if style.justified:
        # ...except when the indent comes with extra space above it, which a
        # drop capital or a figure never adds. Needed for the paragraph whose
        # last line happens to run almost to the margin: on an ACL page
        # "...accelerating LRM inference." stopped 5pt short and the next
        # paragraph was swallowed into it.
        return indented and gap > style.leading * 1.12
    return indented


def _group_paragraphs(lines: Sequence[_Line], context: _PageContext) -> list[list[_Line]]:
    per_column: dict[int, list[_Line]] = {}
    for line in lines:
        per_column.setdefault(line.column, []).append(line)

    paragraphs: list[list[_Line]] = []
    for column, column_lines in per_column.items():
        column_lines.sort(key=lambda line: line.baseline)
        bounds = (
            context.columns[column]
            if 0 <= column < len(context.columns)
            else (context.rect.x0, context.rect.x1)
        )
        style = _column_style(column_lines, bounds, context.body_size)
        current: list[_Line] = []
        for line in column_lines:
            if current and _paragraph_break(current[-1], line, style, context.body_size):
                paragraphs.append(current)
                current = []
            current.append(line)
        if current:
            paragraphs.append(current)
    return paragraphs


# -- words --------------------------------------------------------------------


def _line_items(line: _Line, body_size: float) -> list[tuple[str, Any]]:
    """The line as a sequence of ``("text", chars)`` and ``("math", rect)``.

    Adjacent formula spans are fused - an exponent, its base and the operator
    after it are one formula, not three - and a space is inserted between spans
    or characters whose gap is wide enough to be one. That gap rule is what
    recovers the spaces PDFs so often do not store.
    """
    items: list[tuple[str, Any]] = []
    previous: _Span | None = None
    for span in line.spans:
        size = max(span.size, body_size * 0.6)
        if span.math:
            gap = span.rect.x0 - previous.rect.x1 if previous else 0.0
            if items and items[-1][0] == "math" and previous is not None and gap < size * 1.2:
                items[-1] = ("math", items[-1][1].union(span.rect))
            else:
                items.append(("math", span.rect))
            previous = span
            continue

        chars: list[_Char] = []
        if items and items[-1][0] == "text":
            chars = items[-1][1]
            gap = span.rect.x0 - previous.rect.x1 if previous else 0.0
            # A drop capital sits snug against the word it starts, and the word
            # is set in a much smaller size - so the gap is measured against the
            # larger of the two, or "EMOTION" would come out as "E MOTION".
            if previous is not None and previous.size > span.size * 1.6:
                size = max(size, previous.size * 0.75)
            if chars and gap > size * 0.2 and not chars[-1].c.isspace():
                chars.append(_Char(" ", Rect(previous.rect.x1, span.rect.y0, span.rect.x0, span.rect.y1)))  # type: ignore[union-attr]
        else:
            items.append(("text", chars))

        for char in span.chars:
            if chars and not chars[-1].c.isspace() and not char.c.isspace():
                gap = char.rect.x0 - chars[-1].rect.x1
                if gap > span.size * 0.25:
                    chars.append(_Char(" ", Rect(chars[-1].rect.x1, char.rect.y0, char.rect.x0, char.rect.y1)))
            chars.append(char)
        previous = span
    return items


_HYPHENS = "-‐­"


def _words_of_paragraph(
    lines: Sequence[_Line], body_size: float
) -> tuple[list[Word], dict[str, Rect], dict[str, float]]:
    """The paragraph's words in reading order, plus where its formulas were.

    A word hyphenated across a line break becomes one word with two
    rectangles: the reader, the dictionary and the sentence all want
    "regularization", while the page draws "regu-" and "larization" (§Word).
    """
    words: list[Word] = []
    math: dict[str, Rect] = {}
    baselines: dict[str, float] = {}

    for index, line in enumerate(lines):
        line_words: list[Word] = []
        for kind, payload in _line_items(line, body_size):
            if kind == "math":
                token = math_token(len(math) + 1)
                math[token] = payload
                baselines[token] = line.baseline
                line_words.append(Word(token, (payload,), math=True))
                continue
            for text, rect in _split_chars(payload):
                line_words.append(Word(text, (rect,)))

        # Join a word the PDF broke with a hyphen onto the next line.
        if (
            words
            and line_words
            and index > 0
            and not words[-1].math
            and not line_words[0].math
            and len(words[-1].text) > 1
            and words[-1].text[-1] in _HYPHENS
            and line_words[0].text[:1].isalpha()
            and line_words[0].text[:1].islower()
        ):
            head = words.pop()
            tail = line_words.pop(0)
            words.append(Word(head.text[:-1] + tail.text, head.rects + tail.rects))

        words.extend(line_words)

    return words, math, baselines


def _split_chars(chars: Sequence[_Char]) -> list[tuple[str, Rect]]:
    """Split a character stream into whitespace-separated words with boxes."""
    words: list[tuple[str, Rect]] = []
    current: list[_Char] = []
    for char in chars:
        if char.c.isspace():
            if current:
                words.append(_word_of(current))
                current = []
            continue
        current.append(char)
    if current:
        words.append(_word_of(current))
    return words


def _word_of(chars: Sequence[_Char]) -> tuple[str, Rect]:
    rect = union_all(char.rect for char in chars)
    assert rect is not None
    return "".join(char.c for char in chars), rect


# -- sentences ----------------------------------------------------------------


def _sentences_of(words: Sequence[Word], text: str, block_id: str) -> list[Sentence]:
    """Cut the paragraph into sentences and map each to a range of words.

    ``text`` is ``" ".join(word.text ...)``, so a character offset and a word
    index are two views of the same thing and the mapping is exact.
    """
    starts: list[int] = []
    offset = 0
    for word in words:
        starts.append(offset)
        offset += len(word.text) + 1

    sentences: list[Sentence] = []
    for index, (start, end) in enumerate(split_sentences(text), start=1):
        first = 0
        for position, word_start in enumerate(starts):
            if word_start <= start:
                first = position
            else:
                break
        last = first
        for position in range(first, len(words)):
            if starts[position] < end:
                last = position
            else:
                break
        sentences.append(
            Sentence(
                id=f"{block_id}s{index}",
                first=first,
                last=last + 1,
                text=text[start:end],
            )
        )
    return sentences


# -- classification -----------------------------------------------------------

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?s?|tab(?:le)?s?|alg(?:orithm)?s?|listing|scheme|chart)"
    r"\s*\.?\s*(\d+|[IVXL]+)\b",
    re.IGNORECASE,
)
_REFERENCES_RE = re.compile(
    r"^\s*(?:[0-9IVX]+\.?\s*)?(references?|bibliography|참고\s*문헌)\s*$", re.IGNORECASE
)
_TOKEN_STRIP = re.compile("⟦m\\d+⟧")
_ABSTRACT_RE = re.compile(r"^\s*(abstract|초록|요약)\b", re.IGNORECASE)
_NUMBERED_HEADING_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVXL]+\.|[A-Z]\.)\s+\S")


def _classify(
    lines: Sequence[_Line],
    rect: Rect,
    text: str,
    words: Sequence[Word],
    context: _PageContext,
    hint: str | None = None,
) -> str:
    stripped = text.strip()
    if not stripped:
        return BlockKind.GRAPHIC
    # A dingbat, a bullet, a lone number: nothing a translation could improve,
    # and GPT will happily "translate" the ornament "✦" into "!".
    if not re.search(r"[A-Za-z]{2,}", _TOKEN_STRIP.sub(" ", stripped)):
        return BlockKind.EQUATION if _TOKEN_STRIP.search(stripped) else BlockKind.VERBATIM

    height = context.rect.height
    size = statistics.median([line.size for line in lines])
    inside = max((rect.covered_by(region) for region in context.protected), default=0.0)

    if len(lines) == 1 and len(stripped) < 140:
        if rect.y1 <= height * 0.062 or rect.y0 >= height * 0.94:
            return BlockKind.FURNITURE

    if _CAPTION_RE.match(stripped) and inside < 0.9 and size <= context.body_size * 1.25:
        return BlockKind.CAPTION

    # Before the model's hint: it labels some titles "section-header".
    # 1.25x, not more: ACL and many conference styles set the title only 30%
    # larger than the body, and at 1.5x it was taken for an author line.
    if context.first_page and size >= context.body_size * 1.25 and rect.y0 < height * 0.3:
        return BlockKind.TITLE

    # The layout model's word, where it has one. Formula regions have already
    # had their prose lines split off, so an EQUATION hint is safe to take.
    if hint in (BlockKind.FURNITURE, BlockKind.GRAPHIC, BlockKind.EQUATION, BlockKind.CAPTION):
        return hint
    if hint == BlockKind.TITLE:
        return BlockKind.TITLE
    if hint == BlockKind.HEADING and len(stripped) <= 200:
        return BlockKind.HEADING

    if all(line.equation for line in lines):
        return BlockKind.EQUATION
    math_words = sum(1 for word in words if word.math)
    if words and math_words / len(words) > 0.6:
        return BlockKind.EQUATION
    # A "paragraph" holding formulas and no real words is a display equation
    # whose pieces were not all flagged: "X ∗G = U((U T G) ⊙(U T X)) = U ˆGU T X"
    # has plenty of characters and not one word to translate.
    if math_words:
        prose_words = sum(
            1
            for word in words
            if not word.math and sum(1 for c in word.text if c.isalpha()) >= 4
        )
        if prose_words < 2:
            return BlockKind.EQUATION

    if inside > 0.6:
        return BlockKind.GRAPHIC


    if hint == TEXT:
        # The model saw a paragraph. The rules below would call a paragraph
        # opening with a bold run-in phrase ("Unreliable LRM judgment: ...")
        # a heading; the model knows better.
        return BlockKind.BODY

    looks_like_heading = (
        len(stripped) <= 120
        and len(lines) <= 2
        and not stripped.endswith((".", ";", ","))
        and (
            lines[0].bold
            or size > context.body_size * 1.06
            or bool(_NUMBERED_HEADING_RE.match(stripped))
        )
    )
    if looks_like_heading:
        return BlockKind.HEADING

    return BlockKind.BODY


# -- assembling a page --------------------------------------------------------


def _build_block(
    lines: Sequence[_Line], index: int, context: _PageContext, hint: str | None = None
) -> Block | None:
    rect = union_all(line.rect for line in lines)
    if rect is None:
        return None

    words, math, math_baselines = _words_of_paragraph(lines, context.body_size)
    if not words:
        return None

    text = " ".join(word.text for word in words)
    block_id = f"p{context.number}b{index}"
    kind = _classify(lines, rect, text, words, context, hint)

    first = lines[0]
    sizes = [line.size for line in lines]
    baselines = [line.baseline for line in lines]
    deltas = [b - a for a, b in zip(baselines, baselines[1:]) if b > a]
    body_lines = [line for line in lines if not line.equation] or list(lines)
    leading = statistics.median(deltas) if deltas else statistics.median(sizes) * 1.2

    left = min(line.rect.x0 for line in lines)
    bounds = (
        context.columns[first.column]
        if 0 <= first.column < len(context.columns)
        else (context.rect.x0, context.rect.x1)
    )
    align = "justify"
    if len(lines) == 1 or kind in (BlockKind.TITLE, BlockKind.EQUATION):
        centred = abs((rect.x0 + rect.x1) / 2 - (bounds[0] + bounds[1]) / 2) < context.body_size
        align = "center" if centred and rect.width < (bounds[1] - bounds[0]) * 0.97 else "left"
    elif kind in (BlockKind.HEADING, BlockKind.CAPTION):
        align = "left"

    dominant = max(
        (span for line in body_lines for span in line.spans if not span.math),
        key=lambda span: len(span.text),
        default=first.spans[0],
    )

    block = Block(
        id=block_id,
        page=context.number,
        kind=kind,
        rect=rect,
        words=words,
        sentences=[],
        text=text,
        font_size=statistics.median([line.size for line in body_lines]),
        leading=leading,
        indent=max(0.0, first.rect.x0 - left),
        align=align,
        color=dominant.color,
        bold=dominant.bold,
        italic=dominant.italic,
        serif=dominant.serif,
        column=first.column,
        math=math,
        math_baseline=math_baselines,
        line_rects=[line.rect for line in lines],
    )
    if block.kind in {BlockKind.TITLE, BlockKind.HEADING, BlockKind.BODY, BlockKind.CAPTION}:
        block.sentences = _sentences_of(words, text, block_id)
    return block


def _paragraphs_from_regions(
    lines: Sequence[_Line], regions: Sequence[Region], context: _PageContext
) -> list[tuple[list[_Line], str | None]]:
    """Group lines by the layout model's regions (see :mod:`.layout_model`).

    Each line goes to the region that contains most of it. Inside a region,
    runs of equation lines and prose lines are separated, because the model
    sometimes draws one box around "By passing each X into our RGNN, the
    output probability is" and the display formula under it - the sentence must
    be translated and the formula must not. Lines no region claims are grouped
    by the geometric rules, so nothing on the page is dropped.
    """
    claimed: dict[int, list[_Line]] = {}
    leftovers: list[_Line] = []
    for line in lines:
        best, best_cover = -1, 0.0
        for index, region in enumerate(regions):
            cover = line.rect.covered_by(region.rect.inflated(1.5))
            if cover > best_cover:
                best, best_cover = index, cover
        if best >= 0 and best_cover >= 0.5:
            claimed.setdefault(best, []).append(line)
        else:
            leftovers.append(line)

    result: list[tuple[list[_Line], str | None]] = []
    for index in sorted(claimed):
        region = regions[index]
        region_lines = sorted(claimed[index], key=lambda line: (line.baseline, line.rect.x0))
        if region.kind not in (TEXT, BlockKind.EQUATION):
            result.append((region_lines, region.kind))
            continue
        run: list[_Line] = []
        for line in region_lines:
            if run and run[-1].equation != line.equation:
                result.append((run, BlockKind.EQUATION if run[-1].equation else TEXT))
                run = []
            run.append(line)
        if run:
            result.append((run, BlockKind.EQUATION if run[-1].equation else TEXT))

    if leftovers:
        result += [(paragraph, None) for paragraph in _group_paragraphs(leftovers, context)]
    return result


def _reading_order(blocks: list[Block], context: _PageContext) -> list[Block]:
    """Sort into the order a person reads: bands, then columns, then down.

    Anything spanning the columns - the title, a wide figure, a table across
    the page - divides what is above it from what is below, which is the whole
    of the classic XY-cut rule and enough for every paper layout in practice.
    """
    boundaries = sorted(block.rect.y0 for block in blocks if block.column == -1)

    def band(block: Block) -> int:
        return sum(1 for boundary in boundaries if boundary <= block.rect.y0 + 0.5)

    return sorted(
        blocks,
        key=lambda block: (band(block), 0 if block.column < 0 else 1, max(block.column, 0), block.rect.y0, block.rect.x0),
    )


def extract_page(page: Any, number: int) -> PageLayout:
    """Read one page into paragraphs, sentences and protected regions."""
    page_rect = Rect.from_list(list(page.rect))
    spans = _page_spans(page)
    if not spans:
        return PageLayout(
            number=number,
            width=page_rect.width,
            height=page_rect.height,
            protected=protected_regions(page),
        )

    body_font, body_size = _dominant_body(spans)
    _mark_math(spans, body_font, body_size)
    columns = detect_columns(page_rect, [span.rect for span in spans], body_size=body_size)
    context = _PageContext(
        number=number,
        rect=page_rect,
        body_size=body_size,
        body_font=body_font,
        columns=columns,
        protected=protected_regions(page),
        first_page=number == 0,
    )

    lines = _group_lines(spans, context)
    regions = model_regions(page)
    if regions:
        # Tables and pictures the model found are protected like any other.
        context.protected.extend(r.rect for r in regions if r.kind == BlockKind.GRAPHIC)
        paragraphs = _paragraphs_from_regions(lines, regions, context)
    else:
        paragraphs = [(paragraph, None) for paragraph in _group_paragraphs(lines, context)]

    blocks: list[Block] = []
    for paragraph, hint in paragraphs:
        block = _build_block(paragraph, len(blocks), context, hint)
        if block is not None:
            blocks.append(block)

    layout = PageLayout(
        number=number,
        width=page_rect.width,
        height=page_rect.height,
        blocks=_reading_order(blocks, context),
        protected=context.protected,
        columns=columns,
    )
    _mark_front_matter(layout, body_size)
    return layout


def _mark_front_matter(layout: PageLayout, body_size: float) -> None:
    """Author names and affiliations are left in the original (§1.4).

    Everything between the title and the abstract on the first page is names,
    institutions and email addresses. Translating a name is worse than useless:
    it makes the paper harder to cite.
    """
    if layout.number != 0:
        return

    # The small print along the bottom of a first page - "Manuscript received
    # ...", "The authors are with ...", email addresses - is boilerplate whose
    # translation nobody reads, and it is a tenth of the paper's first page.
    for block in layout.blocks:
        if (
            block.rect.y0 > layout.height * 0.82
            and block.font_size <= body_size * 0.92
            and block.kind in (BlockKind.BODY, BlockKind.HEADING)
        ):
            block.kind = BlockKind.VERBATIM
            block.sentences = []

    abstract_at: int | None = None
    for index, block in enumerate(layout.blocks):
        if _ABSTRACT_RE.match(block.text):
            abstract_at = index
            break
    if abstract_at is None:
        return
    for block in layout.blocks[:abstract_at]:
        if block.kind in (BlockKind.BODY, BlockKind.HEADING):
            block.kind = BlockKind.VERBATIM
            block.sentences = []


def _mark_references(layout: PaperLayout) -> None:
    """From the "References" heading to the end of the paper, keep the original.

    A bibliography translated into Korean cannot be looked up, and it is a
    quarter of the sentences in a typical paper - so this also cuts the cost of
    translating one by a quarter (§5.1).
    """
    found = False
    for page in layout.pages:
        for block in page.blocks:
            if not found and _REFERENCES_RE.match(block.text.strip()):
                found = True
            if found and block.kind not in (BlockKind.GRAPHIC, BlockKind.FURNITURE):
                block.kind = BlockKind.VERBATIM
                block.sentences = []


def _mark_running_heads(layout: PaperLayout) -> None:
    """Running heads and footers: whatever repeats in the margins, page to page.

    A fixed margin band cannot find these. IEEE pages start their columns at
    y=44, so a band deep enough to catch ACM's running head at y=62 swallows
    the first line of every IEEE column. Repetition is the real signature: the
    same short line, digits aside, near the top or bottom of several pages.
    """
    def signature(block: Block) -> str:
        return re.sub(r"\d+", "#", " ".join(block.text.split())).lower()

    def in_margin(block: Block, page: PageLayout) -> bool:
        return block.rect.y1 <= page.height * 0.11 or block.rect.y0 >= page.height * 0.9

    seen: dict[str, set[int]] = {}
    for page in layout.pages:
        for block in page.blocks:
            if in_margin(block, page) and len(block.line_rects) <= 2 and len(block.text) < 160:
                seen.setdefault(signature(block), set()).add(page.number)

    threshold = max(2, int(len(layout.pages) * 0.3))
    repeated = {key for key, pages in seen.items() if len(pages) >= threshold}
    for page in layout.pages:
        for block in page.blocks:
            if not in_margin(block, page):
                continue
            bare = re.sub(r"[\s⟦⟧m\d.,/|–—-]", "", block.text)
            if signature(block) in repeated or not bare:
                block.kind = BlockKind.FURNITURE
                block.sentences = []


def extract_paper(document: Any, *, pages: Iterable[int] | None = None) -> PaperLayout:
    """Read every page of an open PyMuPDF document."""
    numbers = list(pages) if pages is not None else range(document.page_count)
    layout = PaperLayout(pages=[])
    for number in numbers:
        try:
            layout.pages.append(extract_page(document[number], number))
        except Exception:
            log.exception("Page %s could not be read; it stays untranslated", number + 1)
            page = document[number]
            layout.pages.append(
                PageLayout(number=number, width=page.rect.width, height=page.rect.height)
            )
    _mark_running_heads(layout)
    _mark_references(layout)
    return layout
