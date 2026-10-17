"""What a paper is, once its layout has been read off the PDF.

The shape of this model is what makes the two panes line up. A page is a list
of paragraphs; a paragraph is a rectangle, the words inside it in reading
order, and the sentences those words form. Nothing here knows about Korean,
about Qt or about GPT: the translation is a separate file keyed by sentence id
(§5.4), so a paper can be re-extracted without losing its translation and
translated without re-reading the PDF.

Everything is JSON-serialisable. ``layout.json`` is the cache of an extraction
that takes a second or two per paper, and the reason reopening is instant.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .geometry import Rect, merge_into_lines, union_all

__all__ = [
    "Block",
    "BlockKind",
    "MATH_TOKEN_END",
    "MATH_TOKEN_START",
    "PageLayout",
    "PaperLayout",
    "Sentence",
    "TRANSLATABLE_KINDS",
    "Word",
    "math_token",
]

#: Inline formulas are taken out of the text before it is translated and put
#: back as the original pixels afterwards (§1.4). The brackets are characters
#: no paper uses, so a placeholder can never collide with real content, and
#: they survive a round trip through GPT intact.
MATH_TOKEN_START = "⟦"
MATH_TOKEN_END = "⟧"


def math_token(index: int) -> str:
    return f"{MATH_TOKEN_START}m{index}{MATH_TOKEN_END}"


class BlockKind:
    """What a paragraph is, which decides whether it is translated.

    The distinction is not cosmetic: a block that is not translated keeps the
    original pixels, so misclassifying a paragraph as a figure leaves English
    on the page, while misclassifying a figure's axis labels as a paragraph
    would paint Korean over the plot.
    """

    TITLE = "title"
    HEADING = "heading"
    BODY = "body"
    CAPTION = "caption"
    #: Author lines, affiliations, the reference list: text that means the same
    #: in any language and reads worse translated.
    VERBATIM = "verbatim"
    #: Running heads, page numbers, the copyright strip.
    FURNITURE = "furniture"
    #: A display equation, on its own line(s).
    EQUATION = "equation"
    #: Text that lives inside a figure, plot or table.
    GRAPHIC = "graphic"


TRANSLATABLE_KINDS = frozenset(
    {BlockKind.TITLE, BlockKind.HEADING, BlockKind.BODY, BlockKind.CAPTION}
)


@dataclass(frozen=True, slots=True)
class Word:
    """One whitespace-separated token of a paragraph, with where it is drawn.

    ``rects`` holds more than one rectangle only for a word the PDF broke
    across a line with a hyphen: "regu-" / "larization" is one word to the
    reader, to the dictionary and to the sentence it belongs to, but two strips
    on the page.
    """

    text: str
    rects: tuple[Rect, ...]
    #: True for a formula placeholder standing in for pixels.
    math: bool = False

    @property
    def rect(self) -> Rect:
        merged = union_all(self.rects)
        assert merged is not None  # a Word is never built without a rectangle
        return merged

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"t": self.text, "r": [r.as_list() for r in self.rects]}
        if self.math:
            data["m"] = True
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Word:
        return cls(
            text=data["t"],
            rects=tuple(Rect.from_list(r) for r in data["r"]),
            math=bool(data.get("m", False)),
        )


@dataclass(frozen=True, slots=True)
class Sentence:
    """One sentence of the original, as a range of its paragraph's words.

    Held as indices rather than rectangles so that the source side has exactly
    one representation: the words. The strips to highlight are derived from
    them (:meth:`rects`), which cannot drift out of step with the text.
    """

    id: str
    #: Index of the first word, and one past the last.
    first: int
    last: int
    text: str

    def words(self, block: Block) -> list[Word]:
        return block.words[self.first : self.last]

    def rects(self, block: Block) -> list[Rect]:
        """One rectangle per line the sentence covers (§2.2)."""
        boxes = [rect for word in self.words(block) for rect in word.rects]
        return merge_into_lines(boxes)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "a": self.first, "b": self.last, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Sentence:
        return cls(id=data["id"], first=data["a"], last=data["b"], text=data["text"])


@dataclass
class Block:
    """A paragraph, heading, caption, equation or piece of figure text."""

    id: str
    page: int
    kind: str
    rect: Rect
    words: list[Word]
    sentences: list[Sentence]
    #: The paragraph's text, which is ``" ".join(word.text ...)`` - the join
    #: that makes a sentence's offsets and its word indices the same thing.
    text: str
    #: Median size of the body spans, in points: the size Korean starts at.
    font_size: float
    #: Distance between the baselines of the original lines. Reusing it keeps
    #: the translated paragraph on the same lines as the English (§7.1).
    leading: float
    #: Indent of the first line relative to the paragraph, in points.
    indent: float = 0.0
    align: str = "justify"
    #: Original text colour, 0xRRGGBB.
    color: int = 0
    bold: bool = False
    italic: bool = False
    #: Whether the original face has serifs, which picks a Myeongjo (serif) or
    #: Gothic (sans) Korean face so a translated page keeps its typographic tone.
    serif: bool = True
    #: Which column of the page, left to right. Full-width blocks are -1.
    column: int = 0
    #: Where each formula placeholder came from, keyed by its token.
    math: dict[str, Rect] = field(default_factory=dict)
    #: The baseline of the line each formula was drawn on. Without it a
    #: subscript would be re-planted sitting on the new baseline instead of
    #: hanging below it, and every "x_i" in the paper would read as "xi".
    math_baseline: dict[str, float] = field(default_factory=dict)
    #: Rectangle of each original line, for diagnostics and for the fallback
    #: when a paragraph cannot be laid out at all.
    line_rects: list[Rect] = field(default_factory=list)

    @property
    def translatable(self) -> bool:
        return self.kind in TRANSLATABLE_KINDS and bool(self.sentences)

    def word_at(self, x: float, y: float, *, tolerance: float = 0.0) -> Word | None:
        """The word under a point, or the nearest one within ``tolerance``."""
        best: Word | None = None
        best_distance = tolerance
        for word in self.words:
            for rect in word.rects:
                distance = rect.distance_to(x, y)
                if distance == 0.0:
                    return word
                if distance <= best_distance:
                    best, best_distance = word, distance
        return best

    def sentence_of_word(self, index: int) -> Sentence | None:
        for sentence in self.sentences:
            if sentence.first <= index < sentence.last:
                return sentence
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "kind": self.kind,
            "rect": self.rect.as_list(),
            "words": [word.to_dict() for word in self.words],
            "sentences": [sentence.to_dict() for sentence in self.sentences],
            "text": self.text,
            "font_size": round(self.font_size, 2),
            "leading": round(self.leading, 2),
            "indent": round(self.indent, 2),
            "align": self.align,
            "color": self.color,
            "bold": self.bold,
            "italic": self.italic,
            "serif": self.serif,
            "column": self.column,
            "math": {token: rect.as_list() for token, rect in self.math.items()},
            "math_baseline": {token: round(y, 2) for token, y in self.math_baseline.items()},
            "lines": [rect.as_list() for rect in self.line_rects],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Block:
        return cls(
            id=data["id"],
            page=int(data["page"]),
            kind=data["kind"],
            rect=Rect.from_list(data["rect"]),
            words=[Word.from_dict(w) for w in data["words"]],
            sentences=[Sentence.from_dict(s) for s in data["sentences"]],
            text=data["text"],
            font_size=float(data["font_size"]),
            leading=float(data["leading"]),
            indent=float(data.get("indent", 0.0)),
            align=data.get("align", "justify"),
            color=int(data.get("color", 0)),
            bold=bool(data.get("bold", False)),
            italic=bool(data.get("italic", False)),
            serif=bool(data.get("serif", True)),
            column=int(data.get("column", 0)),
            math={token: Rect.from_list(r) for token, r in data.get("math", {}).items()},
            math_baseline={
                token: float(y) for token, y in data.get("math_baseline", {}).items()
            },
            line_rects=[Rect.from_list(r) for r in data.get("lines", [])],
        )


@dataclass
class PageLayout:
    """One page: its size in points, its paragraphs, and what must not move."""

    number: int
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)
    #: Figures, plots, tables and anything else whose pixels are kept. The
    #: translated page re-blits these over any paint that touches them, so an
    #: over-eager whiteout can never eat a figure (§1.2).
    protected: list[Rect] = field(default_factory=list)
    #: Left and right edge of each text column, for diagnostics.
    columns: list[tuple[float, float]] = field(default_factory=list)

    @property
    def rect(self) -> Rect:
        return Rect(0.0, 0.0, self.width, self.height)

    def block(self, block_id: str) -> Block | None:
        for block in self.blocks:
            if block.id == block_id:
                return block
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "width": round(self.width, 2),
            "height": round(self.height, 2),
            "blocks": [block.to_dict() for block in self.blocks],
            "protected": [rect.as_list() for rect in self.protected],
            "columns": [[round(a, 1), round(b, 1)] for a, b in self.columns],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PageLayout:
        return cls(
            number=int(data["number"]),
            width=float(data["width"]),
            height=float(data["height"]),
            blocks=[Block.from_dict(b) for b in data["blocks"]],
            protected=[Rect.from_list(r) for r in data.get("protected", [])],
            columns=[(float(a), float(b)) for a, b in data.get("columns", [])],
        )


@dataclass
class PaperLayout:
    """Every page of one paper, plus the version that produced it.

    ``version`` is compared on load: when the extractor improves, an old
    ``layout.json`` is thrown away and the PDF read again. The translation
    survives that, because it is keyed by sentence id and sentence ids are
    derived from positions in the document, not from the extractor's internals.
    """

    #: Raise when a change to extraction should invalidate cached layouts.
    VERSION = 8

    pages: list[PageLayout] = field(default_factory=list)
    version: int = VERSION

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def sentences(self) -> Iterator[tuple[PageLayout, Block, Sentence]]:
        """Every source sentence of the paper, in reading order."""
        for page in self.pages:
            for block in page.blocks:
                if not block.translatable:
                    continue
                for sentence in block.sentences:
                    yield page, block, sentence

    def sentence_count(self) -> int:
        return sum(1 for _ in self.sentences())

    def locate(self, sentence_id: str) -> tuple[PageLayout, Block, Sentence] | None:
        for page, block, sentence in self.sentences():
            if sentence.id == sentence_id:
                return page, block, sentence
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "pages": [page.to_dict() for page in self.pages]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperLayout:
        return cls(
            pages=[PageLayout.from_dict(p) for p in data.get("pages", [])],
            version=int(data.get("version", 0)),
        )
