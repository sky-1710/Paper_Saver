"""백지 복습: a keyword sheet made from the PDF, the reader's writing marked
against its fixed answer key, and each miss leading back to the PDF (§4e)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from paper_saver.core.geometry import Rect
from paper_saver.core.models import Block, BlockKind, PageLayout, PaperLayout, Sentence


def _block(page: int, index: int, kind: str, sentences: list[str]) -> Block:
    items = [Sentence(f"p{page}b{index}s{n}", 0, 0, text) for n, text in enumerate(sentences, start=1)]
    return Block(f"p{page}b{index}", page, kind, Rect(0, 0, 100, 20), [], items, " ".join(sentences), 10.0, 12.0)


def _layout() -> PaperLayout:
    return PaperLayout([
        PageLayout(0, 792, 612, [
            _block(0, 0, BlockKind.HEADING, ["Clock algorithm"]),
            _block(0, 1, BlockKind.BODY, ["The hand skips pages whose reference bit is 1.", "It clears the bit as it passes."]),
        ]),
        PageLayout(1, 792, 612, [
            _block(1, 0, BlockKind.HEADING, ["Working set"]),
            _block(1, 1, BlockKind.BODY, ["The working set is the pages used in the last delta references."]),
        ]),
    ])


def _pages(layout: PaperLayout) -> dict[str, int]:
    return {s.id: page.number for page, _, s in layout.sentences()}


def _sheet_answer() -> dict:
    return {
        "topics": [
            {
                "heading": "Clock 알고리즘",
                "keywords": ["reference bit", "clock hand"],
                "points": [
                    {"text": "reference bit가 1이면 건너뛴다.", "sources": [2], "important": True},
                    {"text": "지나가며 bit를 0으로 바꾼다.", "sources": [3], "important": False},
                ],
            },
            {
                "heading": "Working set",
                "keywords": ["delta"],
                "points": [{"text": "최근 delta번 참조한 페이지의 집합.", "sources": [5], "important": False}],
            },
        ]
    }


def _request(layout: PaperLayout, **extra):  # type: ignore[no-untyped-def]
    from paper_saver.core.review import paper_text
    from paper_saver.translate.review import ReviewRequest

    text, numbering = paper_text(layout)
    return ReviewRequest("blank", "OS08", text, numbering, _pages(layout), **extra)


class FakeEngine:
    key = "fake"
    chat_model = "fake-model"

    def __init__(self, answers: list[str], delay: float = 0.0) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.images: list[list] = []
        self.delay = delay

    def generate_json(self, prompt, schema, *, images=(), cancel):  # noqa: ANN001
        self.prompts.append(prompt)
        self.schemas.append(schema)
        # What a picture held when it was sent: (its size, a PNG), or None.
        self.images.append([
            (Path(p).stat().st_size, Path(p).read_bytes()[:4] == b"\x89PNG") if Path(p).is_file() else None
            for p in images
        ])
        time.sleep(self.delay)
        return self.answers.pop(0)


def test_the_sheet_is_made_from_the_pdf_with_short_keywords_and_a_cited_key() -> None:
    from paper_saver.core.review import ReviewFormatError
    from paper_saver.translate.blank import SHEET_SCHEMA, CodexBlankGenerator

    layout = _layout()
    request = _request(layout, lecture=True, transcripts=("OS08-1.md",))
    bad = _sheet_answer()
    bad["topics"][0]["keywords"] = ["reference bit가 1이면 그 페이지를 건너뛰고 bit를 0으로 바꾼 뒤 다음으로 간다"]
    engine = FakeEngine([json.dumps(bad), json.dumps(_sheet_answer())])
    sheet = CodexBlankGenerator(engine).sheet(request, cancel=threading.Event())

    assert [t.heading for t in sheet.topics] == ["Clock 알고리즘", "Working set"]
    assert [p.number for p in sheet.points()] == [1, 2, 3]
    assert sheet.points()[0].important and sheet.points()[0].refs == ["p0b1s1"]
    assert engine.schemas == [SHEET_SCHEMA] * 2
    assert "키워드는 설명 없이 용어만" in engine.prompts[1]
    prompt = engine.prompts[0]
    # The PDF is the standard; the STT only says what was stressed.
    assert "[S2] The hand skips" in prompt and prompt.index("[S2]") < prompt.index("[쓸 문서: 백지 복습")
    assert "STT에만 있고 PDF에\n  없는 내용은 points에 넣지 마세요" in prompt
    assert "한 장 요약" not in prompt.split("[쓸 문서: 백지 복습")[1]

    uncited = _sheet_answer()
    for topic in uncited["topics"]:
        for point in topic["points"]:
            point["sources"] = []
    from paper_saver.core.blank import sheet_from_answer

    with pytest.raises(ReviewFormatError, match="근거 문장 번호"):
        sheet_from_answer(uncited, numbering=request.numbering, page_of=request.pages.get)


def test_marking_covers_every_point_and_reads_back_to_the_pdf(tmp_path: Path) -> None:
    from paper_saver.core.blank import (
        BlankStore,
        result_markdown,
        sheet_from_answer,
        wrote_anything,
    )
    from paper_saver.translate.blank import GRADE_SCHEMA, CodexBlankGenerator

    layout = _layout()
    request = _request(layout, lecture=True)
    store = BlankStore(tmp_path)
    store.put_sheet(
        sheet_from_answer(_sheet_answer(), numbering=request.numbering, page_of=request.pages.get),
        anchors={s.id: s.text for _, _, s in layout.sentences()},
    )
    attempt = store.start([1, 2])
    assert attempt.text.startswith("## 1. Clock 알고리즘\n> 키워드: reference bit · clock hand")
    assert not wrote_anything(attempt.text)
    store.set_text(attempt.id, attempt.text.replace("clock hand\n", "clock hand\nbit가 1이면 넘어가고 0으로 만든다.\n"))
    assert wrote_anything(store.get(attempt.id).text)

    partial = {"marks": [{"point": 1, "status": "recalled", "missing": ""}], "wrong": [], "overall": ""}
    full = {
        "marks": [
            {"point": 1, "status": "recalled", "missing": ""},
            {"point": 2, "status": "partial", "missing": "언제 0으로 바꾸는지"},
            {"point": 3, "status": "missed", "missing": ""},
        ],
        "wrong": [{"topic": 1, "wrote": "bit가 0이면 넘어간다", "correct": "bit가 1이면 넘어간다.", "sources": [2]}],
        "overall": "Clock은 기억했고 working set은 다시 보세요.",
    }
    engine = FakeEngine([json.dumps(partial), json.dumps(full)])
    marks, missing, wrong, overall = CodexBlankGenerator(engine).grade(
        request, store.sheet, attempt.topics, store.get(attempt.id).text, cancel=threading.Event()
    )
    assert engine.schemas == [GRADE_SCHEMA] * 2
    assert "채점하지 않은 기준 항목이 있습니다: 2, 3" in engine.prompts[1]
    # The key is sent, the keyword lines the sheet printed are not part of the answer.
    answer = engine.prompts[0].rsplit("[학생 답안]", 1)[1]
    assert "bit가 1이면 넘어가고" in answer and "> 키워드:" not in answer
    assert "채점할 번호: 1, 2, 3" in engine.prompts[0]

    store.grade(attempt.id, marks, missing, wrong, overall, anchors={})
    marked = store.get(attempt.id)
    assert marked.score() == (1.5, 3) and marked.score([1], store.sheet) == (1.5, 2)
    text = result_markdown(store.sheet, marked, request.pages.get)
    assert "**1.5/3 (50%)**" in text and "## 1. Clock 알고리즘 — 1.5/2" in text
    assert "- ✗ “bit가 0이면 넘어간다” → bit가 1이면 넘어간다. [p.1](ref:p0b1s1)" in text
    assert "- △ 지나가며 bit를 0으로 바꾼다. — 빠진 부분: 언제 0으로 바꾸는지 [p.1](ref:p0b1s2)" in text
    assert "**빠뜨린 것**\n- 최근 delta번 참조한 페이지의 집합. [p.2](ref:p1b1s1)" in text
    assert "- ✓ reference bit가 1이면 건너뛴다." in text

    # A second attempt on what was missed shows the last score beside it.
    again = store.start([2])
    store.grade(again.id, {3: "recalled"}, {}, [], "", anchors={})
    assert "## 2. Working set — 1/1 (지난번 0/1)" in result_markdown(
        store.sheet, store.get(again.id), request.pages.get, before=store.marked()[1:]
    )
    assert store.last_score(2) == (1.0, 1)

    # Saved whole; a re-extracted layout moves the key's links.
    reloaded = BlankStore(tmp_path)
    assert [a.id for a in reloaded.marked()] == [again.id, attempt.id]
    assert reloaded.get(attempt.id).wrong[0].refs == ["p0b1s1"]
    moved = PaperLayout([
        PageLayout(0, 792, 612, [
            _block(0, 0, BlockKind.HEADING, ["Clock algorithm"]),
            _block(0, 5, BlockKind.BODY, ["The hand skips pages whose reference bit is 1.", "It clears the bit as it passes."]),
        ]),
        layout.pages[1],
    ])
    assert reloaded.bind(moved)
    assert reloaded.sheet.points()[0].refs == ["p0b5s1"] and reloaded.get(attempt.id).wrong[0].refs == ["p0b5s1"]


def test_starting_again_replaces_an_empty_sheet_but_keeps_writing(tmp_path: Path) -> None:
    from paper_saver.core.blank import BlankStore, sheet_from_answer

    layout = _layout()
    request = _request(layout)
    store = BlankStore(tmp_path)
    store.put_sheet(sheet_from_answer(_sheet_answer(), numbering=request.numbering, page_of=request.pages.get), anchors={})
    first = store.start([1])
    second = store.start([1, 2])
    assert store.get(first.id) is None and store.open_attempt() is second
    store.set_text(second.id, second.text + "기억나는 것\n")
    third = store.start([2])
    assert store.get(second.id) is not None and store.open_attempt() is third


def _wait(qapp, condition, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def test_blank_mode_covers_the_pdf_while_writing_and_leads_back_to_it(
    qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from PySide6.QtCore import QUrl

    from paper_saver.config import Settings
    from paper_saver.core.review import paper_text
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
    _, numbering = paper_text(layout)
    sheet = {
        "topics": [
            {
                "heading": "그래프 신경망",
                "keywords": ["adjacency matrix", "learnable"],
                "points": [
                    {"text": "인접 행렬은 학습 중에 갱신된다.", "sources": [3], "important": True},
                    {"text": "Zhong 등이 2020년에 정규화 모델을 제안했다.", "sources": [2], "important": False},
                ],
            },
            {
                "heading": "실험 결과",
                "keywords": ["SEED"],
                "points": [{"text": "SEED에서 정확도 79.37%.", "sources": [4], "important": True}],
            },
        ]
    }
    marking = {
        "marks": [
            {"point": 1, "status": "recalled", "missing": ""},
            {"point": 2, "status": "missed", "missing": ""},
            {"point": 3, "status": "partial", "missing": "정확한 수치"},
        ],
        "wrong": [],
        "overall": "결과 수치를 다시 보세요.",
    }
    engine = FakeEngine([json.dumps(sheet), json.dumps(marking)], delay=0.1)
    reader._engine_provider = lambda: engine

    reader.set_pane_mode("right", "blank")
    panel = reader.blank_panel
    assert reader.stacks["right"].currentWidget() is panel and panel.stack.currentWidget() is panel._empty
    assert "이 PDF 자체" in panel.lead.text()
    panel.make_button.click()
    assert panel.busy == "sheet" and "만드는 중" in panel.lead.text()
    _wait(qapp, lambda: reader.blank_store.sheet is not None and not panel.busy)
    # Made from the PDF, never from a review of it.
    assert "[S3]" in engine.prompts[0] and "백지 복습 문제지" in engine.prompts[0]
    assert panel.stack.currentWidget() is panel._home and panel.topics.count() == 2
    assert panel.primary.text() == "백지 시작 (2개 주제)"

    # Writing: only the keywords show, and the PDF beside it is covered.
    panel.primary.click()
    assert panel.writing and panel.stack.currentWidget() is panel.editor
    assert "> 키워드: adjacency matrix · learnable" in panel.editor.toPlainText()
    assert "인접 행렬은 학습 중에" not in panel.editor.toPlainText()
    assert reader.covered("left") and reader.stacks["left"].currentWidget() is reader._covers["left"]
    panel.primary.click()  # nothing written yet
    assert "아직 아무것도 쓰지 않았습니다" in panel.error.text() and len(engine.prompts) == 1
    panel.editor.setPlainText(panel.editor.toPlainText().replace("learnable\n", "learnable\n인접 행렬은 학습된다.\n", 1))
    panel.editor.edited.emit()
    panel.flush()
    attempt = reader.blank_store.open_attempt()
    assert "인접 행렬은 학습된다." in attempt.text

    # Left and opened again, the writing is still there; the cover too.
    panel.show_list()
    assert not reader.covered("left") and reader.stacks["left"].currentWidget() is reader.left_scroll
    assert "쓰는 중" in panel.history.item(0).text()
    panel._open_item(panel.history.item(0))
    assert panel.writing and reader.covered("left")

    panel.primary.click()
    assert panel.busy == "grade" and panel.editor.isReadOnly()
    _wait(qapp, lambda: not panel.busy)
    assert "인접 행렬은 학습된다." in engine.prompts[1].rsplit("[학생 답안]", 1)[1]
    marked = reader.blank_store.get(attempt.id)
    assert marked.checked and marked.score() == (1.5, 3)
    assert panel.stack.currentWidget() is panel.view and "1.5/3" in panel.title.text()
    assert not reader.covered("left") and reader.stacks["left"].currentWidget() is reader.left_scroll
    shown = panel.view.toPlainText()
    assert "빠뜨린 것" in shown and "Zhong 등이 2020년에" in shown and "★ SEED에서" in shown

    # A miss leads to its sentence in the PDF.
    missed = next(n for n, refs in enumerate(panel._segments) if refs == [numbering[2]])
    panel._clicked(QUrl(f"seg:{missed}"))
    assert reader.left._evidence == [numbering[2]]

    reader.blank_to_note(attempt.id)
    note = reader.note_panel.editor.toPlainText()
    assert "백지 채점" in note and "(p.1)" in note and "ref:" not in note

    # Again on what was not full: both topics here, each with its last score.
    panel.again_button.click()
    again = reader.blank_store.open_attempt()
    assert again.id != attempt.id and again.topics == [1, 2] and panel.writing
    panel.show_list()
    assert panel.topics.item(0).text().endswith("1/2") and panel.topics.item(1).text().endswith("0.5/1")

    # The panel goes to whichever pane chose it.
    reader.set_pane_mode("left", "blank")
    assert reader.stacks["left"].currentWidget() is panel and reader.pane_mode("right") == "original"
    assert reader.stacks["right"].indexOf(panel) < 0
    window.close()


@pytest.fixture()
def slides_with_a_table_picture(tmp_path: Path) -> Path:
    """Five landscape slides, each with the template's logo in its corner;
    slide 3 holds a table that is only a picture - no text to extract - and
    slide 4 two graphs side by side."""
    import pymupdf

    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), 0)
    logo.set_rect(logo.irect, (200, 30, 30))
    table = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 240), 0)
    table.set_rect(table.irect, (255, 255, 255))
    for row in range(0, 240, 60):
        table.set_rect(pymupdf.IRect(0, row, 400, row + 2), (0, 0, 0))
    doc = pymupdf.open()
    for number in range(5):
        page = doc.new_page(width=792, height=612)
        page.insert_text((60, 80), f"Page replacement part {number + 1}", fontsize=28)
        page.insert_text((60, 140), "The clock hand skips a page whose reference bit is set.", fontsize=16)
        page.insert_image(pymupdf.Rect(730, 20, 770, 60), pixmap=logo)
        if number == 2:
            page.insert_image(pymupdf.Rect(150, 200, 650, 500), pixmap=table)
        if number == 3:
            page.insert_image(pymupdf.Rect(60, 200, 360, 450), pixmap=table)
            page.insert_image(pymupdf.Rect(420, 200, 720, 450), pixmap=table)
    target = tmp_path / "OS08-PageReplacement.pdf"
    doc.save(str(target))
    doc.close()
    return target


def test_figures_and_tables_go_as_pictures_but_not_the_template(
    qapp, tmp_path: Path, slides_with_a_table_picture: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.core.blank import figure_regions
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    project, layout = _prepare(slides_with_a_table_picture, None, settings.projects_dir)(
        threading.Event(), lambda _m: None
    )
    # The table on slide 3, both graphs of slide 4 in one picture; the logo
    # on every slide is the template's.
    regions = figure_regions(layout)
    assert [page for page, _ in regions] == [2, 3]
    assert regions[1][1].x0 < 70 and regions[1][1].x1 > 710 and regions[1][1].x1 < 730
    # Over the limit, the pages with the most figure on them stay.
    assert [page for page, _ in figure_regions(layout, limit=1)] == [2] and figure_regions(layout, limit=0) == []

    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    window._opened((project, layout))
    qapp.processEvents()
    reader = window.reader
    answer = {
        "topics": [{
            "heading": "Clock",
            "keywords": ["reference bit", "표: 알고리즘별 page fault 수"],
            "points": [{"text": "표: FIFO 9번, LRU 8번.", "sources": [1], "important": True}],
        }]
    }
    engine = FakeEngine([json.dumps(answer), json.dumps(answer)])
    reader._engine_provider = lambda: engine
    panel = reader.blank_panel
    reader.set_pane_mode("right", "blank")
    assert panel.figures_box.isChecked() and "표와 그림" in panel.lead.text()
    reader.generate_blank_sheet()
    _wait(qapp, lambda: reader.blank_store.sheet is not None and not panel.busy)
    # Cut out of the PDF for this run, a PNG of the table, and gone after it.
    assert len(engine.images[0]) == 2 and all(png and size > 1000 for size, png in engine.images[0])
    assert "[첨부 이미지] PDF의 그림·표 2개" in engine.prompts[0]
    assert "이미지 1 = p.3, 이미지 2 = p.4" in engine.prompts[0]
    assert "표: 알고리즘별 page fault 수" in reader.blank_store.sheet.topics[0].keywords
    assert reader.blank_store.sheet.figures == 2 and "그림·표 2개 반영" in panel.info.text()

    # Turned off, nothing but text goes.
    panel.figures_box.setChecked(False)
    assert not reader.blank_store.send_figures
    reader.generate_blank_sheet()
    _wait(qapp, lambda: len(engine.prompts) == 2 and not panel.busy)
    assert engine.images[1] == [] and "[첨부 이미지]" not in engine.prompts[1]
    window.close()
