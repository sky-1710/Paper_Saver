"""One-page reviews and Cornell notes: what GPT is shown, how its answer becomes
an editable document, and how a part of it leads back to the paper (§4d)."""

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


def _figure(page: int, index: int, text: str) -> Block:
    """Text inside a figure, table or code listing: it has no sentences."""
    return Block(f"p{page}b{index}", page, BlockKind.GRAPHIC, Rect(0, 0, 100, 20), [], [], text, 10.0, 12.0)


def _layout() -> PaperLayout:
    first = PageLayout(0, 612, 792, [
        _block(0, 0, BlockKind.TITLE, ["Attention Without Recurrence"]),
        _block(0, 1, BlockKind.VERBATIM, ["Ada Lovelace, University of Somewhere"]),
        _block(0, 2, BlockKind.BODY, ["We drop recurrence entirely.", "Attention alone suffices."]),
        _block(0, 3, BlockKind.FURNITURE, ["Page 1"]),
        _figure(0, 4, "int main(void) {"),
        _figure(0, 5, "return 0; }"),
    ])
    second = PageLayout(1, 612, 792, [
        _block(1, 0, BlockKind.BODY, ["Our model reaches 28.4 BLEU.", "Training takes 12 hours."]),
        # Real papers' "References" line is as often filed verbatim as a heading.
        _block(1, 1, BlockKind.VERBATIM, ["References"]),
        _block(1, 2, BlockKind.VERBATIM, ["[1] A. Author. A paper. 2017."]),
        _block(1, 3, BlockKind.VERBATIM, ["B. Writer and C. Other. Another paper. In Proceedings, 2019."]),
        # ... and the appendix after it is filed verbatim too, headings and all.
        _block(1, 4, BlockKind.VERBATIM, ["A Probability Change at the Critical Chunk"]),
        _block(1, 5, BlockKind.VERBATIM, ["This section analyzes the critical chunk."]),
        _block(1, 6, BlockKind.FURNITURE, ["Dongkun Shin, SKKU"]),
    ])
    third = PageLayout(2, 612, 792, [
        _block(2, 0, BlockKind.HEADING, ["A Appendix"]),
        _block(2, 1, BlockKind.BODY, ["The grading prompt was as follows."]),
    ])
    return PaperLayout([first, second, third])


def _pages(layout: PaperLayout) -> dict[str, int]:
    return {s.id: page.number for page, _, s in layout.sentences()}


# -- what GPT is shown ----------------------------------------------------------------


def test_paper_text_tags_what_can_be_cited_and_leaves_out_the_rest() -> None:
    from paper_saver.core.review import paper_text

    layout = _layout()
    text, numbering = paper_text(layout)
    assert text.startswith("[p.1]\n[S1] Attention Without Recurrence")
    assert "[S2] We drop recurrence entirely. [S3] Attention alone suffices." in text
    # A slide's code, tables and diagrams are its content: one untagged paragraph.
    assert "(그림·표 속 글)\nint main(void) {\nreturn 0; }" in text
    assert "Lovelace" in text  # verbatim lines go too: Korean on a slide is filed so
    assert "Page 1" not in text and "A. Author" not in text  # page numbers and the reference list do not
    assert "B. Writer" not in text
    assert "A Probability Change at the Critical Chunk\n\nThis section analyzes" in text  # the appendix does
    assert "Dongkun Shin, SKKU" in text  # a running foot that is more than a number does
    assert numbering[4] == "p1b0s1" and "[p.2]" in text and "[p.3]" in text

    with_strays, _ = paper_text(layout, strays={2: ["– (2^32 / 2^14) * 4B = 1MB"]})
    assert "(따로 읽은 글)\n– (2^32 / 2^14) * 4B = 1MB" in with_strays.split("[p.3]")[1]

    # What the professor said over a page goes under that page, beyond the limit.
    said = "[영상 2 · Slide 2]\n" + "this BLEU score is on the exam " * 10
    heard, _ = paper_text(layout, spoken={1: said}, limit=len(text))
    page_two = heard.split("[p.2]")[1].split("[p.3]")[0]
    assert f"(강의 STT — 이 쪽을 설명한 부분)\n{said.strip()}" in page_two
    assert "이하 생략" not in heard

    shorter, cut = paper_text(layout, until_references=True)
    assert "grading prompt" not in shorter and "28.4 BLEU" in shorter
    assert max(cut) < max(numbering)


def test_stray_lines_are_what_the_layout_left_out(tmp_path: Path) -> None:
    import pymupdf

    from paper_saver.pdf.text import stray_lines

    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 100), "What are Data?", fontsize=14)
    page.insert_text((72, 140), "0.2 0.4 0.6", fontsize=4)  # a plot's ticks
    path = tmp_path / "slide.pdf"
    document.save(str(path))
    held = PaperLayout([PageLayout(0, 612, 792, [_figure(0, 0, "What are Data?")])])
    assert stray_lines(path, PaperLayout([PageLayout(0, 612, 792, [])])) == {0: ["What are Data?"]}
    assert stray_lines(path, held) == {}


# -- GPT's answer, as Markdown ----------------------------------------------------------


def _one_page() -> dict:
    point = {"text": "순환 구조 없이 어텐션만 쓴다.", "sources": [2, 3]}
    return {
        "one_liner": {"text": "어텐션만으로 번역한다.", "sources": [3]},
        "sections": [
            {"heading": "문제와 동기", "points": [point]},
            {"heading": "실험과 결과", "points": [{"text": "28.4 BLEU [S4]를 얻는다.", "sources": []}]},
            {"heading": "한계와 의문", "points": [{"text": "(평가) 긴 문서는 다루지 않는다.", "sources": [99]}]},
        ],
    }


def test_one_page_answer_becomes_markdown_with_links_per_page() -> None:
    from paper_saver.core.review import paper_text, review_markdown

    layout = _layout()
    _, numbering = paper_text(layout)
    text = review_markdown("one_page", _one_page(), numbering=numbering, page_of=_pages(layout).get, title="Attn")
    assert text.startswith("# 한 장 요약 · Attn\n")
    assert "> **한 줄 요약** 어텐션만으로 번역한다. [p.1](ref:p0b2s2)" in text
    assert "- 순환 구조 없이 어텐션만 쓴다. [p.1](ref:p0b2s1,p0b2s2)" in text
    # A tag written into the text still counts, and leaves the text.
    assert "- 28.4 BLEU를 얻는다. [p.2](ref:p1b0s1)" in text
    # A number the paper did not have is dropped.
    assert "- (평가) 긴 문서는 다루지 않는다.\n" in text


def test_an_answer_that_cites_too_little_or_is_short_is_refused() -> None:
    from paper_saver.core.review import ReviewFormatError, paper_text, review_markdown

    layout = _layout()
    _, numbering = paper_text(layout)
    bare = _one_page()
    for section in bare["sections"]:
        for point in section["points"]:
            point["sources"] = []
            point["text"] = "근거 없음"
    with pytest.raises(ReviewFormatError, match="sources"):
        review_markdown("one_page", bare, numbering=numbering, page_of=_pages(layout).get)
    short = _one_page()
    short["sections"] = short["sections"][:2]
    with pytest.raises(ReviewFormatError, match="3개"):
        review_markdown("one_page", short, numbering=numbering, page_of=_pages(layout).get)


def _cornell() -> dict:
    return {
        "rows": [
            {"cue": "왜 순환을 버리나?", "notes": [{"text": "어텐션만으로 충분하다.", "sources": [3]}]},
            {"cue": "성능은?", "notes": [{"text": "28.4 BLEU.", "sources": [4]}, {"text": "12시간.", "sources": [5]}]},
            {"cue": "## 무엇을 버리나", "notes": [{"text": "순환.", "sources": [2]}]},
        ],
        "summary": [{"text": "어텐션만 쓴다.", "sources": [3]}, {"text": "빠르다.", "sources": [5]}],
    }


def test_cornell_answer_has_cues_notes_and_a_summary() -> None:
    from paper_saver.core.review import paper_text, review_markdown

    layout = _layout()
    _, numbering = paper_text(layout)
    text = review_markdown("cornell", _cornell(), numbering=numbering, page_of=_pages(layout).get)
    assert "### 왜 순환을 버리나?\n- 어텐션만으로 충분하다. [p.1](ref:p0b2s2)" in text
    assert "### 무엇을 버리나\n" in text  # a cue cannot break out of its heading
    assert "## 요약\n어텐션만 쓴다. [p.1](ref:p0b2s2) 빠르다. [p.2](ref:p1b0s2)" in text


def test_citations_split_a_line_and_read_as_page_numbers() -> None:
    from paper_saver.core.review import plain_citations, refs_in, split_citations

    line = "A 문장. [p.1](ref:x) [p.2](ref:y) B 문장. [p.3](ref:z) 끝"
    segments = split_citations(line)
    assert [(s.text.strip(), s.refs, s.labels) for s in segments] == [
        ("A 문장.", ["x", "y"], ["p.1", "p.2"]), ("B 문장.", ["z"], ["p.3"]), ("끝", [], []),
    ]
    assert refs_in(line + " [p.1](ref:x)") == ["x", "y", "z"]
    assert plain_citations(line) == "A 문장. (p.1, p.2) B 문장. (p.3) 끝"


# -- the store ------------------------------------------------------------------------------


def test_store_keeps_edits_and_moves_links_to_their_sentences(tmp_path: Path) -> None:
    from paper_saver.core.review import ReviewStore

    store = ReviewStore(tmp_path)
    store.put("one_page", "- 점 [p.1](ref:p0b2s1)\n", anchors={"p0b2s1": "We drop recurrence entirely."}, model="m")
    assert store.set_text("one_page", "- 고친 점 [p.1](ref:p0b2s1)\n")
    assert not store.set_text("one_page", "- 고친 점 [p.1](ref:p0b2s1)\n")
    again = ReviewStore(tmp_path)
    assert again.get("one_page").text == "- 고친 점 [p.1](ref:p0b2s1)\n"
    assert again.get("one_page").edited and again.get("one_page").model == "m"

    # Re-extraction found a paragraph before it: the sentence is now p0b3s1.
    layout = _layout()
    texts = [[s.text for s in b.sentences] for b in layout.pages[0].blocks]
    texts.insert(2, ["A new first paragraph."])
    kinds = [b.kind for b in layout.pages[0].blocks]
    kinds.insert(2, BlockKind.BODY)
    layout.pages[0].blocks = [_block(0, i, kind, t) for i, (kind, t) in enumerate(zip(kinds, texts))]
    assert again.bind(layout)
    assert again.get("one_page").text == "- 고친 점 [p.1](ref:p0b3s1)\n"
    assert not ReviewStore(tmp_path).bind(layout)


# -- the generator ------------------------------------------------------------------------


class FakeEngine:
    key = "fake"
    chat_model = "fake-model"

    def __init__(self, answers: list[str], delay: float = 0.0) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []
        self.delay = delay

    def generate_json(self, prompt, schema, *, images=(), cancel):  # noqa: ANN001
        self.prompts.append(prompt)
        self.schemas.append(schema)
        time.sleep(self.delay)
        return self.answers.pop(0)


def test_generator_sends_the_paper_first_and_retries_with_the_reason() -> None:
    from paper_saver.core.review import paper_text
    from paper_saver.translate.review import SCHEMAS, CodexReviewGenerator, ReviewRequest

    layout = _layout()
    text, numbering = paper_text(layout)
    short = _cornell()
    short["rows"] = short["rows"][:1]
    engine = FakeEngine([json.dumps(short), "```json\n" + json.dumps(_cornell()) + "\n```"])
    request = ReviewRequest("cornell", "Attn", text, numbering, _pages(layout))
    markdown = CodexReviewGenerator(engine).generate(request, cancel=threading.Event())
    assert markdown.startswith("# 코넬 노트 · Attn")
    assert engine.schemas == [SCHEMAS["cornell"]] * 2
    assert "rows(단서와 노트)가 3개 이상" in engine.prompts[1]
    # The paper comes before what to write, so both documents share a prefix.
    assert engine.prompts[0].index("[S2] We drop") < engine.prompts[0].index("[쓸 문서: 코넬 노트")
    assert "강의 STT 규칙" not in engine.prompts[0]


def test_a_lecture_is_weighted_by_time_and_repetition() -> None:
    from paper_saver.core.review import paper_text
    from paper_saver.translate.review import (
        ReviewRequest,
        page_notes,
        review_prompt,
        review_request,
    )

    layout = _layout()
    notes = page_notes({0: 557, 1: 45}, {0: 6, 1: 1, 2: 3})
    assert notes == {0: "강의 9분 17초 · 6번 띄움", 1: "강의 45초", 2: "3번 띄움"}
    text, _ = paper_text(layout, notes=notes)
    assert text.startswith("[p.1] (강의 9분 17초 · 6번 띄움)\n") and "[p.2] (강의 45초)\n" in text

    paper = review_prompt(ReviewRequest("one_page", "Attn", text))
    assert "문제와 동기" in paper and "시험 대비" not in paper and "강의 자료 규칙" not in paper

    lecture = ReviewRequest(
        "one_page", "OS08", text, lecture=True, seconds={0: 557, 1: 45, 2: 5}, visits={0: 6, 2: 3, 1: 1},
        terms=(("reference bit", 40, 5), ("LRU", 44, 9)), styles="1. Traps are rare. (X)",
    )
    prompt = review_prompt(lecture)
    assert "강의 자료 규칙" in prompt and "헷갈리기 쉬운 점" in prompt and "문제와 동기" not in prompt
    assert "시간과 반복한 횟수" in prompt
    assert "- 오래 설명한 쪽: p.1 9분 17초, p.2 45초, p.3 5초" in prompt
    assert "- 여러 번 다시 띄운 쪽: p.1 6번, p.3 3번" in prompt  # once is no repetition
    assert "- 강의에서 여러 번 말한 용어: reference bit 40회(5개 쪽), LRU 44회(9개 쪽)" in prompt
    assert "Traps are rare" in prompt and prompt.index("[교수님 이전 퀴즈") > prompt.index("[본문]")
    cornell = review_prompt(ReviewRequest("cornell", "OS08", text, lecture=True))
    assert "10~16개" in cornell and "중요하게 다룬 곳" not in cornell and "교수님 이전 퀴즈" not in cornell

    # The mode starts from what the PDF looks like; the reader's choice wins.
    plain = review_request("one_page", "Attn", layout, styles="1. Traps are rare. (X)")
    assert not plain.lecture and plain.styles == ""
    slides = PaperLayout([PageLayout(n, 720, 540, page.blocks) for n, page in enumerate(layout.pages)])
    assert review_request("one_page", "OS08", slides, styles="1. Traps (X)").lecture
    assert not review_request("one_page", "OS08", slides, lecture=False).lecture
    assert review_request("one_page", "Attn", layout, lecture=True).lecture


def test_the_prompt_says_how_to_use_the_lecture_stt() -> None:
    from paper_saver.translate.review import ReviewRequest, review_prompt

    prompt = review_prompt(ReviewRequest("one_page", "OS06", "[p.1]\n[S1] Memory", transcripts=("a.md", "b.md")))
    assert "영상 2개를 강의 순서대로 이었습니다: 영상 1 = a.md, 영상 2 = b.md" in prompt
    assert '"(강의)"로 시작' in prompt
    assert prompt.index("강의 STT 규칙") < prompt.index("[S1] Memory")  # before the text: one shared prefix


# -- drawn for reading ---------------------------------------------------------------------


def test_review_html_links_each_cited_part() -> None:
    from paper_saver.render.review import ACTIVE_BACKGROUND, review_html

    markdown = (
        "# 코넬 노트\n\n### 왜?\n- 이유 하나. [p.1](ref:a)\n- 이유 둘. [p.2](ref:b,c)\n\n"
        "### 결과는?\n- 수치. [p.3](ref:d)\n\n## 요약\n첫 문장. [p.1](ref:a) 둘째 문장. [p.3](ref:d)\n"
    )
    html, segments = review_html(markdown, "cornell")
    assert segments[0] == ["a", "b", "c"]  # the cue: everything under it
    assert ["b", "c"] in segments and ["d"] in segments
    assert html.count('<td width="34%"') == 3  # the grid: its header and two rows
    assert html.count("<tr>") == 4  # and the summary's box
    assert "단서" in html and "요약" in html
    lit, _ = review_html(markdown, "cornell", active=1)
    assert f'<a href="seg:1" style="background-color:{ACTIVE_BACKGROUND}">' in lit

    page, linked = review_html("# 제목\n\n## 방법\n- 점. [p.1](ref:a)\n- 링크 없음\n", "one_page")
    assert linked == [["a"], ["a"]]  # the heading, then the point
    assert "링크 없음" in page


# -- in the reader ------------------------------------------------------------------------


def _wait(qapp, condition, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not condition() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def test_review_mode_makes_shows_edits_and_leads_to_the_paper(
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
    panes = reader.left_scroll.mapTo(reader, reader.left_scroll.rect().topLeft())
    assert panes.y() == reader.right_scroll.mapTo(reader, reader.right_scroll.rect().topLeft()).y()

    answer = {
        "rows": [{"cue": f"단서 {n}", "notes": [{"text": f"노트 {n}", "sources": [n + 1]}]} for n in range(3)],
        "summary": [{"text": "요약 문장.", "sources": [1]}],
    }
    engine = FakeEngine([json.dumps(answer)], delay=0.2)
    reader._engine_provider = lambda: engine
    reader.set_pane_mode("right", "cornell")
    assert reader.pane_mode("right") == "cornell"
    panel = reader.review_panels["right"]
    assert panel.stack.currentWidget() is panel._empty

    # The lecture was recorded in two parts; both go, in order, shared with the quiz.
    parts = []
    for number, said in enumerate(("graph neural networks matter for the exam", "the SEED accuracy is key"), 1):
        part = tmp_path / f"lecture part {number}.md"
        part.write_text(f"## Slide 1\n**[00:00:0{number}]** {said}", encoding="utf-8")
        parts.append(str(part))
    reader._register_transcripts(parts)
    _wait(qapp, lambda: len(reader.quiz_store.transcripts) == 2)
    assert panel.stt_button.text() == "🎙 STT 2개" and "STT 2개" in panel.lead.text()
    assert reader.quiz_panel.stt_list.count() == 2

    reader.generate_review("cornell")
    assert "만드는 중" in panel.lead.text()
    _wait(qapp, lambda: reader.review_store.get("cornell") is not None and not panel._generating)
    assert panel.stack.currentWidget() is panel.view
    assert "[S2]" in engine.prompts[0]
    assert "영상 1 = lecture part 1.md, 영상 2 = lecture part 2.md" in engine.prompts[0]
    spoken = engine.prompts[0].split("(강의 STT — 이 쪽을 설명한 부분)\n", 1)[1]
    assert spoken.index("[영상 1 · Slide 1") < spoken.index("graph neural networks matter")
    assert spoken.index("[영상 2 · Slide 1") < spoken.index("the SEED accuracy is key")
    assert reader.review_store.get("cornell").transcripts == ["lecture part 1.md", "lecture part 2.md"]
    assert "STT 2개 반영" in panel.info.text()
    # With its STT the paper is a lecture until the reader says otherwise.
    assert panel.mode == "lecture" and panel.mode_buttons["lecture"].isChecked()
    assert "강의 자료 규칙" in engine.prompts[0] and "[강의에서 중요하게 다룬 곳" in engine.prompts[0]
    assert reader.review_store.get("cornell").mode == "lecture" and "강의 모드" in panel.info.text()
    panel.mode_buttons["paper"].click()
    assert reader.review_store.mode == "paper" and reader.review_panels["left"].mode == "paper"
    assert reader.review_panels["left"].mode_buttons["paper"].isChecked()
    assert "강의 모드로 만듦 → 다시 만들기로 반영" in panel.info.text()
    panel.choose_mode("lecture")
    reader._move_transcript(reader.quiz_store.transcripts[1].file, -1)
    assert "STT가 바뀜" in panel.info.text()
    assert reader.quiz_panel.stt_list.item(0).text() == "1. lecture part 2.md"

    # Clicking a note lights its sentence on the page.
    ids = [s.id for _, _, s in layout.sentences()]
    _, numbering = paper_text(layout)
    note = next(n for n, refs in enumerate(panel._segments) if refs == [numbering[2]])
    panel._clicked(QUrl(f"seg:{note}"))
    assert reader.left._evidence == [numbering[2]] and numbering[2] in ids
    assert panel._active == note

    # Edited, it is saved; Ctrl+click on a citation leads to the paper too.
    panel.set_editing(True)
    assert panel.editing
    panel.editor.setPlainText(panel.editor.toPlainText().replace("노트 0", "고친 노트"))
    panel.editor.edited.emit()
    assert panel.insert_citation(ids[0], 0)
    panel.flush()
    saved = reader.review_store.get("cornell")
    assert "고친 노트" in saved.text and f"[p.1](ref:{ids[0]})" in saved.text and saved.edited
    seen: list[list[str]] = []
    panel.evidenceRequested.connect(seen.append)
    assert panel.editor.link_handler(f"ref:{ids[0]}")
    assert seen == [[ids[0]]]
    panel.set_editing(False)
    assert panel.stack.currentWidget() is panel.view

    reader.review_to_note("cornell")
    assert "(p.1)" in reader.note_panel.editor.toPlainText()
    assert "ref:" not in reader.note_panel.editor.toPlainText()

    reader.set_pane_mode("right", "translation")
    assert reader.stacks["right"].currentWidget() is reader.right_scroll
    window.close()


def test_either_pane_shows_any_of_the_four(
    qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from PySide6.QtCore import QUrl

    from paper_saver.config import Settings
    from paper_saver.core.geometry import Rect as PageRect
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
    ids = [s.id for _, _, s in layout.sentences()]
    for kind in ("one_page", "cornell"):
        reader.review_store.put(kind, f"# 문서\n\n- 첫 점. [p.1](ref:{ids[1]})\n- 둘째 점. [p.1](ref:{ids[2]})\n", anchors={})
    assert reader.pane_mode("left") == "original" and reader.pane_mode("right") == "translation"

    # The translation on the left and the original on the right: strokes
    # belong to the side of the paper they were drawn on.
    reader.set_pane_mode("left", "translation")
    reader.set_pane_mode("right", "original")
    assert reader.left.translated and not reader.right.translated
    reader.set_pen("#ffd400")
    reader.left.rangeHighlighted.emit(0, [PageRect(60, 200, 200, 210)], "번역 부분")
    assert project.highlights.strokes[-1].side == "translated"

    # Cornell notes on the left, the PDF on the right: a point lights the right.
    reader.set_pane_mode("left", "cornell")
    qapp.processEvents()
    left = reader.review_panels["left"]
    assert reader.stacks["left"].currentWidget() is left and left.stack.currentWidget() is left.view
    point = next(n for n, refs in enumerate(left._segments) if refs == [ids[2]])
    left._clicked(QUrl(f"seg:{point}"))
    assert reader.right._evidence == [ids[2]]

    # One editor at a time, and an edit shows in the other pane.
    right = reader.review_panels["right"]
    reader.set_pane_mode("right", "cornell")
    left.set_editing(True)
    left.editor.setPlainText(left.editor.toPlainText().replace("첫 점", "고친 점"))
    left.editor.edited.emit()
    right.set_editing(True)
    assert not left.editing and right.editing
    assert "고친 점" in reader.review_store.get("cornell").text and "고친 점" in right.editor.toPlainText()
    right.editor.setPlainText(right.editor.toPlainText().replace("둘째 점", "다시 고친 점"))
    right.flush()
    assert "다시 고친 점" in left.view.toPlainText()
    right.set_editing(False)

    # Documents in both panes: a click turns the other pane to the original.
    reader.set_pane_mode("right", "one_page")
    qapp.processEvents()
    point = next(n for n, refs in enumerate(right._segments) if refs == [ids[1]])
    right._clicked(QUrl(f"seg:{point}"))
    _wait(qapp, lambda: reader.left._evidence == [ids[1]])
    assert reader.pane_mode("left") == "original" and not reader.left.translated
    window.close()
