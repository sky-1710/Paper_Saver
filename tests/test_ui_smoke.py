"""The window opens a paper, links hover across panes, and paints both sides."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent


def test_open_hover_and_paint(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()

    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    ids = [s.id for _, _, s in layout.sentences()]
    project.translations.update({ids[0]: "합성 논문에 대한 연구"})
    window._opened((project, layout))
    qapp.processEvents()

    reader = window.reader
    assert reader is not None
    ref = window.session.sentence(ids[0])
    rect = ref.sentence.rects(ref.block)[0]
    placed = reader.left._placed[ref.page]
    pos = QPointF(placed.x + rect.cx * reader.left.zoom, placed.y + rect.cy * reader.left.zoom)
    event = QMouseEvent(
        QMouseEvent.Type.MouseMove, pos, pos, Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
    )
    reader.left.mouseMoveEvent(event)
    assert reader.right._hover == ids[0]
    assert "합성 논문" in reader.pair_target.text()
    assert window.session.rects_of(ids[0], translated=True)

    image = window.grab()
    assert not image.isNull()
    window.close()


def test_exported_pdf_keeps_searchable_text(qapp, tmp_path: Path, two_column_pdf: Path) -> None:
    import pymupdf

    from paper_saver.render.export import translated_pdf
    from paper_saver.ui.main_window import _prepare

    project, layout = _prepare(two_column_pdf, None, tmp_path / "projects")(threading.Event(), lambda _m: None)
    first, *_ = [(s.id, s.text) for _, _, s in layout.sentences()]
    project.translations.update({first[0]: "합성 논문에 대한 연구"})
    with pymupdf.open(two_column_pdf) as source:
        original = source[0].get_text()
        untouched = next(w[4] for w in source[0].get_text("words") if w[4] not in first[1] and w[4].isalpha())

    for side_by_side in (False, True):
        target = tmp_path / f"out-{side_by_side}.pdf"
        translated_pdf(project, layout, target, side_by_side=side_by_side)
        with pymupdf.open(target) as document:
            page = document[0]
            text = page.get_text()
            assert "합성 논문에 대한 연구" in text  # the Korean can be found
            assert page.search_for("합성 논문")
            assert untouched in text  # and so can what was left as the original
            if side_by_side:
                left = page.get_text(clip=pymupdf.Rect(0, 0, page.rect.width / 2 - 9, page.rect.height))
                assert left.split() == original.split()  # the paper's own page, text and all
