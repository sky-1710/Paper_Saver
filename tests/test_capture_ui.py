"""Capture a region, paste it in the note, ask GPT about it, put the answer in the note."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent


class _FakeEngine:
    supports_images = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Path]]] = []

    def ask(self, prompt: str, *, images=(), cancel: threading.Event) -> str:  # type: ignore[no-untyped-def]
        self.calls.append((prompt, list(images)))
        return "이 그림은 **정확도** 곡선입니다."


def _mouse(kind, pos: QPointF, button=Qt.MouseButton.LeftButton):  # type: ignore[no-untyped-def]
    buttons = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, pos, pos, button, buttons, Qt.KeyboardModifier.NoModifier)


def _wait(qapp, condition, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    import time

    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def test_capture_note_and_chat(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
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
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _FakeEngine()
    reader._engine_provider = lambda: engine

    # A drag in capture mode reports a rectangle in page points.
    captured: list[tuple[int, object]] = []
    reader._on_capture = lambda *_a, **_k: None  # the popup menu would block the test
    reader.left.regionCaptured.connect(lambda page, rect: captured.append((page, rect)))
    reader.left.set_capture_mode(True)
    placed = reader.left._placed[0]
    z = reader.left.zoom
    start = QPointF(placed.x + 60 * z, placed.y + 50 * z)
    end = QPointF(placed.x + 300 * z, placed.y + 170 * z)
    reader.left.mousePressEvent(_mouse(QMouseEvent.Type.MouseButtonPress, start))
    reader.left.mouseMoveEvent(_mouse(QMouseEvent.Type.MouseMove, end))
    reader.left.mouseReleaseEvent(_mouse(QMouseEvent.Type.MouseButtonRelease, end))
    reader.left.set_capture_mode(False)
    assert len(captured) == 1
    page, rect = captured[0]
    assert page == 0 and abs(rect.x0 - 60) < 1 and abs(rect.y1 - 170) < 1

    image = window.session.capture(page, rect, translated=False)
    assert image.width() > 240 * 2  # rendered sharper than the screen

    # Into the note: saved beside it, linked at the cursor, saved to disk.
    reader.use_capture(image, 0, "p.1 원문", "note")
    note = reader.note_panel
    text = note.editor.toPlainText()
    assert "![p.1 원문](assets/capture-p1-" in text
    assert list((project.root / "assets").glob("capture-p1-*.png"))
    note.save()
    assert (project.root / "note.md").read_text(encoding="utf-8") == text

    # Into the chat, asked about, answered, and the answer into the note.
    reader.use_capture(image, 0, "p.1 원문", "ask")
    assert reader.chat_panel.pending
    reader.chat_panel.input.setPlainText("이 부분 설명해줘")
    reader.chat_panel.send()
    _wait(qapp, lambda: len(reader.chat_log.messages) == 2)
    prompt, images = engine.calls[0]
    assert "이 부분 설명해줘" in prompt and "A Study of Synthetic Papers" in prompt
    assert len(images) == 1 and images[0].is_file()
    assert "정확도" in reader.chat_panel.view.toPlainText()

    # A follow-up with no picture re-sends the one just asked about.
    reader.chat_panel.input.setPlainText("x축은?")
    reader.chat_panel.send()
    _wait(qapp, lambda: len(reader.chat_log.messages) == 4)
    assert engine.calls[1][1] == images

    reader.answer_to_note(reader.chat_log.messages[1].id)
    text = note.editor.toPlainText()
    assert "> **Q.** 이 부분 설명해줘" in text and text.rstrip().endswith("이 그림은 **정확도** 곡선입니다.")
    assert text.count("assets/capture-p1-") == 2

    # Moving the note elsewhere takes its pictures along.
    target = tmp_path / "vault" / "my note.md"
    monkeypatch.setattr(
        "paper_saver.ui.note_panel.QFileDialog.getSaveFileName", lambda *a, **k: (str(target), "")
    )
    note.save_as()
    assert target.read_text(encoding="utf-8") == note.editor.toPlainText()
    assert len(list((target.parent / "assets").glob("*.png"))) == 2
    assert project.meta["note_file"] == str(target)

    window.close()
    # Reopened, the conversation is still there.
    from paper_saver.core.chat import ChatLog

    assert len(ChatLog(project.root / "chat.json").messages) == 4


def test_paste_prefers_text_over_its_picture(qapp) -> None:
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QColor, QImage

    from paper_saver.ui.markdown_view import ImagePasteEdit

    edit = ImagePasteEdit()
    pasted: list[QImage] = []
    edit.imagePasted.connect(pasted.append)
    picture = QImage(20, 10, QImage.Format.Format_RGB32)
    picture.fill(QColor("red"))

    # Copied from Word: the text and a picture of it. The text is what pastes.
    office = QMimeData()
    office.setText("copied words")
    office.setImageData(picture)
    edit.insertFromMimeData(office)
    assert edit.toPlainText() == "copied words" and not pasted

    # A screenshot: only a picture.
    shot = QMimeData()
    shot.setImageData(picture)
    edit.insertFromMimeData(shot)
    assert len(pasted) == 1 and edit.toPlainText() == "copied words"
