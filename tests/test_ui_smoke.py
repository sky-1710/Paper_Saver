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
