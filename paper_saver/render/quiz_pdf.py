"""Every slide quiz of a lecture in one printable PDF (§4c).

Two parts, so the sheet can be solved on paper before the answers are seen:

``문제``
    Slide by slide, each question with its difficulty and an O / X to circle.
    Questions are numbered through the whole document.
``정답 및 해설``
    From a fresh page: the answer, the explanation and the hint for every
    question under the same number, and the reader's own answer where they
    gave one in the app.

:func:`slides_quiz_pdf` makes the study sheet instead: every slide on a page
of its own with its questions set beside it (``"right"``) or under it
(``"below"``), then the same answer key. The slide is the lecture PDF's own
page, placed, not pictured, so its text stays searchable; a quiz that does
not fit its panel is set smaller rather than spilling onto another page.

Laid out by MuPDF's HTML engine (``pymupdf.Story``). The face is Malgun
Gothic when Windows has it, otherwise MuPDF's built-in CJK fallback, which
covers Korean on any platform - no font has to be installed for this to work.
No Qt: this runs in a worker and in tests without a display.
"""

from __future__ import annotations

import html
import io
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pymupdf

from ..core.quiz import QuizAttempt, SlideQuiz

__all__ = ["PLACEMENTS", "quiz_pdf", "slides_quiz_pdf"]

#: Where a slide's questions go on its page.
PLACEMENTS = ("right", "below")

_PAGE = pymupdf.paper_rect("a4")
_MARGIN = 54  # 3/4 inch
_LEVELS = {"easy": ("Easy", "#137333"), "medium": ("Medium", "#b06000"), "hard": ("Hard", "#c5221f")}

_CSS = """
* { font-family: sans-serif; }
body { font-size: 10.5pt; line-height: 1.45; color: #202124; }
h1 { font-size: 19pt; margin: 0 0 4pt 0; }
h2 { font-size: 15pt; margin: 0 0 10pt 0; border-bottom: 1px solid #9aa0a6; }
h3 { font-size: 11.5pt; margin: 14pt 0 6pt 0; color: #1a4f8b; }
p { margin: 0 0 3pt 0; }
.meta { color: #5f6368; font-size: 9.5pt; margin-bottom: 14pt; }
.q { margin: 0 0 9pt 0; }
.num { font-weight: bold; }
.mark { color: #5f6368; }
.label { font-weight: bold; }
.right { color: #137333; font-weight: bold; }
.wrong { color: #c5221f; font-weight: bold; }
.small { color: #5f6368; font-size: 9.5pt; }
"""


def _korean_css() -> tuple[str, pymupdf.Archive | None]:
    """Malgun Gothic from the Windows font folder, if it is there."""
    if sys.platform != "win32":
        return "", None
    folder = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    regular, bold = folder / "malgun.ttf", folder / "malgunbd.ttf"
    if not regular.is_file():
        return "", None
    faces = f"@font-face {{ font-family: sans-serif; src: url({regular.name}); }}\n"
    if bold.is_file():
        faces += f"@font-face {{ font-family: sans-serif; font-weight: bold; src: url({bold.name}); }}\n"
    return faces, pymupdf.Archive(str(folder))


def _e(text: Any) -> str:
    return html.escape(str(text)).replace("\n", "<br>")


def _mark(answer: Any) -> str:
    return "O" if answer is True else "X" if answer is False else str(answer)


def _level(difficulty: str) -> str:
    name, color = _LEVELS.get(difficulty, (difficulty.title(), "#5f6368"))
    return f'<span style="color:{color}; font-weight:bold;">[{name}]</span>'


def _slide_heading(quiz: SlideQuiz, titles: dict[int, str]) -> str:
    title = quiz.title or titles.get(quiz.slide, "")
    return f"<h3>Slide {quiz.slide}{' · ' + _e(title) if title else ''}</h3>"


def _questions_html(lecture: str, quizzes: list[SlideQuiz], titles: dict[int, str]) -> str:
    count = sum(len(q.questions) for q in quizzes)
    parts = [
        f"<h1>{_e(lecture or '퀴즈')}</h1>",
        f'<p class="meta">슬라이드 {len(quizzes)}장 · 문제 {count}개 · O/X · {date.today().isoformat()}</p>',
        "<h2>문제</h2>",
    ]
    number = 0
    for quiz in quizzes:
        parts.append(_slide_heading(quiz, titles))
        for question in quiz.questions:
            number += 1
            parts.append(
                f'<p class="q"><span class="num">{number}.</span> {_level(question.difficulty)} '
                f'{_e(question.question)} <span class="mark">( O / X )</span></p>'
            )
    return "\n".join(parts)


def _answers_html(quizzes: list[SlideQuiz], attempts: dict[int, QuizAttempt], titles: dict[int, str]) -> str:
    parts = ["<h2>정답 및 해설</h2>"]
    number = 0
    for quiz in quizzes:
        parts.append(_slide_heading(quiz, titles))
        attempt = attempts.get(quiz.slide)
        for question in quiz.questions:
            number += 1
            line = f'<span class="num">{number}.</span> {_level(question.difficulty)} 정답 <b>{_mark(question.answer)}</b>'
            if attempt is not None and question.id in attempt.answers:
                chosen = attempt.answers[question.id]
                ok = question.is_correct(chosen)
                verdict = '<span class="right">✓</span>' if ok else '<span class="wrong">✗</span>'
                line += f" · 내 답 {_e(_mark(chosen))} {verdict}"
            parts.append(f'<div class="q"><p>{line}</p>')
            parts.append(f'<p><span class="label">해설</span> {_e(question.explanation)}</p>')
            parts.append(f'<p class="small">힌트 · {_e(question.hint)}</p></div>')
    return "\n".join(parts)


def _place(writer: pymupdf.DocumentWriter, story: pymupdf.Story) -> None:
    """Flow one story onto as many fresh pages as it needs."""
    frame = _PAGE + (_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
    more = True
    while more:
        device = writer.begin_page(_PAGE)
        more, _ = story.place(frame)
        story.draw(device)
        writer.end_page()


def quiz_pdf(
    quizzes: dict[int, SlideQuiz],
    target: Path,
    *,
    lecture: str = "",
    attempts: dict[int, QuizAttempt] | None = None,
    titles: dict[int, str] | None = None,
) -> int:
    """Write every quiz, in slide order, to ``target``. Returns the number of slides.

    ``attempts`` adds the reader's answers to the answer key; ``titles`` names
    slides whose quiz carries no title of its own.
    """
    ordered = [quizzes[n] for n in sorted(quizzes)]
    if not ordered:
        raise ValueError("내보낼 퀴즈가 없습니다. 먼저 슬라이드에서 퀴즈를 만들어 주세요.")
    attempts, titles = attempts or {}, titles or {}
    faces, archive = _korean_css()
    css = faces + _CSS

    buffer = io.BytesIO()
    writer = pymupdf.DocumentWriter(buffer)
    for body in (_questions_html(lecture, ordered, titles), _answers_html(ordered, attempts, titles)):
        _place(writer, pymupdf.Story(body, user_css=css, archive=archive))
    writer.close()

    # Page numbers, now that the count is known.
    document = pymupdf.open("pdf", buffer.getvalue())
    total = document.page_count
    for index, page in enumerate(document, start=1):
        label = f"{index} / {total}"
        width = pymupdf.get_text_length(label, fontname="helv", fontsize=8.5)
        page.insert_text(
            ((_PAGE.width - width) / 2, _PAGE.height - 28), label, fontname="helv", fontsize=8.5, color=(0.45, 0.45, 0.45)
        )
    data = document.tobytes(garbage=3, deflate=True)
    document.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(ordered)


# -- slides with their questions ------------------------------------------------------

_PANEL_CSS = """
* { font-family: sans-serif; }
body { line-height: 1.4; color: #202124; }
h3 { font-size: 1.05em; margin: 0 0 0.6em 0; color: #1a4f8b; }
p { margin: 0 0 0.7em 0; }
.num { font-weight: bold; }
.mark { color: #5f6368; }
.none { color: #9aa0a6; }
"""


def _panel_html(quiz: SlideQuiz | None, slide: int, first: int, titles: dict[int, str]) -> str:
    """One slide's questions, numbered from ``first`` to match the answer key."""
    title = (quiz.title if quiz is not None else "") or titles.get(slide, "")
    parts = [f"<h3>Slide {slide}{' · ' + _e(title) if title else ''}</h3>"]
    if quiz is None:
        parts.append('<p class="none">이 슬라이드의 퀴즈는 아직 없습니다.</p>')
        return "\n".join(parts)
    for number, question in enumerate(quiz.questions, start=first):
        parts.append(
            f'<p><span class="num">{number}.</span> {_level(question.difficulty)} '
            f'{_e(question.question)} <span class="mark">( O / X )</span></p>'
        )
    return "\n".join(parts)


def _set_panel(device: Any, body: str, faces: str, archive: pymupdf.Archive | None, area: pymupdf.Rect) -> None:
    """Draw ``body`` into ``area`` at the largest size that fits, down to a floor."""
    size = max(7.0, min(18.0, min(area.width, area.height * 2) / 20))
    while True:
        story = pymupdf.Story(body, user_css=faces + _PANEL_CSS + f"body {{ font-size: {size:.1f}pt; }}", archive=archive)
        more, _ = story.place(area)
        if not more or size <= 5.0:
            story.draw(device)  # at the floor, whatever does not fit is left off
            return
        size = max(5.0, size * 0.9)


def slides_quiz_pdf(
    slides_pdf: Path,
    quizzes: dict[int, SlideQuiz],
    target: Path,
    *,
    placement: str = "right",
    lecture: str = "",
    attempts: dict[int, QuizAttempt] | None = None,
    titles: dict[int, str] | None = None,
) -> int:
    """Each slide with its questions beside or under it, then the answer key.

    Every slide is included, quiz or not, so the file is the whole lecture.
    Returns the number of slides that had a quiz.
    """
    if placement not in PLACEMENTS:
        raise ValueError(f"placement must be one of {PLACEMENTS}, not {placement!r}")
    ordered = [quizzes[n] for n in sorted(quizzes)]
    if not ordered:
        raise ValueError("내보낼 퀴즈가 없습니다. 먼저 슬라이드에서 퀴즈를 만들어 주세요.")
    attempts, titles = attempts or {}, titles or {}
    faces, archive = _korean_css()

    # Number questions through the document, in slide order, as the key does.
    first: dict[int, int] = {}
    count = 0
    for quiz in ordered:
        first[quiz.slide] = count + 1
        count += len(quiz.questions)

    with pymupdf.open(str(slides_pdf)) as slides:
        sheets: list[tuple[pymupdf.Rect, pymupdf.Rect]] = []  # (sheet, where the slide goes)
        buffer = io.BytesIO()
        writer = pymupdf.DocumentWriter(buffer)
        for index in range(slides.page_count):
            w, h = slides[index].rect.width, slides[index].rect.height
            pad = round(min(w, h) * 0.04, 1)
            if placement == "right":
                sheet = pymupdf.Rect(0, 0, w + w * 0.6, h)
                panel = pymupdf.Rect(w + pad, pad, sheet.width - pad, h - pad)
            else:
                sheet = pymupdf.Rect(0, 0, w, h + h * 0.6)
                panel = pymupdf.Rect(pad, h + pad, w - pad, sheet.height - pad)
            sheets.append((sheet, pymupdf.Rect(0, 0, w, h)))
            device = writer.begin_page(sheet)
            quiz = quizzes.get(index + 1)
            body = _panel_html(quiz, index + 1, first.get(index + 1, 0), titles)
            _set_panel(device, body, faces, archive, panel)
            writer.end_page()
        # The key flows over pages the size of the last sheet, so the file is one width.
        key_sheet = sheets[-1][0]
        key_margin = min(key_sheet.width, key_sheet.height) * 0.07
        story = pymupdf.Story(_answers_html(ordered, attempts, titles), user_css=faces + _CSS, archive=archive)
        more = True
        while more:
            device = writer.begin_page(key_sheet)
            more, _ = story.place(key_sheet + (key_margin, key_margin, -key_margin, -key_margin))
            story.draw(device)
            writer.end_page()
        writer.close()

        document = pymupdf.open("pdf", buffer.getvalue())
        for index, (sheet, slot) in enumerate(sheets):
            page = document[index]
            page.show_pdf_page(slot, slides, index)
            # A hairline round the slide: most slides are white on white paper.
            page.draw_rect(slot, color=(0.8, 0.8, 0.8), width=0.6)
        if lecture:
            document.set_metadata({"title": f"{lecture} - 퀴즈"})
        data = document.tobytes(garbage=3, deflate=True)
        document.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return len(ordered)
