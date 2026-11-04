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


# -- linked materials: Lecture Saver and the style library ------------------------------


def _lecture_saver_project(folder: Path, name: str = "OS08-PageReplacement-1") -> Path:
    """A Lecture Saver project as it lies on disk: slide 2 shown twice."""
    root = folder / name
    root.mkdir(parents=True)
    (root / "project.json").write_text(json.dumps({"name": name, "updated_at": "2026-10-14"}), "utf-8")
    segments = [
        {"id": "s1", "start": 1.0, "end": 5.0, "original_text": "today page replacement", "edited_text": None},
        {"id": "s2", "start": 12.0, "end": 20.0, "original_text": "the the referense bit", "edited_text": "the reference bit"},
        {"id": "s3", "start": 21.0, "end": 22.0, "original_text": "um", "edited_text": ""},
        {"id": "s4", "start": 41.0, "end": 47.0, "original_text": "back to the reference bit", "edited_text": None},
    ]
    (root / "transcript.json").write_text(json.dumps({"schema_version": 1, "segments": segments}), "utf-8")
    shown = [(1, 1, 0.0, 10.0), (2, 2, 10.0, 30.0), (3, 3, 30.0, 40.0), (4, 2, 40.0, 50.0)]
    appearances = [{"appearance_id": a, "slide_id": s, "start": b, "end": e} for a, s, b, e in shown]
    (root / "slides.json").write_text(json.dumps({"slides": [], "appearances": appearances}), "utf-8")
    return root


def test_a_lecture_saver_project_reads_as_its_markdown_export(tmp_path: Path) -> None:
    from paper_saver.core.lecture import parse_transcript, visits_by_page
    from paper_saver.core.lecture_saver import find_projects, same_lecture, transcript_markdown

    root = _lecture_saver_project(tmp_path / "Projects")
    _lecture_saver_project(tmp_path / "Projects", "OS08-PageReplacement-2")
    (tmp_path / "Projects" / "not a lecture").mkdir()
    found = find_projects([tmp_path / "Projects", root, tmp_path / "missing"])
    assert [p.name for p in found] == ["OS08-PageReplacement-1", "OS08-PageReplacement-2"]

    sections = parse_transcript(transcript_markdown(root))
    assert [(s.number, s.same_as, s.span) for s in sections] == [
        (1, None, "00:00:00–00:00:10"), (2, None, "00:00:10–00:00:30"),
        (3, None, "00:00:30–00:00:40"), (4, 2, "00:00:40–00:00:50"),
    ]
    # The reader's edits are what is read; a line blanked there is gone.
    assert [s.text for s in sections] == ["today page replacement", "the reference bit", "", "back to the reference bit"]
    assert visits_by_page(sections, {0: [0], 1: [1, 3], 2: [2]})[1] == 2

    assert same_lecture("OS08-PageReplacement.pdf", "OS08-PageReplacement-2")
    assert same_lecture("OS02-LimitedDirectExecution (1).pdf", "OS02-LimitedDirectExecution")
    assert same_lecture("3_EDA.pdf", "3_EDA_statistics")
    assert not same_lecture("OS07-Paging.pdf", "OS08-PageReplacement-1")
    assert not same_lecture("3.pdf", "3_EDA_statistics")  # too short to say


def test_a_linked_recording_follows_lecture_saver_and_outlives_it(tmp_path: Path) -> None:
    from paper_saver.core.lecture_saver import find_projects, transcript_markdown
    from paper_saver.core.quiz import QuizStore

    root = _lecture_saver_project(tmp_path / "Projects")
    project = find_projects([root])[0]
    store = QuizStore(tmp_path / "paper")
    material = store.link_lecture(project, transcript_markdown(root))
    assert store.link_lecture(project, "") is material and len(store.transcripts) == 1
    assert "the reference bit" in store.transcript_parts()[0][1]

    # Corrected in Lecture Saver: read that way here, after a reopen too.
    data = json.loads((root / "transcript.json").read_text("utf-8"))
    data["segments"][0]["edited_text"] = "today, page replacement policies"
    (root / "transcript.json").write_text(json.dumps(data), "utf-8")
    again = QuizStore(tmp_path / "paper")
    assert again.transcripts[0].lecture == str(root.resolve())
    assert "page replacement policies" in again.transcript_parts()[0][1]

    # Moved away: the text read last is still there.
    root.rename(tmp_path / "moved")
    assert "page replacement policies" in again.transcript_parts()[0][1]
    again.remove_transcript(material.key)
    assert again.transcripts == [] and (tmp_path / "paper").is_dir()
    assert not (tmp_path / "paper" / material.text).exists()


def test_the_style_library_links_files_and_course_folders(tmp_path: Path) -> None:
    from paper_saver.core.quiz import QuizStore, StyleEntry, StyleLibrary

    library = StyleLibrary(tmp_path / "Quiz Styles", (".pdf", ".md", ".txt"))
    assert library.entries() == []
    course = tmp_path / "Quiz Styles" / "운영체제"
    course.mkdir(parents=True)
    for name in ("OS10-Quiz.md", "OS2-Quiz.md", "~$lock.md", "notes.docx"):
        (course / name).write_text(f"1. {name} (O)", "utf-8")
    picked = tmp_path / "Downloads" / "OS2-Quiz.md"
    picked.parent.mkdir()
    picked.write_text("1. loose (X)", "utf-8")

    assert library.add(picked) == "OS2-Quiz.md"
    assert library.add(picked) == "OS2-Quiz (2).md"  # a second copy, not an overwrite
    assert library.add(course / "OS10-Quiz.md") == "운영체제/OS10-Quiz.md"  # already there: linked in place
    assert library.entries() == [
        StyleEntry("운영체제", True, 2), StyleEntry("OS2-Quiz (2).md", False, 1), StyleEntry("OS2-Quiz.md", False, 1),
    ]
    assert [p.name for p in library.files("운영체제")] == ["OS2-Quiz.md", "OS10-Quiz.md"]
    assert library.files("gone") == []

    store = QuizStore(tmp_path / "paper")
    store.link_style("운영체제")
    store.link_style("OS2-Quiz (2).md")
    store.link_style("운영체제")
    assert QuizStore(tmp_path / "paper").style_links == ["운영체제", "OS2-Quiz (2).md"]
    # Put in the folder later: taken too. Named like a copy the lecture holds: read once.
    (course / "OS11-Quiz.md").write_text("1. later (O)", "utf-8")
    text = library.text(store.style_links, lambda p: p.read_text("utf-8").upper(), skip=["OS10-Quiz.md"])
    assert [line for line in text.splitlines() if line.startswith("# ")] == [
        "# OS2-Quiz.md", "# OS11-Quiz.md", "# OS2-Quiz (2).md",
    ]
    assert "1. LATER (O)" in text
    store.unlink_style("운영체제")
    assert QuizStore(tmp_path / "paper").style_links == ["OS2-Quiz (2).md"]


def test_panel_offers_the_recordings_and_ticks_the_library(qapp, tmp_path: Path) -> None:
    from PySide6.QtCore import Qt

    from paper_saver.core.lecture_saver import find_projects
    from paper_saver.core.quiz import QuizStore, StyleLibrary
    from paper_saver.ui.quiz_panel import QuizPanel

    projects = find_projects([_lecture_saver_project(tmp_path / "Projects"), _lecture_saver_project(tmp_path / "x", "Other")])
    (tmp_path / "Quiz Styles" / "운영체제").mkdir(parents=True)
    (tmp_path / "Quiz Styles" / "운영체제" / "OS2-Quiz.md").write_text("1. a (O)", "utf-8")
    store = QuizStore(tmp_path / "paper")
    panel = QuizPanel(store, 5, "OS08-PageReplacement.pdf", tmp_path, library=StyleLibrary(tmp_path / "Quiz Styles", (".md",)))
    assert not panel.lecture_offer.isVisibleTo(panel)

    panel.set_lectures(projects, [projects[0].key])
    assert panel.lecture_offer.isVisibleTo(panel)
    assert "OS08-PageReplacement-1" in panel.lecture_offer_label.text()
    assert [(p.name, near, linked) for p, near, linked in panel.lecture_choices()] == [
        ("OS08-PageReplacement-1", True, False), ("Other", False, False),
    ]
    linked: list[list] = []
    panel.lecturesLinked.connect(linked.append)
    panel._fill_lecture_menu()
    menu = [a for a in panel._lecture_menu.actions() if a.isCheckable()]
    menu[0].trigger()
    assert linked == [[projects[0].key]]

    item = panel.style_list.item(0)
    assert item.text() == "📁 운영체제 — 1개"
    item.setCheckState(Qt.CheckState.Checked)
    assert store.style_links == ["운영체제"]
    _wait(qapp, lambda: "스타일 참고 1개" in panel.materials_summary.text())


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


def test_how_long_the_lecture_stayed_on_each_slide() -> None:
    from paper_saver.core.lecture import format_duration, parse_transcript, seconds_by_page

    sections = parse_transcript(
        "## Slide 1\n`00:00:00 – 00:00:30`\nmemory is the topic today\n"
        "## Slide 2\n`00:00:30 – 00:09:47`\nevery process has a private address space virtual view\n"
        "## Slide 3\n`00:09:47 – 00:10:02`\nstack memory allocated implicitly by the compiler"
    )
    assert seconds_by_page(SLIDES, sections) == {0: 30, 1: 557, 2: 15}
    assert format_duration(557) == "9분 17초" and format_duration(45) == "45초"
    assert seconds_by_page(SLIDES, parse_transcript("memory private address space " * 50)) == {}  # no times


def test_what_the_professor_came_back_to_and_kept_saying() -> None:
    from paper_saver.core.lecture import (
        align_transcript,
        parse_transcript,
        repeated_terms,
        visits_by_page,
    )

    slides = [
        "Page Replacement\nLRU algorithm",
        "Reference bits\nEach page has a reference bit set by the MMU",
        "History register\nThe history register keeps eight reference bits",
        "Thrashing\nToo little memory: the process pages all the time",
        "Summary\nmemory pages",
    ]
    said = (
        "## Slide 1\nLRU evicts the page used longest ago. LRU needs the time of every access. LRU again. LRU.\n"
        "## Slide 2\nthe reference bit is set on access. the reference bit is cleared. so the reference bit "
        "tells use. one more reference bit. and the reference bit again.\n"
        "## Slide 3\nthe history register shifts. a history register per page. history register value. "
        "history register compared.\n"
        "## Slide 4 · Same as Slide 2\nback to the reference bit\n"
        "## Slide 5\nthrashing happens when memory is short"
    )
    sections = parse_transcript(said)
    mapping = align_transcript(slides, sections)
    assert visits_by_page(sections, mapping)[1] == 2  # shown, then come back to
    assert visits_by_page(parse_transcript("lru " * 400), {0: [0]}) == {}  # no slide markers, no visits
    # Terms of two or three words and acronyms; "access", "page", "memory" are no terms.
    assert repeated_terms(slides, sections, mapping) == [
        ("reference bit", 6, 1), ("history register", 4, 1), ("LRU", 4, 1),
    ]


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


def test_a_question_rests_on_its_slide_and_the_pages_it_applies() -> None:
    from paper_saver.core.lecture import EvidenceIndex

    index = EvidenceIndex([
        (0, "a", "Lottery Scheduling"),
        (0, "b", "Each job holds tickets; a random draw picks the winner."),
        (1, "c", "Stride Scheduling"),
        (1, "d", "Pick the process to run that has the lowest pass value so far."),
        (1, "e", "Each time a process runs, its pass value grows by its stride."),
        (2, "f", "Comparison between lottery and stride schedulers"),
        (2, "g", "Lottery scheduling has no global state."),
        (3, "h", "Homework: read chapter nine before the next lecture."),
    ])
    # Asked on the comparison slide, it applies the slide before.
    found = index.evidence(
        "Stride scheduling selects a process with the lowest pass value.\n"
        "슬라이드에 따르면 stride scheduling은 pass value가 가장 작은 process를 고릅니다.",
        home=2,
    )
    assert found == {1: ["d", "e"], 2: ["f"]}
    # A word or two in common, with no phrase, lights no other page.
    assert index.evidence("Even the lowest value of a lecture", home=0) == {}


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


def test_an_answer_lights_its_evidence_and_lists_the_pages(qapp, tmp_path: Path) -> None:
    from PySide6.QtWidgets import QLabel, QPushButton

    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict
    from paper_saver.ui.quiz_panel import QuizPanel

    store = QuizStore(tmp_path)
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=2))

    def evidence(question) -> dict[int, list[str]]:
        return {1: ["s2"], 3: ["s4a", "s4b"]} if question.difficulty == "hard" else {}

    panel = QuizPanel(store, 5, "OS06-Memory.pdf", tmp_path, evidence=evidence)
    panel.set_slide(2)
    lit: list[tuple[list, int]] = []
    panel.evidenceRequested.connect(lambda ids, page: lit.append((ids, page)))

    def links() -> list[QLabel]:
        return [label for label in panel._body.findChildren(QLabel) if label.objectName() == "quizEvidence"]

    assert not links()  # nothing before the answer
    hard = [b for b in panel._body.findChildren(QPushButton) if b.text() == "O" and b.isEnabled()][2]
    hard.click()
    assert lit == [(["s2", "s4a", "s4b"], 0)]  # lit at once, the reader left where they are
    [label] = links()
    assert label.text().index("이 슬라이드") < label.text().index(">p.4<")
    label.linkActivated.emit("4")
    assert lit[-1] == (["s2", "s4a", "s4b"], 4)
    easy = [b for b in panel._body.findChildren(QPushButton) if b.text() == "O" and b.isEnabled()][0]
    easy.click()
    assert len(lit) == 2 and len(links()) == 1  # nothing found, nothing lit or listed

    panel.set_away(4)
    assert not panel.away_bar.isHidden() and "p.4" in panel.away_label.text()
    requested: list[int] = []
    panel.slideRequested.connect(requested.append)
    panel.back_button.click()
    assert requested == [2]
    panel.set_away(None)
    assert panel.away_bar.isHidden()


def test_reader_keeps_the_question_while_its_evidence_is_shown(qapp, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    import pymupdf

    from paper_saver.config import Settings
    from paper_saver.core.quiz import slide_quiz_from_dict
    from paper_saver.ui.main_window import MainWindow, _prepare

    pdf = tmp_path / "OS07-Scheduling.pdf"
    doc = pymupdf.open()
    for body in (
        "Lottery scheduling gives each job tickets, and a random draw picks the winner of the processor. "
        "A job holding more tickets wins more often over a long run.",
        "Stride scheduling is deterministic. Pick the process to run that has the lowest pass value so far. "
        "Each time a process runs, its pass value grows by its stride.",
        "Comparison between lottery and stride schedulers. Lottery scheduling has no global state, "
        "so a new job is simple to add.",
    ):
        page = doc.new_page(width=612, height=792)
        page.insert_textbox(pymupdf.Rect(72, 90, 540, 300), body, fontsize=11)
    doc.save(pdf)

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    window.resize(1400, 900)
    window.show()
    project, layout = _prepare(pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    for _ in range(5):  # the first zoom, and the scroll it puts back
        qapp.processEvents()
    reader, panel = window.reader, window.reader.quiz_panel
    page_of = {s.id: page.number for page, _, s in layout.sentences()}

    quiz = _quiz_dict(slide=3)
    [item] = [q for q in quiz["questions"] if q["difficulty"] == "easy"]
    item["question"] = "Stride scheduling selects the process with the lowest pass value."
    item["explanation"] = "pass value가 가장 작은 process를 고릅니다."
    reader.quiz_store.put(slide_quiz_from_dict(quiz, slide=3))
    reader.side.setCurrentWidget(panel)
    reader._show_slide(3)
    qapp.processEvents()
    assert panel.slide == 3

    [easy] = [q for q in reader.quiz_store.get(3).questions if q.difficulty == "easy"]
    panel.choose(easy, True)
    assert {page_of[i] for i in reader.left._evidence} == {1, 2}  # its slide, and the one it applies
    assert reader.right._evidence == reader.left._evidence
    assert panel.slide == 3

    panel.show_evidence(easy, 2)  # "p.2" in its card
    qapp.processEvents()
    assert reader.current_page == 1 and panel.slide == 3  # the question stays
    assert not panel.away_bar.isHidden() and "p.2" in panel.away_label.text()

    reader.left.pressed.emit()  # a click on the page: lights out, the tab follows again
    assert reader.left._evidence == [] and panel.away_bar.isHidden()
    assert panel.slide == 2

    reader._show_slide(3)
    panel.show_evidence(easy, 2)
    qapp.processEvents()
    assert reader.current_page == 1 and panel.slide == 3
    panel.back_button.click()  # or back to the question's slide
    qapp.processEvents()
    assert panel.slide == 3 and panel.away_bar.isHidden()
    window.close()


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


def test_reader_links_the_recording_and_the_course_quizzes(qapp, tmp_path: Path, two_column_pdf: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from PySide6.QtCore import Qt

    from paper_saver.config import Settings
    from paper_saver.ui.main_window import MainWindow, _prepare

    recording = _lecture_saver_project(tmp_path / "Lecture Saver", "synthetic-1")
    data = json.loads((recording / "transcript.json").read_text("utf-8"))
    data["segments"][0]["edited_text"] = "graph neural networks on the SEED dataset, as said in the lecture"
    (recording / "transcript.json").write_text(json.dumps(data), "utf-8")
    course = tmp_path / "Quiz Styles" / "운영체제"
    course.mkdir(parents=True)
    (course / "OS02-Quiz.md").write_text("1. The kernel runs in user mode. (X)\n해설: kernel mode입니다.", "utf-8")

    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("lecture_saver_dir", str(tmp_path / "Lecture Saver"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    project, layout = _prepare(two_column_pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    reader = window.reader
    panel = reader.quiz_panel
    assert panel.library.root == tmp_path / "Quiz Styles"

    # Named like the PDF: offered, and linked with one press.
    _wait(qapp, lambda: panel.lecture_offer.isVisibleTo(panel))
    panel.lecturesLinked.emit(list(panel._suggested))
    _wait(qapp, lambda: bool(reader.quiz_store.transcripts))
    assert reader.quiz_store.transcripts[0].lecture == str(recording.resolve())
    assert "Lecture Saver STT 연결: synthetic-1" in panel.materials_status.text()
    assert not panel.lecture_offer.isVisibleTo(panel)
    assert reader.review_panels["left"].stt_button.text() == "🎙 STT 1개"
    panel.style_list.item(0).setCheckState(Qt.CheckState.Checked)

    engine = FakeEngine([json.dumps(_quiz_dict(slide=1))])
    reader._engine_provider = lambda: engine
    reader.generate_quiz(1)
    _wait(qapp, lambda: reader.quiz_store.get(1) is not None)
    assert "as said in the lecture" in engine.prompts[0]
    assert "The kernel runs in user mode" in engine.prompts[0]
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
    assert [a.text() for a in actions if not a.isSeparator()] == [*quiz_panel.PDF_LAYOUTS.values(), "즐겨찾기한 문제만…"]
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


# -- favorites ------------------------------------------------------------------------


def test_a_starred_question_outlives_regenerate_and_a_reload(tmp_path: Path) -> None:
    import pymupdf

    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict
    from paper_saver.render.quiz_pdf import quiz_pdf

    store = QuizStore(tmp_path)
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=2))
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=5))
    hard2, easy2 = store.get(2).questions[2], store.get(2).questions[0]
    easy5 = store.get(5).questions[0]
    for question in (hard2, easy5, easy2):
        store.set_favorite(question, True)
    store.set_favorite(easy2, True)  # starring twice keeps one
    assert len(store.favorites) == 3 and store.is_favorite(hard2)

    # Regenerate: the same ids, new wording. The starred one stays as it was.
    again = _quiz_dict()
    again["questions"][0]["question"] = "A page table maps virtual pages to frames."
    store.put(slide_quiz_from_dict(again, slide=2))
    fresh = store.get(2).questions[2]
    assert fresh.id == hard2.id and not store.is_favorite(fresh)
    assert store.is_favorite(store.get(2).questions[0])  # easy kept its wording

    store = QuizStore(tmp_path)
    assert [(q.slide, q.difficulty) for q in store.favorite_questions()] == [(2, "easy"), (2, "hard"), (5, "easy")]
    starred = store.favorite_questions()[1]
    assert starred.question == hard2.question and starred.answer is True and starred.explanation == hard2.explanation
    assert starred.slide_title == "Address Spaces"
    store.set_favorite(easy5, False)
    assert sorted(store.favorite_quizzes()) == [2] and len(store.favorite_quizzes()[2].questions) == 2

    target = tmp_path / "favorites.pdf"
    assert quiz_pdf(store.favorite_quizzes(), target, lecture="OS06 · 즐겨찾기") == 1
    with pymupdf.open(target) as document:
        text = "\n".join(page.get_text() for page in document)
    assert "문제 2개" in text and "16KB + 1KB = 17KB." in text
    assert "Each process needs physically separate memory." not in text  # not starred


def test_panel_stars_questions_and_reviews_them_apart(qapp, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QLabel, QPushButton, QToolButton

    from paper_saver.core.quiz import QuizStore, slide_quiz_from_dict
    from paper_saver.ui import quiz_panel
    from paper_saver.ui.quiz_panel import QuizPanel

    store = QuizStore(tmp_path)
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=2))
    store.put(slide_quiz_from_dict(_quiz_dict(), slide=4))
    panel = QuizPanel(store, 5, "OS06-Memory.pdf", tmp_path)
    assert panel.favorites_button.text() == "☆ 즐겨찾기" and not panel.favorites_pdf_action.isEnabled()

    def stars() -> list[QToolButton]:
        return [b for b in panel._body.findChildren(QToolButton) if b.text() in ("☆", "★")]

    def texts() -> list[str]:
        return [label.text() for label in panel._body.findChildren(QLabel)]

    def click(mark: str) -> None:
        next(b for b in panel._body.findChildren(QPushButton) if b.text() == mark and b.isEnabled()).click()

    panel.set_slide(2)
    assert [b.text() for b in stars()] == ["☆", "☆", "☆"]
    stars()[2].click()  # slide 2, hard
    panel.set_slide(4)
    stars()[0].click()  # slide 4, easy
    assert [b.text() for b in stars()] == ["★", "☆", "☆"]
    assert panel.favorites_button.text() == "★ 즐겨찾기 2" and panel.favorites_pdf_action.isEnabled()

    panel.favorites_button.click()
    assert panel.showing_favorites and panel.slide_label.text() == "★ 즐겨찾기 · 2문제"
    assert panel.next_button.isHidden()
    assert any("Slide 2 · Address Spaces" in t for t in texts()) and any("Slide 4" in t for t in texts())
    assert any("If base is 16KB" in t for t in texts()) and "16KB + 1KB = 17KB." not in texts()  # not answered yet
    # Pages scroll by: the starred view stays.
    panel.set_slide(3)
    assert panel.showing_favorites and panel.slide == 3
    # Answered here, kept apart from the slide's own record.
    click("X")  # slide 2 hard: answer O
    assert any(t == "✗ 오답입니다" for t in texts())
    assert any(t.startswith("맞힌 문제 0 / 2   ·   푼 문제 1 / 2") for t in texts())
    assert not store.attempt(2).answers
    next(b for b in panel._body.findChildren(QPushButton) if b.text() == "다시 풀기").click()
    assert any(t.startswith("맞힌 문제 0 / 2   ·   푼 문제 0 / 2") for t in texts())
    # Unstarred here, it leaves the list.
    stars()[1].click()
    assert panel.slide_label.text() == "★ 즐겨찾기 · 1문제" and len(stars()) == 1

    target = str(tmp_path / "fav.pdf")
    monkeypatch.setattr(quiz_panel.QFileDialog, "getSaveFileName", lambda *a, **k: (target, ""))
    requested: list[tuple[str, str]] = []
    panel.pdfExportRequested.connect(lambda path, layout: requested.append((path, layout)))
    panel.favorites_pdf_action.trigger()
    assert requested == [(target, "favorites")]

    # The slide's name goes to that slide's own quiz.
    slides: list[int] = []
    panel.slideRequested.connect(slides.append)
    link = next(label for label in panel._body.findChildren(QLabel) if "Slide 2" in label.text())
    link.linkActivated.emit("2")
    assert slides == [2] and not panel.showing_favorites and not panel.favorites_button.isChecked()


def _deck_pdf(path: Path, topics: list[str]) -> Path:
    """One slide per topic: a title and a few lines about it."""
    import pymupdf

    document = pymupdf.open()
    for topic in topics:
        page = document.new_page(width=720, height=540)
        page.insert_text((40, 60), f"{topic} Algorithm", fontsize=28)
        for row, line in enumerate((f"{topic} replaces the page chosen by its rule", f"{topic} keeps a reference bit per frame")):
            page.insert_text((60, 140 + 40 * row), line, fontsize=18)
    document.save(path)
    document.close()
    return path


def test_reader_makes_every_missing_quiz_and_keeps_going(qapp, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from paper_saver.config import Settings
    from paper_saver.core.quiz import slide_quiz_from_dict
    from paper_saver.translate.engine import EngineRateLimited
    from paper_saver.ui.main_window import MainWindow, _prepare

    pdf = _deck_pdf(tmp_path / "OS08-PageReplacement.pdf", ["FIFO", "Belady", "Clock", "Thrashing"])
    settings = Settings(tmp_path / "settings.json")
    settings.set("projects_dir", str(tmp_path / "projects"))
    settings.set("auto_translate", False)
    window = MainWindow(settings)
    project, layout = _prepare(pdf, None, settings.projects_dir)(threading.Event(), lambda _m: None)
    window._opened((project, layout))
    reader = window.reader
    store, panel = reader.quiz_store, reader.quiz_panel
    made = slide_quiz_from_dict(_quiz_dict(), slide=2)
    store.put(made)
    panel.refresh()
    assert panel.missing() == [1, 3, 4]

    class Engine:
        key = "fake"

        def __init__(self) -> None:
            self.asked: list[str] = []

        def generate_json(self, prompt, schema, *, images=(), cancel):  # noqa: ANN001
            topic = next(t for t in ("FIFO", "Belady", "Clock", "Thrashing") if f"{t} keeps a reference bit" in prompt)
            self.asked.append(topic)
            time.sleep(0.05)
            if topic == "Clock":  # never a valid answer: that slide fails, the rest go on
                return "not json"
            return json.dumps(_quiz_dict())

    engine = Engine()
    reader._engine_provider = lambda: engine
    reader.side.setCurrentWidget(reader.quiz_panel)
    reader.generate_all_quizzes()
    assert panel.busy and panel.batch_bar.isVisibleTo(panel) and "0 / 3" in panel.batch_label.text()
    reader.generate_quiz(1)  # one at a time: ignored while the batch runs
    _wait(qapp, lambda: not panel.busy)
    qapp.processEvents()

    assert sorted(engine.asked) == ["Clock", "Clock", "FIFO", "Thrashing"]  # Clock retried once
    assert store.get(1) is not None and store.get(4) is not None and store.get(3) is None
    assert store.get(1).questions[0].slide == 1 and store.get(1).title.startswith("FIFO")
    assert store.get(2) is made  # what was there stays
    assert "퀴즈 2장을 만들었습니다 · 실패 1장" in panel.batch_label.text()
    assert 3 in panel._errors and panel.missing() == [3]

    class Limited:
        key = "fake"

        def generate_json(self, prompt, schema, *, images=(), cancel):  # noqa: ANN001
            raise EngineRateLimited("usage limit")

    reader._engine_provider = lambda: Limited()
    reader.generate_all_quizzes()
    _wait(qapp, lambda: not panel.busy)
    qapp.processEvents()
    assert "사용량 한도" in panel.batch_label.text() and panel.missing() == [3]
    window.close()
