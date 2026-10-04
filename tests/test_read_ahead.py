"""Read-ahead translation: only the pages around the one in view."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from tests.conftest import LOREM


class _CountingEngine:
    key = "fake"
    model = ""
    supports_images = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        self.prompts.append(prompt)
        numbers = re.findall(r"^\[(\d+)\]", prompt.split("번역할 문장:")[-1], re.M)
        return "\n".join(f"[{n}] 번역 {n}" for n in numbers)


def _wait(qapp, condition, timeout: float = 10.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def _paper(tmp_path: Path, pages: int) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(pymupdf.Rect(60, 60, 552, 720), f"Page {number + 1}. " + LOREM * 3, fontsize=10)
    path = tmp_path / "long.pdf"
    doc.save(path)
    return path


def test_read_ahead_translates_near_the_page_in_view(qapp, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _page_sentences, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("translate_ahead", 1)
    settings.translation_enabled = True
    window = MainWindow(settings)
    window._engine = _CountingEngine()  # type: ignore[assignment]
    project, layout = _prepare(_paper(tmp_path, 6), None, settings.projects_dir)(threading.Event(), lambda _m: None)
    store = project.translations

    def translated(number: int) -> bool:
        return all(store.settled(s.id) for s in _page_sentences(layout, number))

    def idle() -> bool:
        task = window._translations.get(window.reader)
        return task is None or not task.running

    window._opened((project, layout))
    reader = window.reader
    _wait(qapp, lambda: idle() and translated(0) and translated(1))
    assert not any(translated(n) for n in (2, 3, 4, 5))

    # Moving on translates the new neighbourhood, not the pages skipped.
    reader._page = 4
    reader.pageChanged.emit(4)
    _wait(qapp, lambda: idle() and all(translated(n) for n in (3, 4, 5)))
    assert not translated(2)

    # Asking for the whole paper fills in the rest.
    window.translate_whole_paper()
    _wait(qapp, lambda: idle() and translated(2))
    window.close()
