"""Extraction on a synthetic paper, and the fitting engine with a fake font."""

from __future__ import annotations

from pathlib import Path

import pytest

from paper_saver.core.geometry import Rect
from paper_saver.core.models import BlockKind, PaperLayout
from paper_saver.pdf.columns import assign_column, detect_columns
from paper_saver.pdf.document import PaperDocument
from paper_saver.pdf.extract import extract_paper
from paper_saver.render.fit import fit_block


class FakeMetrics:
    """Hangul one em wide, everything else half an em."""

    def width(self, text: str, size: float) -> float:
        return sum(size if "가" <= c <= "힣" else size * 0.5 for c in text)


# -- columns ---------------------------------------------------------------------------


def test_gutter_survives_one_overhanging_line() -> None:
    lines = [Rect(48, y, 300, y + 9) for y in range(100, 700, 12)]
    lines += [Rect(312, y, 564, y + 9) for y in range(100, 700, 12)]
    lines.append(Rect(68, 80, 325, 89))  # "Index Terms" reaching across
    columns = detect_columns(Rect(0, 0, 612, 792), lines, body_size=9.5)
    assert len(columns) == 2
    assert assign_column(Rect(48, 0, 300, 9), columns) == 0
    assert assign_column(Rect(60, 0, 550, 9), columns) == -1


# -- extraction -------------------------------------------------------------------------


def test_two_columns_are_read_separately(two_column_pdf: Path) -> None:
    with PaperDocument(two_column_pdf) as document:
        layout = extract_paper(document.raw)
    page = layout.pages[0]
    assert len(page.columns) == 2
    kinds = {block.kind for block in page.blocks}
    assert BlockKind.TITLE in kinds
    bodies = [b for b in page.blocks if b.kind == BlockKind.BODY and b.column >= 0]
    # No paragraph may contain text from both columns.
    for block in bodies:
        assert not ("Column 1" in block.text and "Column 2" in block.text)
    assert any(b.text.endswith("Column 1 ends here.") for b in bodies)
    # Reading order: the left column's end comes before the right column's.
    texts = [b.text for b in page.blocks]
    first = next(i for i, t in enumerate(texts) if "Column 1 ends" in t)
    second = next(i for i, t in enumerate(texts) if "Column 2 ends" in t)
    assert first < second


def test_sentences_map_to_exact_words(two_column_pdf: Path) -> None:
    with PaperDocument(two_column_pdf) as document:
        layout = extract_paper(document.raw)
    for _, block, sentence in layout.sentences():
        joined = " ".join(w.text for w in sentence.words(block))
        assert joined == sentence.text
        assert sentence.rects(block)


def test_layout_round_trips_through_json(two_column_pdf: Path) -> None:
    with PaperDocument(two_column_pdf) as document:
        layout = extract_paper(document.raw)
    again = PaperLayout.from_dict(layout.to_dict())
    assert [s.id for _, _, s in again.sentences()] == [s.id for _, _, s in layout.sentences()]


# -- fitting (§7.1) ------------------------------------------------------------------------


def test_fits_at_original_size_when_there_is_room() -> None:
    rect = Rect(0, 0, 200, 60)
    laid = fit_block(rect, [("s1", "가나다 라마바 사아자")], FakeMetrics(), font_size=10, leading=12)
    assert laid.size == 10 and not laid.overflow
    assert laid.sentence_rects["s1"][0].x0 == 0


def test_shrinks_but_never_grows_the_rectangle() -> None:
    rect = Rect(0, 0, 100, 24)
    text = " ".join(["가나다라"] * 30)
    laid = fit_block(rect, [("s1", text)], FakeMetrics(), font_size=10, leading=12)
    assert laid.size < 10
    bottom = max(r.y1 for rects in laid.sentence_rects.values() for r in rects)
    assert laid.overflow or bottom <= rect.y1 + 1


def test_formula_keeps_its_offset_from_the_baseline() -> None:
    rect = Rect(0, 0, 300, 40)
    math = {"⟦m1⟧": Rect(0, 12, 8, 16)}  # a subscript hanging 2pt below baseline 10
    laid = fit_block(
        rect, [("s1", "값 ⟦m1⟧ 는 작다")], FakeMetrics(), font_size=10, leading=12,
        math_rects=math, math_baselines={"⟦m1⟧": 10.0},
    )
    piece = next(p for line in laid.lines for p in line.pieces if p.math)
    assert piece.math_rect is not None
    assert piece.math_rect.y0 == pytest.approx(laid.lines[0].baseline + 2.0)


def test_invented_formula_tokens_are_dropped() -> None:
    laid = fit_block(Rect(0, 0, 300, 40), [("s1", "값 ⟦m9⟧ 이다")], FakeMetrics(), font_size=10, leading=12)
    assert all("⟦" not in p.text for line in laid.lines for p in line.pieces)


def test_two_sentences_get_separate_rects() -> None:
    laid = fit_block(
        Rect(0, 0, 400, 40), [("a", "첫 문장이다."), ("b", "둘째 문장이다.")], FakeMetrics(),
        font_size=10, leading=12,
    )
    assert set(laid.sentence_rects) == {"a", "b"}
    assert laid.sentence_rects["a"][0].x1 <= laid.sentence_rects["b"][0].x0
