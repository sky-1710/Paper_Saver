"""Slide quizzes: the questions, the reader's answers, and the materials (§4c).

Everything lives in the project folder::

    <project>/quiz.json                 quizzes by slide, answers, material list
    <project>/quiz/materials/           the STT files and style references copied in,
                                        each with its extracted text beside it

except what is linked rather than copied. A Lecture Saver recording is read
from its own project every time (:mod:`.lecture_saver`), so an edit made there
shows here; a copy of its text is kept in ``quiz/materials/`` in case the
project moves. The professor's earlier quizzes are kept once, in a folder of
their own (:class:`StyleLibrary`), and each lecture names the files or the
course folders it takes from there.

The STT is the lecture's, not the quiz's: a professor who records a lecture in
parts leaves several files, kept in lecture order, and the one-page review and
Cornell notes (§4d) read the same list.

A quiz is kept until the reader asks for a new one, so going back to a slide
shows the same questions and the answers already given - Codex runs once per
slide, not once per visit.

The JSON of a quiz is the shape the reader asked for, camelCase and all, so a
quiz file can be exported, edited by hand and imported again::

    {"slide": 2, "title": "Address Spaces", "questions": [
        {"id": "os06-memory-slide2-easy", "slide": 2, "slideTitle": "Address Spaces",
         "difficulty": "easy", "type": "true_false", "question": "…",
         "answer": true, "hint": "…", "explanation": "…",
         "source": {"slide": 2, "transcriptSection": "Slide 3 (00:00:20–00:03:41)"}}]}

:func:`slide_quiz_from_dict` is the one gate every quiz passes through -
Codex's answer and an imported file alike - and it is strict about content:
three questions, one per difficulty, each complete, and a hint that does not
give the answer away. Question types other than true/false are a validator
and a card away (``_ANSWER_CHECKS``); nothing else here knows the type.
"""

from __future__ import annotations

import copy
import logging
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_io import read_json, write_json_atomic, write_text_atomic
from .lecture_saver import LectureProject, LectureSaverError, transcript_markdown

log = logging.getLogger(__name__)

__all__ = [
    "DIFFICULTIES",
    "Favorite",
    "Material",
    "QuizAttempt",
    "QuizFormatError",
    "QuizQuestion",
    "QuizStore",
    "SlideQuiz",
    "StyleEntry",
    "StyleLibrary",
    "id_prefix",
    "slide_quiz_from_dict",
]

DIFFICULTIES = ("easy", "medium", "hard")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class QuizFormatError(ValueError):
    """A quiz that is not what was asked for. ``str(err)`` says what is wrong."""


# -- the questions -----------------------------------------------------------------


@dataclass
class QuizQuestion:
    id: str
    slide: int
    difficulty: str
    type: str
    question: str
    #: For "true_false", a bool. Other types will carry their own kind of answer.
    answer: Any
    hint: str
    explanation: str
    slide_title: str = ""
    source: dict[str, Any] = field(default_factory=dict)

    def is_correct(self, chosen: Any) -> bool:
        return bool(chosen == self.answer)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "slide": self.slide,
            "slideTitle": self.slide_title,
            "difficulty": self.difficulty,
            "type": self.type,
            "question": self.question,
            "answer": self.answer,
            "hint": self.hint,
            "explanation": self.explanation,
        }
        if self.source:
            data["source"] = dict(self.source)
        return data


@dataclass
class SlideQuiz:
    slide: int
    title: str
    questions: list[QuizQuestion]
    generated: str = ""
    engine: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "slide": self.slide,
            "title": self.title,
            "questions": [q.to_dict() for q in self.questions],
        }
        if self.generated:
            data["generated"] = self.generated
        if self.engine:
            data["engine"] = self.engine
        return data

    def question(self, question_id: str) -> QuizQuestion | None:
        return next((q for q in self.questions if q.id == question_id), None)


def question_from_dict(data: Any) -> QuizQuestion:
    """One question as :meth:`QuizQuestion.to_dict` wrote it."""
    if not isinstance(data, dict):
        raise QuizFormatError("문제가 JSON 객체가 아닙니다.")
    slide = data.get("slide")
    if not isinstance(slide, int) or isinstance(slide, bool) or slide < 1:
        raise QuizFormatError("slide 번호가 없습니다.")
    level = str(data.get("difficulty") or "").strip().lower()
    if level not in DIFFICULTIES:
        raise QuizFormatError(f"difficulty '{level}'는 easy/medium/hard가 아닙니다.")
    kind = str(data.get("type") or "true_false").strip().lower()
    check = _ANSWER_CHECKS.get(kind)
    if check is None:
        raise QuizFormatError(f"지원하지 않는 문제 형식 '{kind}'.")
    return QuizQuestion(
        id=str(data.get("id") or f"slide{slide}-{level}"),
        slide=slide,
        difficulty=level,
        type=kind,
        question=_text(data, "question", "문제"),
        answer=check(data.get("answer"), "문제"),
        hint=str(data.get("hint") or "").strip(),
        explanation=str(data.get("explanation") or "").strip(),
        slide_title=str(data.get("slideTitle") or ""),
        source=dict(data["source"]) if isinstance(data.get("source"), dict) else {},
    )


def _text(item: dict[str, Any], key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise QuizFormatError(f"{where}: '{key}'가 비어 있습니다.")
    return " ".join(value.split()) if key == "question" else value.strip()


def _true_false(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "o", "t", "yes"):
            return True
        if word in ("false", "x", "f", "no"):
            return False
    raise QuizFormatError(f"{where}: answer가 true/false가 아닙니다 ({value!r}).")


#: How each question type's answer is checked and normalised.
_ANSWER_CHECKS = {"true_false": _true_false}

#: A hint that says what the answer is has stopped being a hint.
_GIVEAWAY = re.compile(
    r"정답|answer is|the statement is (?:true|false)|\(\s*[OX]\s*\)|참입니다|거짓입니다|맞습니다|틀립니다",
    re.IGNORECASE,
)


def slide_quiz_from_dict(
    data: Any, *, slide: int | None = None, title: str = "", id_prefix: str = "", source: dict[str, Any] | None = None
) -> SlideQuiz:
    """Check a quiz and put it in order: easy, medium, hard.

    ``slide`` and ``title``, when given, override what the data says - the
    quiz belongs to the slide it was asked for. Question ids are rebuilt from
    the slide and difficulty so they are unique across the lecture.
    """
    if not isinstance(data, dict):
        raise QuizFormatError("퀴즈가 JSON 객체가 아닙니다.")
    number = slide if slide is not None else data.get("slide")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise QuizFormatError("slide 번호가 없습니다.")
    name = (title or str(data.get("title") or "")).strip()
    items = data.get("questions")
    if not isinstance(items, list):
        raise QuizFormatError("questions 목록이 없습니다.")

    by_level: dict[str, QuizQuestion] = {}
    for index, item in enumerate(items, start=1):
        where = f"문제 {index}"
        if not isinstance(item, dict):
            raise QuizFormatError(f"{where}: 객체가 아닙니다.")
        level = str(item.get("difficulty") or "").strip().lower()
        if level not in DIFFICULTIES:
            raise QuizFormatError(f"{where}: difficulty '{level}'는 easy/medium/hard가 아닙니다.")
        if level in by_level:
            raise QuizFormatError(f"{level} 문제가 두 개 이상입니다.")
        kind = str(item.get("type") or "true_false").strip().lower()
        check = _ANSWER_CHECKS.get(kind)
        if check is None:
            raise QuizFormatError(f"{where}: 지원하지 않는 문제 형식 '{kind}'.")
        hint = _text(item, "hint", where)
        if _GIVEAWAY.search(hint):
            raise QuizFormatError(f"{where}: hint가 정답을 직접 알려 줍니다.")
        own_source = item.get("source") if isinstance(item.get("source"), dict) else {}
        by_level[level] = QuizQuestion(
            id=f"{id_prefix}slide{number}-{level}",
            slide=number,
            difficulty=level,
            type=kind,
            question=_text(item, "question", where),
            answer=check(item.get("answer"), where),
            hint=hint,
            explanation=_text(item, "explanation", where),
            slide_title=name,
            source=dict(source) if source is not None else dict(own_source),
        )
    missing = [level for level in DIFFICULTIES if level not in by_level]
    if missing:
        raise QuizFormatError(f"{', '.join(missing)} 문제가 없습니다.")
    return SlideQuiz(
        slide=number,
        title=name,
        questions=[by_level[level] for level in DIFFICULTIES],
        generated=str(data.get("generated") or ""),
        engine=str(data.get("engine") or ""),
    )


@dataclass
class QuizAttempt:
    """What the reader did with one slide's quiz."""

    #: Question id -> the answer chosen. Once chosen, it stays chosen.
    answers: dict[str, Any] = field(default_factory=dict)
    #: Questions whose hint was opened.
    hints: list[str] = field(default_factory=list)

    def score(self, quiz: SlideQuiz) -> tuple[int, int]:
        """(correct, answered)."""
        answered = [q for q in quiz.questions if q.id in self.answers]
        return sum(1 for q in answered if q.is_correct(self.answers[q.id])), len(answered)


@dataclass
class Favorite:
    """A question the reader starred, kept whole: Regenerate Quiz gives its
    slide new questions under the same ids, and the starred one stays."""

    question: QuizQuestion
    starred: str = field(default_factory=_now)


# -- materials ---------------------------------------------------------------------


@dataclass
class Material:
    """A file the reader registered, copied into the project - or a Lecture
    Saver recording, linked."""

    #: The name it had where the reader picked it.
    name: str
    #: The copy, relative to the project; "" for a linked recording.
    file: str
    #: The text taken from it, relative to the project. For a linked
    #: recording, the text read last, used when the project cannot be.
    text: str
    added: str = field(default_factory=_now)
    #: The Lecture Saver project folder it is read from, if linked.
    lecture: str = ""

    @property
    def key(self) -> str:
        """What names it in the list: its copy, or the project it is read from."""
        return self.lecture or self.file


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:120] or "material"


class QuizStore:
    FILE = "quiz.json"
    FOLDER = "quiz"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / self.FILE
        #: The lecture's STT, one file per recording, in lecture order.
        self.transcripts: list[Material] = []
        #: Style references copied in, before there was a library to link from.
        self.styles: list[Material] = []
        #: Files and folders of the style library, relative to it.
        self.style_links: list[str] = []
        #: Send the slide's picture along, so figures can be asked about.
        self.send_slide_image = False
        self.quizzes: dict[int, SlideQuiz] = {}
        self.attempts: dict[int, QuizAttempt] = {}
        #: Starred questions, in the order they were starred.
        self.favorites: list[Favorite] = []
        self._load()

    # -- disk ----------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = read_json(self.path)
        except (OSError, ValueError):
            backup = self.path.with_suffix(".json.damaged")
            log.exception("Could not read %s; keeping it as %s", self.path, backup.name)
            try:
                self.path.replace(backup)
            except OSError:
                pass
            return
        materials = data.get("materials") or {}
        try:
            # One file before several could be registered: "transcript".
            listed = materials.get("transcripts") or ([materials["transcript"]] if materials.get("transcript") else [])
            self.transcripts = [Material(**m) for m in listed]
            self.styles = [Material(**m) for m in materials.get("styles") or []]
        except TypeError:
            log.warning("Quiz materials list is malformed; ignoring it")
        self.style_links = [str(link) for link in materials.get("styleLinks") or [] if link]
        self.send_slide_image = bool(data.get("sendSlideImage", False))
        for item in data.get("quizzes") or []:
            try:
                quiz = slide_quiz_from_dict(item, id_prefix=_prefix_of(item))
            except QuizFormatError as exc:
                log.warning("Skipping a stored quiz: %s", exc)
                continue
            self.quizzes[quiz.slide] = quiz
        for key, item in (data.get("attempts") or {}).items():
            if str(key).isdigit() and isinstance(item, dict):
                self.attempts[int(key)] = QuizAttempt(
                    answers=dict(item.get("answers") or {}), hints=list(item.get("hints") or [])
                )
        for item in data.get("favorites") or []:
            try:
                question = question_from_dict(item.get("question") if isinstance(item, dict) else None)
            except QuizFormatError as exc:
                log.warning("Skipping a starred question: %s", exc)
                continue
            self.favorites.append(Favorite(question, str(item.get("starred") or "")))

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "materials": {
                    "transcripts": [m.__dict__ for m in self.transcripts],
                    "styles": [m.__dict__ for m in self.styles],
                    "styleLinks": self.style_links,
                },
                "sendSlideImage": self.send_slide_image,
                "quizzes": [self.quizzes[n].to_dict() for n in sorted(self.quizzes)],
                "attempts": {
                    str(n): {"answers": a.answers, "hints": a.hints}
                    for n, a in sorted(self.attempts.items())
                    if a.answers or a.hints
                },
                "favorites": [{"question": f.question.to_dict(), "starred": f.starred} for f in self.favorites],
            },
        )

    # -- materials -----------------------------------------------------------------

    @property
    def materials_dir(self) -> Path:
        return self.root / self.FOLDER / "materials"

    def import_material(self, source: Path, text: str) -> Material:
        """Copy a file in and write its text beside it; the store is unchanged.

        Touches only new files, so a worker may do it while the reader keeps
        answering; :meth:`add_transcript` / :meth:`add_style` then take it in.
        """
        folder = self.materials_dir
        folder.mkdir(parents=True, exist_ok=True)
        base = _safe_name(source.name)
        target = folder / base
        counter = 2
        while target.exists():
            target = folder / f"{Path(base).stem} ({counter}){Path(base).suffix}"
            counter += 1
        shutil.copy2(source, target)
        text_path = target.with_name(target.name + ".txt")
        write_text_atomic(text_path, text)
        return Material(
            name=source.name,
            file=target.relative_to(self.root).as_posix(),
            text=text_path.relative_to(self.root).as_posix(),
        )

    def _discard(self, material: Material) -> None:
        for relative in (material.file, material.text):
            if not relative:
                continue
            try:
                (self.root / relative).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove %s", relative, exc_info=True)

    def add_transcript(self, material: Material) -> None:
        """Register one recording's STT, after the ones already there."""
        self.transcripts.append(material)
        self.save()

    def link_lecture(self, project: LectureProject, text: str) -> Material:
        """Read the STT from a Lecture Saver project from now on, after the
        ones already there. ``text`` is what it holds now, kept as the copy."""
        existing = next((m for m in self.transcripts if m.lecture == project.key), None)
        if existing is not None:
            return existing
        folder = self.materials_dir
        folder.mkdir(parents=True, exist_ok=True)
        base = _safe_name(project.name) + ".lecture"
        target, counter = folder / f"{base}.md", 2
        while target.exists():
            target = folder / f"{base} ({counter}).md"
            counter += 1
        write_text_atomic(target, text)
        material = Material(
            name=project.name, file="", text=target.relative_to(self.root).as_posix(), lecture=project.key
        )
        self.add_transcript(material)
        return material

    def remove_transcript(self, key: str) -> None:
        for material in [m for m in self.transcripts if m.key == key]:
            self._discard(material)
        self.transcripts = [m for m in self.transcripts if m.key != key]
        self.save()

    def move_transcript(self, key: str, step: int) -> None:
        """Earlier (-1) or later (+1) in the lecture."""
        index = next((i for i, m in enumerate(self.transcripts) if m.key == key), None)
        if index is None or not 0 <= index + step < len(self.transcripts):
            return
        items = self.transcripts
        items[index], items[index + step] = items[index + step], items[index]
        self.save()

    def add_style(self, material: Material) -> None:
        """Register one of the professor's earlier quizzes, as extracted examples."""
        self.styles.append(material)
        self.save()

    def remove_style(self, file: str) -> None:
        for material in [m for m in self.styles if m.file == file]:
            self._discard(material)
        self.styles = [m for m in self.styles if m.file != file]
        self.save()

    def link_style(self, link: str) -> None:
        """Take a file or a folder of the style library into this lecture's quizzes."""
        if link not in self.style_links:
            self.style_links.append(link)
            self.save()

    def unlink_style(self, link: str) -> None:
        if link in self.style_links:
            self.style_links.remove(link)
            self.save()

    def _read_text(self, material: Material | None) -> str:
        if material is None:
            return ""
        copy = self.root / material.text
        if material.lecture:
            try:
                text = transcript_markdown(Path(material.lecture))
            except LectureSaverError:
                log.warning("Lecture Saver project unreadable, using the copy: %s", material.lecture, exc_info=True)
            else:
                try:
                    if not copy.is_file() or copy.read_text(encoding="utf-8") != text:
                        write_text_atomic(copy, text)
                except OSError:
                    log.warning("Could not keep a copy of %s", material.lecture, exc_info=True)
                return text
        try:
            return copy.read_text(encoding="utf-8")
        except OSError:
            log.warning("Material text missing: %s", material.text)
            return ""

    def transcript_parts(self) -> list[tuple[str, str]]:
        """Each recording's STT as (file name, text), in lecture order."""
        return [(m.name, text) for m in self.transcripts if (text := self._read_text(m)).strip()]

    def style_text(self) -> str:
        """Every copied reference's examples, each under the name of its file.
        The linked ones are read by :meth:`StyleLibrary.text`."""
        parts = []
        for material in self.styles:
            text = self._read_text(material).strip()
            if text:
                parts.append(f"# {material.name}\n{text}")
        return "\n\n".join(parts)

    def set_send_slide_image(self, on: bool) -> None:
        self.send_slide_image = on
        self.save()

    # -- quizzes and answers ------------------------------------------------------

    def get(self, slide: int) -> SlideQuiz | None:
        return self.quizzes.get(slide)

    def attempt(self, slide: int) -> QuizAttempt:
        return self.attempts.setdefault(slide, QuizAttempt())

    def put(self, quiz: SlideQuiz) -> None:
        """A new quiz for its slide; the answers to the old one go with it."""
        self.quizzes[quiz.slide] = quiz
        self.attempts[quiz.slide] = QuizAttempt()
        self.save()

    def choose(self, slide: int, question_id: str, answer: Any) -> bool:
        """Record an answer. Returns False if it had already been answered."""
        attempt = self.attempt(slide)
        if question_id in attempt.answers:
            return False
        attempt.answers[question_id] = answer
        self.save()
        return True

    def reveal_hint(self, slide: int, question_id: str) -> None:
        attempt = self.attempt(slide)
        if question_id not in attempt.hints:
            attempt.hints.append(question_id)
            self.save()

    def reset(self, slide: int) -> None:
        """Try the same questions again."""
        self.attempts[slide] = QuizAttempt()
        self.save()

    # -- favorites -----------------------------------------------------------------

    def _favorite(self, question: QuizQuestion) -> Favorite | None:
        # A question is its slide and its wording: ids are reused by Regenerate.
        return next(
            (f for f in self.favorites if f.question.slide == question.slide and f.question.question == question.question),
            None,
        )

    def is_favorite(self, question: QuizQuestion) -> bool:
        return self._favorite(question) is not None

    def set_favorite(self, question: QuizQuestion, on: bool) -> None:
        found = self._favorite(question)
        if on and found is None:
            self.favorites.append(Favorite(copy.deepcopy(question)))
        elif not on and found is not None:
            self.favorites.remove(found)
        else:
            return
        self.save()

    def favorite_questions(self) -> list[QuizQuestion]:
        """The starred questions in lecture order: by slide, then Easy to Hard."""
        order = {level: n for n, level in enumerate(DIFFICULTIES)}
        ranked = sorted(
            enumerate(self.favorites),
            key=lambda item: (item[1].question.slide, order.get(item[1].question.difficulty, 9), item[0]),
        )
        return [f.question for _, f in ranked]

    def favorite_quizzes(self) -> dict[int, SlideQuiz]:
        """The starred questions as one quiz per slide, for a PDF."""
        quizzes: dict[int, SlideQuiz] = {}
        for question in self.favorite_questions():
            quiz = quizzes.setdefault(question.slide, SlideQuiz(question.slide, question.slide_title, []))
            quiz.questions.append(question)
        return quizzes

    # -- import / export ---------------------------------------------------------

    def export(self, path: Path, lecture: str = "") -> int:
        quizzes = [self.quizzes[n].to_dict() for n in sorted(self.quizzes)]
        write_json_atomic(path, {"lecture": lecture, "quizzes": quizzes})
        return len(quizzes)

    def import_file(self, path: Path) -> int:
        """Quizzes from a file made by :meth:`export` (or one quiz on its own).

        All or nothing: one bad quiz and nothing is imported, so a file is
        never half taken in.
        """
        data = read_json(path)
        items = data.get("quizzes") if isinstance(data, dict) and "quizzes" in data else [data]
        if not isinstance(items, list):
            raise QuizFormatError("quizzes 목록이 없습니다.")
        quizzes = [slide_quiz_from_dict(item, id_prefix=_prefix_of(item)) for item in items]
        for quiz in quizzes:
            self.quizzes[quiz.slide] = quiz
            self.attempts[quiz.slide] = QuizAttempt()
        self.save()
        return len(quizzes)


# -- the style library ---------------------------------------------------------------


@dataclass(frozen=True)
class StyleEntry:
    """A file or a folder at the top of the style library."""

    #: Relative to the library, "/"-separated: what a lecture links to.
    link: str
    folder: bool
    #: The files it holds: 1 for a file.
    count: int


def _natural(name: str) -> list[Any]:
    """"OS2" before "OS10"."""
    return [int(piece) if piece.isdigit() else piece for piece in re.split(r"(\d+)", name.lower())]


class StyleLibrary:
    """The professor's earlier quizzes, kept once and linked from each lecture.

    A plain folder the reader may fill and arrange in the file browser too.
    A lecture links a file, or a folder - a course - and so takes every file
    in it, those put there later as well. Nothing is extracted ahead: the
    files are read when a quiz or a review is asked for, in its worker, so a
    file replaced in the folder is read as it now is.
    """

    def __init__(self, root: Path, suffixes: Sequence[str]) -> None:
        self.root = Path(root)
        self.suffixes = tuple(s.lower() for s in suffixes)

    def _material(self, path: Path) -> bool:
        return path.suffix.lower() in self.suffixes and not path.name.startswith((".", "~$")) and path.is_file()

    def _files_in(self, folder: Path) -> list[Path]:
        try:
            found = [p for p in folder.rglob("*") if self._material(p)]
        except OSError:
            return []
        return sorted(found, key=lambda p: _natural(p.relative_to(folder).as_posix()))

    def entries(self) -> list[StyleEntry]:
        """Folders first, then loose files, each by name."""
        try:
            items = sorted(self.root.iterdir(), key=lambda p: _natural(p.name)) if self.root.is_dir() else []
        except OSError:
            items = []
        folders = [StyleEntry(p.name, True, len(self._files_in(p))) for p in items if p.is_dir() and p.name[0] != "."]
        return folders + [StyleEntry(p.name, False, 1) for p in items if self._material(p)]

    def files(self, link: str) -> list[Path]:
        """What a link stands for now; nothing if it is gone."""
        path = self.root / link
        if path.is_dir():
            return self._files_in(path)
        return [path] if self._material(path) else []

    def add(self, source: Path) -> str:
        """Copy a file to the top of the library, under a name of its own, and
        return its link. A file already in the library is linked where it is."""
        source = Path(source)
        try:
            return source.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            pass
        self.root.mkdir(parents=True, exist_ok=True)
        base = _safe_name(source.name)
        target, counter = self.root / base, 2
        while target.exists():
            target = self.root / f"{Path(base).stem} ({counter}){Path(base).suffix}"
            counter += 1
        shutil.copy2(source, target)
        return target.name

    def text(self, links: Sequence[str], read: Callable[[Path], str], skip: Sequence[str] = ()) -> str:
        """Every linked file's examples under its name, as
        :meth:`QuizStore.style_text` gives the copied ones. A file named in
        ``skip`` (copied into the lecture already) or reached twice is read
        once; one that cannot be read is passed over."""
        seen = set(skip)
        parts = []
        for link in links:
            for path in self.files(link):
                if path.name in seen:
                    continue
                seen.add(path.name)
                try:
                    text = read(path).strip()
                except Exception:  # pymupdf raises several unrelated types
                    log.warning("Could not read the style reference %s", path, exc_info=True)
                    continue
                if text:
                    parts.append(f"# {path.name}\n{text}")
        return "\n\n".join(parts)


def _prefix_of(item: Any) -> str:
    """The id prefix a stored quiz was made with ("os06-memory-")."""
    try:
        first = str(item["questions"][0]["id"])
    except (KeyError, IndexError, TypeError):
        return ""
    match = re.match(r"(.*?)slide\d+-", first)
    return match.group(1) if match else ""


def id_prefix(title: str) -> str:
    """"OS06-Memory" -> "os06-memory-": question ids readable in a JSON file."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return f"{slug[:40]}-" if slug else ""
