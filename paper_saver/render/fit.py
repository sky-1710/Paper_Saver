"""Setting a translated paragraph into the rectangle the original occupied (§7.1).

The rectangle is fixed. That is the promise the right-hand pane makes - same
page, same place, same size - and it rules out the easy answer to "the Korean
is longer than the English", which is to let the paragraph grow. Instead:

1. Try the original size and the original line spacing. When the Korean fits,
   the translated paragraph sits on exactly the same lines as the English and
   the two panes line up line for line.
2. Shrink in small steps until it fits, keeping words whole (Korean wraps
   between 어절, the way Korean is normally set).
3. Only if that runs out, allow breaks between any two syllables - the tighter
   setting Korean newspapers use - and keep shrinking.
4. If even the floor size does not fit, set it at the floor and say so; the UI
   shows the full sentence on hover, so nothing is lost, only clipped.

Formula placeholders (``⟦m3⟧``) are laid out as boxes the width of the
original formula, scaled with the text, and hung at the same distance from the
baseline they had in the original - a subscript stays a subscript.

Everything here is arithmetic over a :class:`Metrics` object, so it is tested
with a fake font and used with a real one (:mod:`paper_saver.render.fonts`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..core.geometry import Rect
from ..core.models import MATH_TOKEN_END, MATH_TOKEN_START

__all__ = [
    "BlockLayout",
    "LaidLine",
    "Metrics",
    "Piece",
    "fit_block",
]

#: Size steps tried, as fractions of the original size.
SCALES = (1.0, 0.97, 0.94, 0.91, 0.88, 0.85, 0.82, 0.79, 0.76, 0.73, 0.7, 0.66, 0.62, 0.58, 0.54, 0.5)
#: Below this fraction of the original size a formula crop stops shrinking with
#: the text. Past it, mathematics becomes unreadable long before text does.
MATH_FLOOR = 0.8
#: Justification never stretches a gap beyond this many space-widths; a line
#: that would need more is left ragged instead of looking like a ladder.
MAX_STRETCH = 2.2

_TOKEN_RE = re.compile(f"({re.escape(MATH_TOKEN_START)}m\\d+{re.escape(MATH_TOKEN_END)})")
#: Characters that may not start a line.
_NO_START = set(",.;:!?)]}%'\"”’」』〉》、。，．！？：；…·")
#: Characters that may not end a line.
_NO_END = set("([{'\"“‘「『〈《")


#: Where the baseline sits in the em box. Fitting and highlighting both work
#: in em boxes rather than in the font's own ascent and descent: a Korean face
#: reports an ascent a third larger than the Latin face it replaces, and using
#: it would make every one-line heading "not fit" at its original size.
EM_ASCENT = 0.88
EM_DESCENT = 0.12


class Metrics(Protocol):
    """What the layout needs to know about a font: how wide text is."""

    def width(self, text: str, size: float) -> float:
        """Advance width of ``text`` at ``size`` points."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One thing to draw: a run of text, or a formula crop."""

    x: float
    width: float
    sentence: str
    text: str = ""
    #: Set for a formula: the token, and where to draw its crop.
    math: str = ""
    math_rect: Rect | None = None


@dataclass(slots=True)
class LaidLine:
    baseline: float
    pieces: list[Piece] = field(default_factory=list)


@dataclass(slots=True)
class BlockLayout:
    """A paragraph, set: what to draw, and where each sentence ended up."""

    rect: Rect
    size: float
    leading: float
    lines: list[LaidLine]
    #: One rectangle per line a sentence covers - the right pane's half of the
    #: hover-highlight pair (§2.2).
    sentence_rects: dict[str, list[Rect]]
    #: True when even the floor size did not fit and the text was clipped.
    overflow: bool = False
    #: True when syllable-level breaking was needed to fit.
    tight: bool = False


# -- units: the things the line breaker moves around ------------------------


@dataclass(slots=True)
class _Segment:
    text: str
    math: str
    width: float


@dataclass(slots=True)
class _Unit:
    """An unbreakable run. ``space`` is whether a space precedes it."""

    segments: list[_Segment]
    width: float
    sentence: str
    space: bool


def _is_cjk(character: str) -> bool:
    code = ord(character)
    return (
        0xAC00 <= code <= 0xD7A3  # Hangul syllables
        or 0x1100 <= code <= 0x11FF
        or 0x3130 <= code <= 0x318F
        or 0x4E00 <= code <= 0x9FFF  # CJK ideographs
        or 0x3000 <= code <= 0x303F  # CJK punctuation
        or 0xFF00 <= code <= 0xFFEF  # full-width forms
    )


def _pieces_of_word(word: str) -> list[str]:
    """A word split into its text runs and formula tokens, in order."""
    return [part for part in _TOKEN_RE.split(word) if part]


def _breakable_chunks(word: str) -> list[str]:
    """Split a word where Korean may break inside it (break-all mode).

    Hangul breaks between any two syllables; a run of Latin letters, digits or
    a formula token never breaks; punctuation stays with the character before
    it (_NO_START) or after it (_NO_END).
    """
    chunks: list[str] = []
    for part in _pieces_of_word(word):
        if part.startswith(MATH_TOKEN_START):
            chunks.append(part)
            continue
        current = ""
        for character in part:
            if current and _is_cjk(character) and character not in _NO_START:
                if current[-1] not in _NO_END:
                    chunks.append(current)
                    current = ""
            elif current and _is_cjk(current[-1]) and not _is_cjk(character):
                if character not in _NO_START and current[-1] not in _NO_END:
                    chunks.append(current)
                    current = ""
            current += character
        if current:
            chunks.append(current)
    # Glue punctuation that would otherwise start a chunk onto its neighbour.
    glued: list[str] = []
    for chunk in chunks:
        if glued and chunk[0] in _NO_START:
            glued[-1] += chunk
        else:
            glued.append(chunk)
    return glued


def _units(
    sentences: Sequence[tuple[str, str]],
    size: float,
    metrics: Metrics,
    math_widths: Mapping[str, float],
    *,
    break_all: bool,
) -> list[_Unit]:
    units: list[_Unit] = []
    for sentence_id, text in sentences:
        for index, word in enumerate(text.split()):
            chunks = _breakable_chunks(word) if break_all else [word]
            for position, chunk in enumerate(chunks):
                segments: list[_Segment] = []
                for part in _pieces_of_word(chunk):
                    if part.startswith(MATH_TOKEN_START):
                        # A placeholder the original never had is GPT's
                        # invention; drawing "⟦m9⟧" as text would be worse
                        # than leaving it out.
                        if part in math_widths:
                            segments.append(_Segment("", part, math_widths[part]))
                        continue
                    segments.append(_Segment(part, "", metrics.width(part, size)))
                if not segments:
                    continue
                units.append(
                    _Unit(
                        segments=segments,
                        width=sum(segment.width for segment in segments),
                        sentence=sentence_id,
                        # The first chunk of every word but the paragraph's
                        # first is preceded by a space; the rest are glued on.
                        space=position == 0 and (index > 0 or bool(units)),
                    )
                )
    return units


# -- breaking and placing ---------------------------------------------------


def _break_lines(units: Sequence[_Unit], width: float, space: float, indent: float) -> list[list[_Unit]]:
    """Greedy first-fit. Optimal (Knuth-Plass) breaking buys little here:
    the paragraph is already being squeezed to fit, and greedy is what every
    word processor the reader has ever used does."""
    lines: list[list[_Unit]] = []
    current: list[_Unit] = []
    used = indent
    for unit in units:
        add = unit.width + (space if current and unit.space else 0.0)
        if current and used + add > width + 0.01:
            lines.append(current)
            current = []
            used = 0.0
            add = unit.width
        current.append(unit)
        used += add
    if current:
        lines.append(current)
    return lines


def _natural_width(line: Sequence[_Unit], space: float) -> float:
    return sum(u.width for u in line) + sum(space for i, u in enumerate(line) if i and u.space)


def _place(
    groups: Sequence[Sequence[_Unit]],
    rect: Rect,
    size: float,
    leading: float,
    indent: float,
    align: str,
    space: float,
    metrics: Metrics,
    math_rects: Mapping[str, Rect],
    math_baselines: Mapping[str, float],
    math_scale: float,
) -> tuple[list[LaidLine], dict[str, list[Rect]]]:
    lines: list[LaidLine] = []
    spans: dict[str, list[Rect]] = {}
    ascent, descent = size * EM_ASCENT, size * EM_DESCENT
    baseline = rect.y0 + ascent

    for number, group in enumerate(groups):
        first = number == 0
        last = number == len(groups) - 1
        start = rect.x0 + (indent if first else 0.0)
        available = rect.x1 - start
        natural = _natural_width(group, space)

        gaps = [i for i, unit in enumerate(group) if i and unit.space]
        extra_per_gap = 0.0
        if align == "justify" and not last and natural < available:
            slack = available - natural
            if gaps and slack / len(gaps) <= space * MAX_STRETCH:
                extra_per_gap = slack / len(gaps)
            elif not gaps and len(group) > 1 and slack / (len(group) - 1) <= space * MAX_STRETCH:
                # A syllable-broken line has no spaces; spread between units.
                gaps = list(range(1, len(group)))
                extra_per_gap = slack / len(gaps)

        if align == "center":
            start = rect.x0 + max(0.0, (rect.width - natural) / 2)
        elif align == "right":
            start = rect.x1 - natural

        line = LaidLine(baseline=baseline)
        pen = start
        gap_set = set(gaps)
        for index, unit in enumerate(group):
            if index:
                if unit.space:
                    pen += space
                if index in gap_set:
                    pen += extra_per_gap
            unit_start = pen
            for segment in unit.segments:
                if segment.math:
                    source = math_rects[segment.math]
                    offset = source.y0 - math_baselines.get(segment.math, source.y1)
                    top = baseline + offset * math_scale
                    target = Rect(pen, top, pen + segment.width, top + source.height * math_scale)
                    line.pieces.append(
                        Piece(pen, segment.width, unit.sentence, math=segment.math, math_rect=target)
                    )
                elif segment.text:
                    line.pieces.append(Piece(pen, segment.width, unit.sentence, text=segment.text))
                pen += segment.width
            box = Rect(unit_start, baseline - ascent, pen, baseline + descent)
            rects = spans.setdefault(unit.sentence, [])
            if rects and abs(rects[-1].y0 - box.y0) < 0.01:
                rects[-1] = rects[-1].union(box)
            else:
                rects.append(box)
        lines.append(line)
        baseline += leading
    return lines, spans


def fit_block(
    rect: Rect,
    sentences: Sequence[tuple[str, str]],
    metrics: Metrics,
    *,
    font_size: float,
    leading: float,
    indent: float = 0.0,
    align: str = "justify",
    math_rects: Mapping[str, Rect] | None = None,
    math_baselines: Mapping[str, float] | None = None,
    min_scale: float = SCALES[-1],
) -> BlockLayout:
    """Set ``sentences`` (``(id, text)`` pairs) inside ``rect``.

    ``font_size`` and ``leading`` are the original paragraph's, and are where
    the search starts. The result never extends below ``rect.y1`` unless
    ``overflow`` is set.
    """
    math_rects = dict(math_rects or {})
    math_baselines = dict(math_baselines or {})
    original_leading = max(leading, font_size)
    attempt: BlockLayout | None = None

    for break_all in (False, True):
        for scale in SCALES:
            if scale < min_scale - 1e-9:
                break
            size = font_size * scale
            line_gap = max(original_leading * scale, size * 1.12)
            math_scale = max(scale, MATH_FLOOR)
            math_widths = {token: r.width * math_scale for token, r in math_rects.items()}
            units = _units(sentences, size, metrics, math_widths, break_all=break_all)
            space = metrics.width(" ", size)
            first_indent = indent * scale
            groups = _break_lines(units, rect.width, space, first_indent)

            # One em for the glyphs plus a line gap per extra line: the same
            # measure the original paragraph's rectangle was built from.
            needed = size + line_gap * (len(groups) - 1)
            widest = max((u.width for u in units), default=0.0)
            fits = needed <= rect.height + 0.75 and widest <= rect.width + 0.5

            lines, spans = _place(
                groups, rect, size, line_gap, first_indent, align, space,
                metrics, math_rects, math_baselines, math_scale,
            )
            attempt = BlockLayout(
                rect=rect, size=size, leading=line_gap, lines=lines,
                sentence_rects=spans, overflow=not fits, tight=break_all,
            )
            if fits:
                return attempt

    assert attempt is not None
    return attempt
