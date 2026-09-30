"""Rectangles in PDF point space.

Every coordinate in Paper Saver that is not a pixel is a PDF point on a
specific page, y growing downwards, origin at the top-left of the page as
PyMuPDF reports it. Both panes share this space - that is the whole reason the
translated page can claim to be at the same position and the same size as the
original (§1.2) - so a single rectangle type is used from extraction through
layout to painting, and pixels appear only at the moment of drawing.

Pure arithmetic: no Qt, no PyMuPDF.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = ["Rect", "merge_into_lines", "union_all"]


@dataclass(frozen=True, slots=True)
class Rect:
    """An axis-aligned rectangle, ``x0 <= x1`` and ``y0 <= y1``."""

    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, x0: float, y0: float, x1: float, y1: float) -> Rect:
        """Build one from possibly inverted coordinates."""
        return cls(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    @classmethod
    def from_list(cls, values: Sequence[float]) -> Rect:
        return cls.of(float(values[0]), float(values[1]), float(values[2]), float(values[3]))

    def as_list(self) -> list[float]:
        """Rounded for storage: sub-0.01pt precision is noise from the PDF."""
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]

    # -- measurements --------------------------------------------------------

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    # -- combinations -------------------------------------------------------

    def union(self, other: Rect) -> Rect:
        return Rect(
            min(self.x0, other.x0),
            min(self.y0, other.y0),
            max(self.x1, other.x1),
            max(self.y1, other.y1),
        )

    def intersection(self, other: Rect) -> Rect | None:
        x0, y0 = max(self.x0, other.x0), max(self.y0, other.y0)
        x1, y1 = min(self.x1, other.x1), min(self.y1, other.y1)
        return Rect(x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None

    def intersects(self, other: Rect) -> bool:
        return self.intersection(other) is not None

    def inflated(self, dx: float, dy: float | None = None) -> Rect:
        dy = dx if dy is None else dy
        return Rect(self.x0 - dx, self.y0 - dy, self.x1 + dx, self.y1 + dy)

    def covered_by(self, other: Rect) -> float:
        """Fraction of *this* rectangle's area that lies inside ``other``.

        The direction matters: deciding whether a paragraph belongs to a figure
        asks how much of the paragraph the figure swallows, not the reverse.
        """
        if self.area <= 0:
            return 1.0 if other.intersects(self) else 0.0
        overlap = self.intersection(other)
        return 0.0 if overlap is None else overlap.area / self.area

    # -- hit testing --------------------------------------------------------

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def distance_to(self, x: float, y: float) -> float:
        """Euclidean distance from a point to the rectangle; 0 inside it.

        This is what "pointing *near* a sentence" is measured with (§2.3).
        """
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        if dx == 0.0:
            return dy
        if dy == 0.0:
            return dx
        return (dx * dx + dy * dy) ** 0.5


def union_all(rects: Iterable[Rect]) -> Rect | None:
    """The smallest rectangle containing all of them, or ``None`` if empty."""
    total: Rect | None = None
    for rect in rects:
        total = rect if total is None else total.union(rect)
    return total


def merge_into_lines(rects: Sequence[Rect], *, tolerance: float = 0.6) -> list[Rect]:
    """Merge rectangles that sit on the same text line into one per line.

    A sentence is a run of words, and a run of words is drawn as one strip per
    line it crosses. Highlighting wants those strips, not fifty word boxes:
    fewer rectangles means no seams between adjacent words and one hit test per
    line. Two rectangles share a line when they overlap vertically by most of
    the shorter one's height.
    """
    if not rects:
        return []
    ordered = sorted(rects, key=lambda r: (r.y0, r.x0))
    lines: list[Rect] = [ordered[0]]
    for rect in ordered[1:]:
        current = lines[-1]
        shorter = min(current.height, rect.height)
        overlap = min(current.y1, rect.y1) - max(current.y0, rect.y0)
        if shorter > 0 and overlap >= shorter * tolerance:
            lines[-1] = current.union(rect)
        else:
            lines.append(rect)
    return lines
