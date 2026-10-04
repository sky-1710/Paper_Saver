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


class _CitingEngine:
    supports_images = True

    def ask(self, prompt: str, *, images=(), cancel: threading.Event) -> str:  # type: ignore[no-untyped-def]
        return "첫 문장이 주제를 밝힙니다 [S2]. 없는 번호 [S999]는 무시됩니다."


def test_answer_citations_scroll_and_light_the_sentences(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
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
    reader._engine_provider = lambda: _CitingEngine()

    last = reader.session.page_count - 1
    text, numbering = reader._page_text(last)
    assert numbering and "[S2]" in text
    cited = numbering[2]
    # Asked from the last page, answered while the reader is back at the top.
    reader._page = last
    reader.chat_panel.input.setPlainText("요점이 뭐야?")
    reader.chat_panel.send()
    reader.goto_page(0)
    _wait(qapp, lambda: len(reader.chat_log.messages) == 2)
    answer = reader.chat_log.messages[1]
    assert answer.refs == [cited] and "S999" not in answer.text

    assert reader.left._evidence == [cited] and reader.right._evidence == [cited]
    bar = reader.left_scroll.verticalScrollBar()
    assert bar.value() >= reader.left.page_top(last) - reader.left_scroll.viewport().height() or bar.value() == bar.maximum()
    html = reader.chat_panel.view.toHtml()
    assert f"ref:{cited}" in html and f"p.{last + 1}" in reader.chat_panel.view.toPlainText()

    # A click on a page puts the lights out; the link brings them back.
    reader.left.pressed.emit()
    assert reader.left._evidence == []
    from PySide6.QtCore import QUrl

    reader.chat_panel._link(QUrl(f"ref:{cited}"))
    assert reader.right._evidence == [cited]

    reader.answer_to_note(answer.id)
    assert "[S2]" not in reader.note_panel.editor.toPlainText()
    assert f"주제를 밝힙니다 (p.{last + 1})." in reader.note_panel.editor.toPlainText()
    window.close()


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


def test_sentences_go_to_the_gpt_tab(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare
    from paper_saver.ui.reader_view import EXPLAIN_QUESTION

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    ids = [s.id for _, _, s in layout.sentences()]
    project.translations.update({ids[0]: "합성 논문에 대한 연구"})
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _FakeEngine()
    reader._engine_provider = lambda: engine
    first, second = reader.session.sentence(ids[0]), reader.session.sentence(ids[1])

    # Gathered: two quotes on top, the reader's own words kept below them.
    reader.chat_panel.input.setPlainText("둘의 관계는?")
    reader.quote_to_gpt(ids[0])
    reader.quote_to_gpt(ids[1])
    text = reader.chat_panel.input.toPlainText()
    lines = text.split("\n")
    assert lines[0] == f"> p.{first.page + 1} · {' '.join(first.sentence.text.split())}"
    assert lines[1] == "> (번역) 합성 논문에 대한 연구"
    assert lines[2].startswith(f"> p.{second.page + 1} · ")
    assert text.rstrip().endswith("둘의 관계는?")
    assert reader.side.currentWidget() is reader.chat_panel
    assert not engine.calls  # nothing is sent until the reader says so

    # Asked at once, about the quoted sentence's page whatever page is in view.
    reader.chat_panel.input.clear()
    last = reader.session.page_count - 1
    reader._page = last
    reader.quote_to_gpt(ids[0], ask=True)
    _wait(qapp, lambda: len(reader.chat_log.messages) == 2)
    question = reader.chat_log.messages[0]
    assert question.text.endswith(EXPLAIN_QUESTION) and question.page == first.page
    assert ids[0] not in engine.calls[0][0] and "합성 논문에 대한 연구" in engine.calls[0][0]
    window.close()


def test_whole_paper_button_sends_every_page(qapp, tmp_path: Path, monkeypatch) -> None:
    import pymupdf

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    pdf = tmp_path / "three.pdf"
    doc = pymupdf.open()
    for number in range(3):
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(
            pymupdf.Rect(60, 80, 552, 700),
            f"Page {number + 1} studies topic {'ABC'[number]} in depth. It reports result {number + 1}.",
            fontsize=11,
        )
    doc.save(pdf)

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    project, layout = _prepare(pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _FakeEngine()
    reader._engine_provider = lambda: engine

    # Off: only the page in view goes.
    reader.chat_panel.input.setPlainText("요약해줘")
    reader.chat_panel.send()
    _wait(qapp, lambda: len(reader.chat_log.messages) == 2)
    assert "topic A" in engine.calls[0][0] and "topic C" not in engine.calls[0][0]

    # On: every page, each headed, sentence tags numbered across the paper.
    reader.chat_panel.set_scope("paper")
    reader.chat_panel.input.setPlainText("논문 전체를 요약해줘")
    reader.chat_panel.send()
    _wait(qapp, lambda: len(reader.chat_log.messages) == 4)
    prompt = engine.calls[1][0]
    assert all(f"[p.{n}]" in prompt and f"topic {'ABC'[n - 1]}" in prompt for n in (1, 2, 3))
    assert "논문 전체 본문" in prompt
    window.close()


def test_whole_paper_numbers_sentences_across_pages(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    import pymupdf

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    pdf = tmp_path / "twice.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        doc.insert_pdf(pymupdf.open(two_column_pdf))
    doc.save(pdf)
    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.show()
    project, layout = _prepare(pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader

    _, first = reader._page_text(0)
    text, numbering = reader._paper_text()
    # One numbering for the paper: page 2's sentences follow page 1's.
    assert len(numbering) == 2 * len(first) and len(set(numbering.values())) == len(numbering)
    assert f"[S{len(numbering)}]" in text and text.index("[p.2]") < text.index(f"[S{len(first) + 1}]")
    window.close()


def test_files_and_other_papers_go_with_every_question(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    import pymupdf

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    other_pdf = tmp_path / "other.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "The other paper proposes Zeta pooling.", fontsize=11)
    doc.save(other_pdf)
    other, _ = _prepare(other_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    settings.add_recent_project(other.root)

    window = MainWindow(settings)
    window.show()
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    engine = _FakeEngine()
    reader._engine_provider = lambda: engine

    notes = tmp_path / "notes.md"
    notes.write_text("Lecture says: Zeta pooling is a baseline.", encoding="utf-8")
    reader.chat_panel.filesChosen.emit([notes])
    _wait(qapp, lambda: len(reader.chat_log.files) == 1)
    assert [title for title, _ in reader._recent_papers()] == [other.title]
    reader.chat_panel.paperChosen.emit(str(other.root))
    _wait(qapp, lambda: len(reader.chat_log.files) == 2)
    assert reader.chat_panel._strip.isVisibleTo(reader.chat_panel)

    for number, question in enumerate(("Zeta가 뭐야?", "다시 설명해줘"), start=1):
        reader.chat_panel.input.setPlainText(question)
        reader.chat_panel.send()
        _wait(qapp, lambda n=number: len(reader.chat_log.messages) == 2 * n)
        prompt = engine.calls[-1][0]
        assert "[자료 1] notes.md" in prompt and "Zeta pooling is a baseline" in prompt
        assert "(다른 논문)" in prompt and "The other paper proposes Zeta pooling" in prompt
    assert reader.chat_log.messages[0].files == ["notes.md", other.title]

    # Taken off, it goes no more.
    reader.chat_panel._detach_file(reader.chat_log.files[0])
    reader.chat_panel.input.setPlainText("이제는?")
    reader.chat_panel.send()
    _wait(qapp, lambda: len(reader.chat_log.messages) == 6)
    assert "notes.md" not in engine.calls[-1][0]
    window.close()


class _CitesEachPaper:
    """Answers citing the first sentence tag sent for paper 1, then for paper 2."""

    supports_images = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ask(self, prompt: str, *, images=(), cancel: threading.Event) -> str:  # type: ignore[no-untyped-def]
        import re

        self.calls.append(prompt)
        first = re.search(r"\[S(\d+)\]", prompt[prompt.index("[논문 1]"):]).group(1)
        second = re.search(r"\[S(\d+)\]", prompt[prompt.index("[논문 2]"):]).group(1)
        return f"첫 논문은 이렇게 말합니다 [S{first}]. 둘째 논문은 다릅니다 [S{second}]."


def test_papers_open_in_tabs_and_answers_light_each_tab(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    import pymupdf
    from PySide6.QtCore import QUrl

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.chat_panel import _ref_href
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    second_pdf = tmp_path / "second.pdf"
    doc = pymupdf.open()
    doc.insert_pdf(pymupdf.open(two_column_pdf))
    doc.set_metadata({"title": "Second Synthetic Paper"})
    doc.save(second_pdf)

    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    first, first_layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    second, second_layout = _prepare(second_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((first, first_layout))
    window._opened((second, second_layout))
    qapp.processEvents()
    assert window.tabs.count() == 2 and window.reader.session.project.root == second.root
    a, b = window.readers()

    # Opening a paper already open shows its tab instead of a second copy.
    window._opened((first, first_layout))
    assert window.tabs.count() == 2 and window.reader is a

    # Ask from the first paper about every open tab; the answer cites one
    # sentence of each paper.
    engine = _CitesEachPaper()
    a._engine_provider = lambda: engine
    a.chat_panel.set_scope("tabs")
    a.chat_panel.input.setPlainText("Graph neural networks 차이는?")
    a.chat_panel.send()
    _wait(qapp, lambda: len(a.chat_log.messages) == 2)
    prompt = engine.calls[0]
    # The page in view, then each paper's opening and its matching passages -
    # not whole papers.
    assert "독자가 보고 있는 쪽(p.1)" in prompt and "[논문 2] " in prompt and "질문과 관련된 부분" in prompt
    assert "논문 전체 본문" not in prompt

    answer = a.chat_log.messages[1]
    first_id = a._paper_text()[1][1]
    assert answer.refs[0] == first_id and answer.refs[1].startswith(f"{b.paper_key}|")
    second_id = answer.refs[1].split("|")[-1]
    assert b.session.sentence(second_id) is not None
    # Each paper lights its own sentence; the other tab is marked.
    assert a.left._evidence == [first_id]
    assert b.left._evidence == [second_id] and b.right._evidence == [second_id]
    assert window.tabs.tabText(window.tabs.indexOf(b)).startswith("◆")
    # The citation of the other paper is labelled with its title.
    label = a._paper_label(f"{b.paper_key}|{second_id}")
    assert label and label in a.chat_panel.view.toPlainText()

    # Its note text names the other paper; its link brings that tab forward.
    assert "p.1" in a._cite_pages([f"{b.paper_key}|{second_id}"])
    a.chat_panel._link(QUrl(_ref_href([f"{b.paper_key}|{second_id}"])))
    assert window.reader is b and not window.tabs.tabText(window.tabs.indexOf(b)).startswith("◆")

    # Closing a tab leaves the other; closing the last goes back to the start.
    window.close_paper()
    assert window.readers() == [a] and window.stack.currentWidget() is window.tabs
    window.close_paper()
    assert window.readers() == [] and window.stack.currentWidget() is window.start
    window.close()
