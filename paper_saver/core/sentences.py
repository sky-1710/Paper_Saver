"""Where a paragraph of a paper can be cut into sentences (§2.1).

Everything downstream is per sentence: the translation request, the highlight
pair, the note anchor, the dictionary's idea of "context". A splitter that cuts
"et al." in half therefore does not produce a cosmetic flaw, it produces a
sentence that GPT translates as a fragment and a highlight that lights up half
a line - so this module is deliberately conservative. It only cuts where it is
confident, and a paragraph left whole is a much smaller failure than a
paragraph cut wrongly.

The hard cases in an academic paper, all of which must NOT end a sentence::

    ... reported by Zhong et al. in 2020         "al."
    ... as shown in Fig. 3 and Table 2           "Fig."
    ... i.e. the degree matrix                   "i.e."
    ... an accuracy of 79.37 % on SEED           a decimal point
    ... described by J. R. Smith                 initials
    ... the loss ⟦m4⟧. The next term ...         a formula placeholder is opaque

Offsets, not strings: the caller maps them back to word indices and so to
rectangles on the page, which is only exact if nothing is rewritten here.
"""

from __future__ import annotations

__all__ = ["MIN_SENTENCE_CHARS", "split_sentences"]

#: A cut that would leave a piece shorter than this joins its neighbour
#: instead. Equation tags ("(13)"), stray page numbers and "Fig. 4." captions
#: with nothing after them are all shorter than this.
MIN_SENTENCE_CHARS = 12

_ENDINGS = ".!?…"
#: Closers that belong to the sentence that is ending, not the next one.
_CLOSERS = "\"'”’)]}»›"
#: Characters a new sentence may legitimately start with.
_OPENERS = "\"'“‘([{⟦«‹"

#: Words which, followed by a full stop, almost never end a sentence in a
#: paper. Stored without the stop and lowercased.
_ABBREVIATIONS = frozenset(
    """
    al et etc cf vs viz resp approx ca eg ie
    fig figs tab tabs eq eqs sec secs app apps alg algs def defs thm lem cor
    ref refs no nos vol vols ch chap pp p pt col cols
    mr mrs ms dr prof sr jr st
    inc ltd corp dept univ lab labs
    ieee acm arxiv doi eds ed trans proc conf j
    min max avg std var approx sqrt
    """.split()
)


def _preceding_token(text: str, stop: int) -> str:
    """The run of letters and digits immediately before ``text[stop]``."""
    start = stop
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "-'"):
        start -= 1
    return text[start:stop]


def _is_boundary(text: str, index: int) -> int | None:
    """End offset of a sentence that stops at ``text[index]``, or ``None``.

    ``text[index]`` is one of :data:`_ENDINGS`. The returned offset is just
    after any closing quotes or brackets that belong to it.
    """
    end = index + 1
    length = len(text)
    while end < length and text[end] in _CLOSERS:
        end += 1

    # A stop with no space after it is inside something: "0.5", "e.g.", a URL,
    # a file name, "U.S.A". The one exception is the end of the paragraph.
    if end < length and not text[end].isspace():
        return None

    after = end
    while after < length and text[after].isspace():
        after += 1

    if after < length:
        nxt = text[after]
        # A lower-case word after a stop means the stop was not a full stop:
        # an abbreviation this module has never heard of, or a decimal.
        if not (nxt.isupper() or nxt.isdigit() or nxt in _OPENERS or not nxt.isascii()):
            return None

    if text[index] == ".":
        token = _preceding_token(text, index)
        lowered = token.lower()
        if lowered in _ABBREVIATIONS:
            return None
        # "J. R. Smith", "W. " - a single letter and a stop is an initial.
        if len(token) == 1 and token.isalpha():
            return None
        # "Vol. 3.2." and friends: a stop straight after a digit, when the
        # next thing is also a number, is part of a numbering.
        if token.isdigit() and after < length and text[after].isdigit():
            return None

    return end


def split_sentences(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` offsets of each sentence in ``text``, in order.

    The spans are trimmed of surrounding whitespace and never overlap. They
    cover everything except whitespace between sentences, so no word of the
    paragraph is left without a sentence to belong to.
    """
    stripped_len = len(text)
    if not text.strip():
        return []

    spans: list[tuple[int, int]] = []
    start = 0
    index = 0
    while index < stripped_len:
        character = text[index]
        if character not in _ENDINGS:
            index += 1
            continue
        end = _is_boundary(text, index)
        if end is None:
            index += 1
            continue
        piece = text[start:end]
        # Too short to stand alone: let it run on into the next sentence.
        if len(piece.strip()) < MIN_SENTENCE_CHARS and spans:
            index = end
            continue
        if len(piece.strip()) < MIN_SENTENCE_CHARS and not spans:
            index = end
            continue
        spans.append((start, end))
        while end < stripped_len and text[end].isspace():
            end += 1
        start = end
        index = end

    tail = text[start:]
    if tail.strip():
        if spans and len(tail.strip()) < MIN_SENTENCE_CHARS:
            # A trailing "(13)" or "2." belongs to the sentence before it.
            first, _ = spans[-1]
            spans[-1] = (first, len(text.rstrip()))
        else:
            spans.append((start, len(text.rstrip())))

    return [_trimmed(text, span) for span in spans]


def _trimmed(text: str, span: tuple[int, int]) -> tuple[int, int]:
    start, end = span
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end
