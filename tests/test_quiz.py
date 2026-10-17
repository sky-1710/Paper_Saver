"""Slide quizzes: materials, slide ↔ STT matching, validation, storage, the tab - no Codex."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


def _quiz_dict(**changes) -> dict:
    questions = [
        {"id": "q3", "difficulty": "hard", "type": "true_false", "question": "If base is 16KB, virtual 1KB maps to physical 17KB.",
         "answer": True, "hint": "base를 더해 보세요.", "explanation": "16KB + 1KB = 17KB."},
        {"id": "q1", "difficulty": "easy", "type": "true_false", "question": "An address space is an abstraction of physical memory.",
         "answer": True, "hint": "슬라이드 제목 아래 첫 줄을 떠올려 보세요.", "explanation": "슬라이드 정의 그대로입니다."},
        {"id": "q2", "difficulty": "medium", "type": "true_false", "question": "Each process needs physically separate memory.",
         "answer": "X", "hint": "'private'가 하드웨어 분리를 뜻하는지 생각해 보세요.", "explanation": "물리 메모리는 공유됩니다."},
    ]
    data = {"slide": 2, "title": "Address Spaces", "questions": questions}
    data.update(changes)
    return data


# -- validation ----------------------------------------------------------------------


def test_quiz_is_validated_ordered_and_given_unique_ids() -> None:
    from paper_saver.core.quiz import slide_quiz_from_dict

    quiz = slide_quiz_from_dict(_quiz_dict(), slide=2, id_prefix="os06-memory-", source={"slide": 2})
    assert [q.difficulty for q in quiz.questions] == ["easy", "medium", "hard"]
    assert [q.id for q in quiz.questions] == ["os06-memory-slide2-easy", "os06-memory-slide2-medium", "os06-memory-slide2-hard"]
    assert quiz.questions[1].answer is False  # "X" read as false
    data = quiz.to_dict()
    assert data["questions"][0]["slideTitle"] == "Address Spaces"
    assert data["questions"][0]["source"] == {"slide": 2}
    assert json.loads(json.dumps(data)) == data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["questions"].pop(2), "medium"),
        (lambda d: d["questions"][0].update(difficulty="easy"), "두 개"),
        (lambda d: d["questions"][0].update(hint="정답은 O입니다."), "hint"),
        (lambda d: d["questions"][0].update(answer="maybe"), "answer"),
        (lambda d: d["questions"][0].update(explanation=" "), "explanation"),
        (lambda d: d["questions"][0].update(type="essay"), "형식"),
    ],
)
def test_bad_quizzes_are_rejected(change, message) -> None:
    from paper_saver.core.quiz import QuizFormatError, slide_quiz_from_dict

    data = _quiz_dict()
    change(data)
    with pytest.raises(QuizFormatError, match=message):
        slide_quiz_from_dict(data, slide=2)


# -- the generator --------------------------------------------------------------------


class FakeEngine:
    key = "fake"

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.schemas: list[dict] = []

    def generate_json(self, prompt, schema, *, images=(), cancel):  # noqa: ANN001
        self.prompts.append(prompt)
        self.schemas.append(schema)
        return self.answers.pop(0)


def test_generator_retries_once_with_the_reason_and_keeps_to_the_sources() -> None:
    from paper_saver.translate.quiz import QUIZ_SCHEMA, CodexQuizGenerator, QuizGenerationInput

    bad = _quiz_dict()
    bad["questions"][0]["hint"] = "The answer is true."
    engine = FakeEngine([json.dumps(bad), "```json\n" + json.dumps(_quiz_dict()) + "\n```"])
    request = QuizGenerationInput(
        slide_number=2, slide_title="Address Spaces", slide_content="Easy to use abstraction of physical memory",
        transcript_content="each process can consider it has its own private contiguous memory space",
        transcript_section="Slide 3 (00:00:20–00:03:41)",
        quiz_style_examples="- Traps can only occur when there is a hardware error. (X)",
        id_prefix="os06-",
    )
    quiz = CodexQuizGenerator(engine).generate_slide_quiz(request, cancel=threading.Event())  # type: ignore[arg-type]

    assert quiz.engine == "fake" and quiz.generated
    assert quiz.questions[0].source == {"slide": 2, "transcriptSection": "Slide 3 (00:00:20–00:03:41)"}
    first, second = engine.prompts
    assert "Easy to use abstraction" in first and "private contiguous memory space" in first
    assert "스타일 참고용" in first and "Traps can only occur" in first
    assert "형식 검사를 통과하지 못했습니다" in second and "hint" in second
    assert engine.schemas[0] is QUIZ_SCHEMA


def test_generator_gives_up_after_two_bad_answers() -> None:
    from paper_saver.translate.engine import TranslationError
    from paper_saver.translate.quiz import CodexQuizGenerator, QuizGenerationInput

    engine = FakeEngine(["no json here", "{}"])
    with pytest.raises(TranslationError, match="형식 검사"):
        CodexQuizGenerator(engine).generate_slide_quiz(  # type: ignore[arg-type]
            QuizGenerationInput(slide_number=1, slide_content="x"), cancel=threading.Event()
        )


def test_schema_is_strict_and_passed_to_codex_before_the_stdin_marker(tmp_path: Path) -> None:
    from paper_saver.translate.codex_cli import CodexEngine
    from paper_saver.translate.quiz import QUIZ_SCHEMA

    item = QUIZ_SCHEMA["properties"]["questions"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False
    cmd = CodexEngine()._command(Path("codex"), tmp_path / "a.md", (), tmp_path / "schema.json")
    assert cmd[cmd.index("--output-schema") + 1] == str(tmp_path / "schema.json")
    assert cmd[-1] == "-"


# -- storage ---------------------------------------------------------------------------


def test_store_keeps_materials_quizzes_and_answers(tmp_path: Path) -> None:
    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict

    source = tmp_path / "in" / "OS06 lecture.md"
    source.parent.mkdir()
    source.write_text("## Slide 1\nhello", encoding="utf-8")
    root = tmp_path / "project"
    store = QuizStore(root)
    store.add_transcript(store.import_material(source, "hello"))
    store.add_style(store.import_material(source, "- style (O)"))
    assert store.transcript_parts() == [("OS06 lecture.md", "hello")]
    assert "# OS06 lecture.md\n- style (O)" in store.style_text()
    assert store.styles[0].file != store.transcripts[0].file  # a second copy, not an overwrite

    quiz = slide_quiz_from_dict(_quiz_dict(), slide=2, id_prefix="os06-")
    store.put(quiz)
    easy, medium = quiz.questions[0], quiz.questions[1]
    assert store.choose(2, easy.id, True)
    assert not store.choose(2, easy.id, False)  # an answer, once given, stays
    store.choose(2, medium.id, True)
    store.reveal_hint(2, medium.id)
    assert store.attempt(2).score(quiz) == (1, 2)

    again = QuizStore(root)
    assert again.get(2).questions[0].id == "os06-slide2-easy"
    assert again.attempt(2).answers == {easy.id: True, medium.id: True}
    assert again.attempt(2).hints == [medium.id]
    assert again.transcripts[0].name == "OS06 lecture.md"

    exported = tmp_path / "quiz.json"
    assert again.export(exported, "OS06") == 1
    other = QuizStore(tmp_path / "other")
    assert other.import_file(exported) == 1
    assert other.get(2).questions[2].question.startswith("If base is 16KB")

    # A lecture recorded in parts: several files, kept in lecture order.
    second = tmp_path / "in" / "OS06 part 2.md"
    second.write_text("## Slide 1\nworld", encoding="utf-8")
    again.add_transcript(again.import_material(second, "world"))
    first = again.transcripts[0].file
    again.move_transcript(again.transcripts[1].file, -1)
    assert [name for name, _ in QuizStore(root).transcript_parts()] == ["OS06 part 2.md", "OS06 lecture.md"]
    again.remove_transcript(first)
    assert not (root / first).exists() and [m.name for m in again.transcripts] == ["OS06 part 2.md"]


def test_a_quiz_file_with_one_transcript_still_loads(tmp_path: Path) -> None:
    from paper_saver.core.quiz import QuizStore

    material = {"name": "stt.md", "file": "quiz/materials/stt.md", "text": "quiz/materials/stt.md.txt", "added": ""}
    (tmp_path / "quiz.json").write_text(json.dumps({"materials": {"transcript": material, "styles": []}}), "utf-8")
    store = QuizStore(tmp_path)
    assert [m.name for m in store.transcripts] == ["stt.md"]
    store.save()
    saved = json.loads((tmp_path / "quiz.json").read_text("utf-8"))
    assert saved["materials"]["transcripts"][0]["name"] == "stt.md"


def test_import_is_all_or_nothing(tmp_path: Path) -> None:
    from paper_saver.core.quiz import QuizFormatError, QuizStore

    bad = _quiz_dict(slide=3)
    bad["questions"].pop()
    path = tmp_path / "in.json"
    path.write_text(json.dumps({"quizzes": [_quiz_dict(), bad]}), encoding="utf-8")
    store = QuizStore(tmp_path / "p")
    with pytest.raises(QuizFormatError):
        store.import_file(path)
    assert store.quizzes == {}


# -- lecture text ---------------------------------------------------------------------

LECTURE_MD = """# OS06-Memory

*OS06-Memory.mp4 · 1 h 25 min*

## Slide 1

`00:00:00 – 00:00:16`

![Slide 1](<slides/001.jpg>)

**[00:00:00]** today's topic is memory

> 오늘의 주제는 memory입니다

## Slide 2 · With notes

`00:00:16 – 00:03:41`

**[00:00:20]** each process has a private contiguous address space, a virtual view of physical memory

> 각 process는 private contiguous address space를 가집니다

## Slide 3

*Nothing was said while this slide was on screen.*

## Slide 4 · With notes

**[00:04:00]** stack memory is allocated implicitly by the compiler, heap memory by malloc and free

> stack memory는 compiler가 암묵적으로 할당합니다

## Slide 5 · Same as Slide 2

**[00:06:00]** again the address space is private for each process

> 다시 address space는 private입니다
"""

SLIDES = [
    "1\nDongkun Shin, SKKU\nMemory\nChap 13, 14",
    "2\nDongkun Shin, SKKU\nAddress Spaces\n•\nEasy to use abstraction of physical memory\nprivate address space virtual view",
    "3\nDongkun Shin, SKKU\nTypes of Memory\nStack memory allocated implicitly by the compiler\nHeap memory malloc free",
    "4\nDongkun Shin, SKKU\nHomework\nRead chapter 13",
]


def test_slide_deck_drops_furniture_and_finds_titles() -> None:
    from paper_saver.core.lecture import SlideDeck

    deck = SlideDeck(SLIDES)
    assert deck.title(1) == "Address Spaces"
    assert "Dongkun Shin" not in deck.content(1) and "•" not in deck.content(1).splitlines()
    assert deck.content(1).startswith("Address Spaces\nEasy to use abstraction")


def test_transcript_sections_are_parsed_and_matched_to_slides() -> None:
    from paper_saver.core.lecture import align_transcript, parse_transcript, transcript_excerpt

    sections = parse_transcript(LECTURE_MD)
    assert [s.label for s in sections] == ["Slide 1", "Slide 2", "Slide 3", "Slide 4", "Slide 5"]
    second = sections[1]
    assert second.span == "00:00:16–00:03:41"
    assert second.text.startswith("each process has a private") and "각" not in second.text  # translation dropped
    assert sections[2].text == "" and sections[4].same_as == 2

    mapping = align_transcript(SLIDES, sections)
    assert mapping[1] == [1, 4]  # Address Spaces: its own section and the "Same as Slide 2" return
    assert mapping[2] == [3]  # Types of Memory
    text, label = transcript_excerpt(sections, mapping[1])
    assert "[Slide 2 · 00:00:16–00:03:41]" in text and "again the address space" in text
    assert label.startswith("Slide 2 (00:00:16–00:03:41), Slide 5")


def test_a_lecture_recorded_in_parts_is_one_lecture() -> None:
    from paper_saver.core.lecture import combine_transcripts, parse_transcript, transcript_by_page

    first = parse_transcript(
        "## Slide 1\nmemory is the topic today\n## Slide 2\n" + "every process has a private address space virtual view " * 8
    )
    second = parse_transcript(
        "## Slide 1\n" + "stack memory is allocated implicitly by the compiler " * 8 + "\n"
        "## Slide 2 · Same as Slide 1\nheap memory with malloc and free"
    )
    sections = combine_transcripts([first, second])
    assert [s.label for s in sections] == ["영상 1 · Slide 1", "영상 1 · Slide 2", "영상 2 · Slide 1", "영상 2 · Slide 2"]
    assert sections[3].same_as == sections[2].number == 3  # its own Slide 1, not the first part's
    assert combine_transcripts([first]) == first

    by_page = transcript_by_page(SLIDES, sections)
    assert "private address space" in by_page[1]
    assert "[영상 2 · Slide 1]" in by_page[2] and "malloc" in by_page[2]
    cut = transcript_by_page(SLIDES, sections, budget=400)
    assert sum(map(len, cut.values())) < sum(map(len, by_page.values()))


def test_transcript_without_markers_is_chunked_and_still_matched() -> None:
    from paper_saver.core.lecture import CHUNK_CHARS, align_transcript, parse_transcript

    text = ("the compiler allocates stack memory implicitly and malloc manages heap memory. " * 40) + "\n" + (
        "homework: read chapter thirteen before next week. " * 40
    )
    sections = parse_transcript(text)
    assert len(sections) >= 2 and all(s.label.startswith("구간") for s in sections)
    assert all(len(s.text) <= CHUNK_CHARS + 100 for s in sections)
    mapping = align_transcript(SLIDES, sections)
    assert 0 in mapping[2]


def test_quiz_examples_are_taken_in_sequence() -> None:
    from paper_saver.core.lecture import extract_quiz_examples, format_quiz_examples

    pages = [
        "1\nDongkun Shin, SKKU\nQuiz\n1.\nTraps can only occur when there is a hardware error. (X)\n"
        "2.\nP1 (arrival 0, burst 7) and P2 (arrival 2, burst 4) run under STCF; the average is 6. (O)\n"
        "↳STCF: (12+5+1)/3 = 6. SJF: (7+10+4)/3 = 7. P1 P2 P3 P1 P3 P2 STCF SJF\n",
        "2\nDongkun Shin, SKKU\nQuiz\n3. In MLFQ, a new job starts in the lowest-priority queue. (X)\n"
        "↳ According to Rule 3 it starts in the highest-priority queue.\n",
        "3\nDongkun Shin, SKKU\nQuiz\n",
    ]
    examples = extract_quiz_examples(pages)
    assert [e.answer for e in examples] == [False, True, False]
    assert examples[1].explanation == "STCF: (12+5+1)/3 = 6. SJF: (7+10+4)/3 = 7."
    assert examples[2].statement.startswith("In MLFQ")
    text = format_quiz_examples(examples)
    assert "- Traps can only occur when there is a hardware error. (X)" in text
    assert format_quiz_examples([], fallback="free   text") == "free text"


# -- the tab --------------------------------------------------------------------------


def test_panel_shows_answers_hints_and_result(qapp, tmp_path: Path) -> None:
    from PySide6.QtWidgets import QLabel, QPushButton, QToolButton

    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict
    from paper_saver.ui.quiz_panel import QuizPanel

    store = QuizStore(tmp_path)
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=2))
    panel = QuizPanel(store, 5, "OS06-Memory.pdf", tmp_path)
    panel.set_slide(2)

    def texts() -> list[str]:
        return [label.text() for label in panel._body.findChildren(QLabel)]

    assert any(t.startswith("맞힌 문제 0 / 3") for t in texts())
    assert not any("Explanation" == t for t in texts())
    hint = next(b for b in panel._body.findChildren(QToolButton) if "Hint" in b.text())
    hint.click()
    assert any("슬라이드 제목 아래" in t for t in texts())

    def click(mark: str, index: int) -> None:
        buttons = [b for b in panel._body.findChildren(QPushButton) if b.text() == mark and b.isEnabled()]
        buttons[index].click()

    click("X", 0)  # easy: answer O, chose X
    assert any("Your Answer: <b>X</b>" in t and "Correct Answer: <b>O</b>" in t for t in texts())
    assert any(t == "✗ 오답입니다" for t in texts()) and any(t == "슬라이드 정의 그대로입니다." for t in texts())
    click("X", 0)  # medium: answer X
    click("O", 0)  # hard: answer O
    assert any(t.startswith("맞힌 문제 2 / 3   ·   푼 문제 3 / 3") for t in texts())
    assert any(t == "결과: 3문제 중 2문제 정답" for t in texts())
    requested: list[int] = []
    panel.slideRequested.connect(requested.append)
    next(b for b in panel._body.findChildren(QPushButton) if b.text() == "다음 슬라이드 ▶").click()
    assert requested == [3]


def _wait(qapp, condition, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert condition()


def test_reader_generates_a_quiz_for_the_slide_in_view(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    reader = window.reader
    engine = FakeEngine([json.dumps(_quiz_dict(slide=1))])
    reader._engine_provider = lambda: engine

    transcript = tmp_path / "stt.md"
    transcript.write_text("## Slide 1\n**[00:00:01]** graph neural networks and the SEED dataset accuracy", encoding="utf-8")
    reader._register_transcripts([str(transcript)])
    _wait(qapp, lambda: bool(reader.quiz_store.transcripts))
    assert "STT 등록" in reader.quiz_panel.materials_status.text()

    reader.side.setCurrentWidget(reader.quiz_panel)
    reader.generate_quiz(1)
    reader.generate_quiz(1)  # a second press while one runs does nothing
    _wait(qapp, lambda: reader.quiz_store.get(1) is not None)
    qapp.processEvents()
    assert len(engine.prompts) == 1
    assert "graph neural networks and the SEED dataset" in engine.prompts[0]
    quiz = reader.quiz_store.get(1)
    assert quiz.questions[0].source["transcriptSection"].startswith("Slide 1")
    assert reader.quiz_panel._generating is None

    target = tmp_path / "exports" / "quiz.pdf"
    reader.export_quiz_pdf(str(target))
    _wait(qapp, lambda: target.is_file() and not reader.quiz_panel._pdf_busy)
    assert "PDF로 저장했습니다" in reader.quiz_panel.materials_status.text()

    beside = tmp_path / "exports" / "slides.pdf"
    reader.export_quiz_pdf(str(beside), "right")
    _wait(qapp, lambda: beside.is_file() and not reader.quiz_panel._pdf_busy)
    window.close()


# -- PDF -------------------------------------------------------------------------------


def test_every_quiz_goes_into_one_pdf_questions_then_answers(tmp_path: Path) -> None:
    import pymupdf

    from paper_saver.core.quiz import QuizAttempt, slide_quiz_from_dict
    from paper_saver.render.quiz_pdf import quiz_pdf

    quizzes = {n: slide_quiz_from_dict(_quiz_dict(), slide=n) for n in (5, 2)}
    attempts = {2: QuizAttempt(answers={"slide2-easy": False})}
    target = tmp_path / "out" / "quiz.pdf"
    assert quiz_pdf(quizzes, target, lecture="OS06 Memory", attempts=attempts) == 2

    with pymupdf.open(target) as document:
        text = "\n".join(page.get_text() for page in document)
        answers_page = next(i for i, page in enumerate(document) if "정답 및 해설" in page.get_text())
        assert "정답 및 해설" not in document[0].get_text()  # answers start on their own page
        assert answers_page > 0
    assert "OS06 Memory" in text and "문제 6개" in text
    assert text.index("Slide 2") < text.index("Slide 5")  # slide order, not insertion order
    assert "An address space is an abstraction of physical memory." in text
    assert "16KB + 1KB = 17KB." in text and "base를 더해 보세요." in text
    assert "내 답 X" in text  # the reader's answer to slide 2's easy question


def test_pdf_with_no_quizzes_is_refused(tmp_path: Path) -> None:
    from paper_saver.render.quiz_pdf import quiz_pdf

    with pytest.raises(ValueError):
        quiz_pdf({}, tmp_path / "quiz.pdf")
    assert not (tmp_path / "quiz.pdf").exists()


def test_panel_pdf_button_needs_a_quiz(qapp, tmp_path: Path, monkeypatch) -> None:
    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict
    from paper_saver.ui import quiz_panel
    from paper_saver.ui.quiz_panel import QuizPanel

    store = QuizStore(tmp_path)
    panel = QuizPanel(store, 5, "OS06-Memory.pdf", tmp_path, "OS06")
    assert not panel.pdf_button.isEnabled()
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=2))
    panel.refresh()
    assert panel.pdf_button.isEnabled()

    target = str(tmp_path / "all.pdf")
    monkeypatch.setattr(quiz_panel.QFileDialog, "getSaveFileName", lambda *a, **k: (target, ""))
    requested: list[tuple[str, str]] = []
    panel.pdfExportRequested.connect(lambda path, layout: requested.append((path, layout)))
    actions = panel.pdf_button.menu().actions()
    assert [a.text() for a in actions] == list(quiz_panel.PDF_LAYOUTS.values())
    actions[0].trigger()
    actions[2].trigger()
    assert requested == [(target, "quiz"), (target, "below")]
    panel.set_pdf_busy(True)
    assert not panel.pdf_button.isEnabled()


@pytest.mark.parametrize("placement", ["right", "below"])
def test_slides_with_their_quizzes(tmp_path: Path, placement: str) -> None:
    import pymupdf

    from paper_saver.core.quiz import slide_quiz_from_dict
    from paper_saver.render.quiz_pdf import slides_quiz_pdf

    deck = tmp_path / "deck.pdf"
    with pymupdf.open() as document:
        for n in range(1, 4):
            document.new_page(width=720, height=540).insert_text((72, 100), f"Slide body {n}", fontsize=28)
        document.save(deck)
    quizzes = {n: slide_quiz_from_dict(_quiz_dict(), slide=n) for n in (3, 1)}
    target = tmp_path / "study.pdf"
    assert slides_quiz_pdf(deck, quizzes, target, placement=placement) == 2

    with pymupdf.open(target) as document:
        assert document.page_count >= 4  # every slide, then the key
        first = document[0]
        wide = first.rect.width > first.rect.height * 1.5
        assert wide == (placement == "right")
        slide = pymupdf.Rect(0, 0, 720, 540)
        # The slide is the deck's own page, text and all, at its own size.
        assert first.get_text(clip=slide).split() == ["Slide", "body", "1"]
        panel = first.get_text(clip=pymupdf.Rect(720, 0, first.rect.width, 540) if placement == "right" else pymupdf.Rect(0, 540, 720, first.rect.height))
        assert "1." in panel and "An address space is an abstraction of physical memory." in " ".join(panel.split())
        assert "아직 없습니다" in document[1].get_text()  # slide 2 has no quiz, but is still there
        assert "4. [Easy]" in document[2].get_text()  # numbered through, as the key is
        assert "정답 및 해설" in document[3].get_text()
