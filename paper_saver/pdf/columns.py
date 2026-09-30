"""Finding the text columns of a page, and which column a thing is in.

Columns have to be known before lines can be assembled, for a reason that is
easy to miss: in a two-column paper the left and right columns have *different*
baselines a point or two apart, and a line-grouping pass that does not know
about columns happily merges "the loss function is" from the left column with
"decreases monotonically" from the right. The result reads like a ransom note
and translates like one.

The gutter is found by projection: count how many text rectangles cover each
vertical strip of the page, and look for a strip covered by nothing that runs
between the columns. Elements that span the whole width - the title, a wide
figure, a table across both columns - are excluded from the count first, since
they bridge the gutter and would fill it in.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..core.geometry import Rect

__all__ = ["assign_column", "detect_columns"]

#: A gutter must be at least this wide, in points, to be believed. Word spacing
#: and the ragged right edge of a column produce narrower gaps.
MIN_GUTTER = 9.0
#: A column narrower than this fraction of the content width is not a column,
#: it is a margin note or a stray glyph.
MIN_COLUMN_FRACTION = 0.16
#: Rectangles wider than this fraction of the content width span the page.
FULL_WIDTH_FRACTION = 0.62


def detect_columns(
    page: Rect, rects: Sequence[Rect], *, body_size: float = 10.0
) -> list[tuple[float, float]]:
    """The ``(left, right)`` edge of each text column, left to right.

    Always returns at least one column. ``rects`` are the text rectangles of
    the page - lines or spans, either works.
    """
    if not rects:
        return [(page.x0, page.x1)]

    content_left = min(rect.x0 for rect in rects)
    content_right = max(rect.x1 for rect in rects)
    content_width = content_right - content_left
    if content_width <= 0:
        return [(page.x0, page.x1)]

    narrow = [r for r in rects if r.width <= content_width * FULL_WIDTH_FRACTION]
    if len(narrow) < 6:
        # Too little to judge by: a title page, or genuinely one column.
        return [(content_left, content_right)]

    # One bin per point, holding how many lines cover that strip. A paper page
    # is under a thousand points wide, so this is both exact enough and small
    # enough not to need care.
    bins = max(1, int(content_width) + 1)
    counts = [0] * bins
    for rect in narrow:
        start = max(0, int(rect.x0 - content_left))
        end = min(bins, int(rect.x1 - content_left) + 1)
        for index in range(start, end):
            counts[index] += 1

    # Counted rather than merely "empty or not": one stray element reaching
    # across the gutter must not be able to hide it. On a real IEEE first page
    # the "Index Terms" line overhangs the gutter by 13pt, and with an
    # emptiness test that single line merged the two columns of the whole
    # introduction - every paragraph came out as left column and right column
    # interleaved line by line.
    busiest = max(counts)
    if busiest == 0:
        return [(content_left, content_right)]
    # 15%, not less: an ACL first page has four centred title and author lines
    # crossing the gutter against a busiest strip of 45, and at 8% those four
    # lines hid the gutter and merged the abstract with the introduction.
    quiet = max(1.0, busiest * 0.15)

    gutters: list[tuple[int, int]] = []
    run_start: int | None = None
    for index in range(bins):
        if counts[index] <= quiet:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            gutters.append((run_start, index))
            run_start = None
    if run_start is not None:
        gutters.append((run_start, bins))

    gutter_floor = max(MIN_GUTTER, body_size * 0.9)
    total = sum(counts)
    splits: list[float] = []
    for start, end in gutters:
        if end - start < gutter_floor:
            continue
        left = content_left + start
        right = content_left + end
        # A gap at either edge of the content is a margin, not a gutter.
        if left <= content_left + 1 or right >= content_right - 1:
            continue
        # Both sides must carry real text, or this is a wide indent rather than
        # a boundary between columns.
        if sum(counts[:start]) < total * 0.12 or sum(counts[end:]) < total * 0.12:
            continue
        splits.append((left + right) / 2)

    if not splits:
        return [(content_left, content_right)]

    edges = [content_left, *sorted(splits), content_right]
    columns: list[tuple[float, float]] = []
    for left, right in zip(edges, edges[1:]):
        if right - left >= content_width * MIN_COLUMN_FRACTION:
            columns.append((left, right))
    return columns or [(content_left, content_right)]


def assign_column(rect: Rect, columns: Sequence[tuple[float, float]]) -> int:
    """Index of the column ``rect`` sits in, or ``-1`` if it spans several.

    A rectangle counts as belonging to a column when its centre is inside it;
    it counts as spanning when it covers a real share of two columns, which is
    what the title and a double-width figure do. Spanning elements are what
    later divides the page into bands for reading order.
    """
    if len(columns) <= 1:
        return 0

    shares = []
    for index, (left, right) in enumerate(columns):
        width = max(1e-6, right - left)
        overlap = min(rect.x1, right) - max(rect.x0, left)
        shares.append((index, max(0.0, overlap) / width))

    substantial = [index for index, share in shares if share > 0.35]
    if len(substantial) > 1:
        return -1

    for index, (left, right) in enumerate(columns):
        if left <= rect.cx <= right:
            return index
    # Outside every column (a margin note): give it to the nearest one.
    return min(
        range(len(columns)),
        key=lambda i: min(abs(rect.cx - columns[i][0]), abs(rect.cx - columns[i][1])),
    )
