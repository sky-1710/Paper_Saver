"""The 퀴즈 tab: three O/X questions on the slide in view (§4c).

The tab follows the reader: the slide is the page on screen, and "다음
슬라이드" scrolls the reader on. Nothing is generated until the reader asks,
and a slide's quiz is kept, answers and all, until they ask for a new one.

Above the questions, the materials the quiz is made from, kept visibly apart:
the slides (the PDF already open - there is nothing to upload), the lecture's
STT, and the professor's earlier quizzes as style references.

This widget draws and reports; it runs nothing. Generating a quiz and reading
a material file happen in the reader view's workers (§7.4). Each question is
drawn by a card function chosen by its type, so a new question type is a new
card, not a change here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.quiz import QuizAttempt, QuizFormatError, QuizQuestion, QuizStore, SlideQuiz
from ..pdf.text import MATERIAL_SUFFIXES

__all__ = ["PDF_LAYOUTS", "QuizPanel"]

#: The quiz PDFs there are, by the name the reader view is told.
PDF_LAYOUTS = {
    "quiz": "퀴즈만 모아서…",
    "right": "슬라이드 + 퀴즈 (오른쪽 옆)…",
    "below": "슬라이드 + 퀴즈 (바로 아래)…",
}

_FILTER = "강의 자료 (" + " ".join(f"*{s}" for s in MATERIAL_SUFFIXES) + ")"

_LEVELS = {
    "easy": ("Easy", "#137333", "#e6f4ea"),
    "medium": ("Medium", "#b06000", "#fef7e0"),
    "hard": ("Hard", "#c5221f", "#fce8e6"),
}
_RIGHT = "color:#137333; background:#e6f4ea; border:2px solid #34a853;"
_WRONG = "color:#c5221f; background:#fce8e6; border:2px solid #d93025;"
_IDLE = "color:#9aa0a6; background:#f8f9fa; border:1px solid #e3e6ea;"


def _label(text: str, name: str = "", *, rich: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText if rich else Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if name:
        label.setObjectName(name)
    return label


def _mark(answer: Any) -> str:
    return "O" if answer is True else "X" if answer is False else str(answer)


class QuizPanel(QWidget):
    #: Make (or remake) the quiz of a slide, numbered from 1.
    generateRequested = Signal(int)
    stopRequested = Signal()
    #: Show this slide (from 1) in the reader.
    slideRequested = Signal(int)
    #: A transcript file was picked; the reader view reads and registers it.
    transcriptChosen = Signal(str)
    #: Style reference files were picked.
    stylesChosen = Signal(list)
    #: Every quiz, in one PDF at this path, laid out as "quiz" (questions
    #: only), "right" or "below" (each slide with its questions there); the
    #: reader view writes it.
    pdfExportRequested = Signal(str, str)

    def __init__(
        self,
        store: QuizStore,
        slide_count: int,
        slides_name: str,
        exports_dir: Path,
        lecture_title: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.slide_count = max(1, slide_count)
        self.exports_dir = exports_dir
        self.lecture_title = lecture_title
        self.slide = 1
        self._titles: dict[int, str] = {}
        self._generating: int | None = None
        self._errors: dict[int, str] = {}
        self._material_busy = False
        self._drawn_slide = 0
        self._pdf_busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addWidget(self._build_materials(slides_name))

        head = QHBoxLayout()
        self.slide_label = QLabel()
        self.slide_label.setObjectName("quizSlide")
        self.prev_button = QToolButton(text="◀")
        self.prev_button.setToolTip("이전 슬라이드")
        self.prev_button.clicked.connect(lambda: self.slideRequested.emit(self.slide - 1))
        self.next_button = QToolButton(text="다음 슬라이드 ▶")
        self.next_button.clicked.connect(self._next)
        self.pdf_button = QToolButton(text="PDF로 내보내기 ▾")
        self.pdf_button.setToolTip("만든 퀴즈를 모두 모아 한 PDF로 저장합니다 (끝에 정답 및 해설)")
        self.pdf_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        pdf_menu = QMenu(self.pdf_button)
        for layout_name, text in PDF_LAYOUTS.items():
            pdf_menu.addAction(text, lambda name=layout_name: self.export_pdf(name))
        self.pdf_button.setMenu(pdf_menu)
        head.addWidget(self.slide_label, 1)
        head.addWidget(self.pdf_button)
        head.addWidget(self.prev_button)
        head.addWidget(self.next_button)
        layout.addLayout(head)
        self.title_label = _label("", "quizTitle")
        layout.addWidget(self.title_label)

        self._body = QWidget()
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 4, 4, 4)
        self._body_layout.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._body)
        self._scroll = scroll
        layout.addWidget(scroll, 1)

        self.refresh_materials()
        self.refresh()

    # -- materials -----------------------------------------------------------------

    def _build_materials(self, slides_name: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("quizMaterials")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.setSpacing(4)

        top = QHBoxLayout()
        heading = QLabel("Quiz Materials")
        heading.setObjectName("sectionTitle")
        self.materials_summary = QLabel()
        self.materials_summary.setObjectName("muted")
        self.materials_toggle = QToolButton()
        self.materials_toggle.setCheckable(True)
        self.materials_toggle.toggled.connect(self._show_materials)
        top.addWidget(heading)
        top.addWidget(self.materials_summary, 1)
        top.addWidget(self.materials_toggle)
        outer.addLayout(top)

        self._materials_body = QWidget()
        body = QVBoxLayout(self._materials_body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(3)

        body.addWidget(_label("① Lecture Slides", "materialKind"))
        body.addWidget(_label(f"지금 열려 있는 PDF · {slides_name} ({self.slide_count}장) — 각 쪽이 한 슬라이드입니다.", "muted"))

        body.addWidget(_label("② Lecture Transcript (STT) · 선택", "materialKind"))
        stt_row = QHBoxLayout()
        self.stt_name = _label("")
        self.stt_button = QPushButton()
        self.stt_button.clicked.connect(self._choose_transcript)
        self.stt_remove = QPushButton("제거")
        self.stt_remove.clicked.connect(self._remove_transcript)
        stt_row.addWidget(self.stt_name, 1)
        stt_row.addWidget(self.stt_button)
        stt_row.addWidget(self.stt_remove)
        body.addLayout(stt_row)
        body.addWidget(_label("PDF·txt·md·srt·vtt. 있으면 각 슬라이드를 설명한 부분을 찾아 문제에 함께 씁니다.", "muted"))

        body.addWidget(_label("③ Professor Quiz Style References", "materialKind"))
        self.style_list = QListWidget()
        self.style_list.setMaximumHeight(78)
        self.style_none = _label("없음")
        body.addWidget(self.style_list)
        body.addWidget(self.style_none)
        style_row = QHBoxLayout()
        style_row.addWidget(_label("문체·함정 방식만 참고하고, 내용은 가져오지 않습니다.", "muted"), 1)
        add = QPushButton("추가…")
        add.clicked.connect(self._choose_styles)
        self.style_remove = QPushButton("제거")
        self.style_remove.clicked.connect(self._remove_style)
        style_row.addWidget(add)
        style_row.addWidget(self.style_remove)
        body.addLayout(style_row)

        self.image_box = QCheckBox("슬라이드 이미지도 함께 보내기 (그림·표 속 관계까지 출제)")
        self.image_box.setChecked(self.store.send_slide_image)
        self.image_box.toggled.connect(self.store.set_send_slide_image)
        body.addWidget(self.image_box)
        self.materials_status = _label("", "muted")
        body.addWidget(self.materials_status)
        body.addWidget(
            _label(
                "문제를 만들 때 이 슬라이드의 텍스트, 연결된 STT 구간, 스타일 예시"
                "(켜 두면 슬라이드 이미지도)가 Codex를 통해 OpenAI로 전송됩니다.",
                "muted",
            )
        )
        outer.addWidget(self._materials_body)
        # Open until there is something registered; afterwards it keeps out of the way.
        self.materials_toggle.setChecked(self.store.transcript is None and not self.store.styles)
        self._show_materials(self.materials_toggle.isChecked())
        return frame

    def _show_materials(self, shown: bool) -> None:
        self._materials_body.setVisible(shown)
        self.materials_toggle.setText("접기 ▴" if shown else "자료 ▾")

    def refresh_materials(self) -> None:
        store = self.store
        if store.transcript is not None:
            self.stt_name.setText(store.transcript.name)
            self.stt_button.setText("교체…")
        else:
            self.stt_name.setText("없음")
            self.stt_button.setText("STT 등록…")
        self.stt_remove.setEnabled(store.transcript is not None and not self._material_busy)
        self.stt_button.setEnabled(not self._material_busy)
        self.style_list.clear()
        for material in store.styles:
            item = QListWidgetItem(material.name)
            item.setData(Qt.ItemDataRole.UserRole, material.file)
            self.style_list.addItem(item)
        self.style_remove.setEnabled(bool(store.styles))
        self.style_list.setVisible(bool(store.styles))
        self.style_none.setVisible(not store.styles)
        summary = [f"STT: {store.transcript.name}" if store.transcript else "STT 없음"]
        summary.append(f"스타일 참고 {len(store.styles)}개")
        self.materials_summary.setText(" · ".join(summary))

    def set_material_busy(self, busy: bool, message: str = "") -> None:
        self._material_busy = busy
        self.materials_status.setText(message)
        self.refresh_materials()

    def _start_dir(self) -> str:
        return str(Path.home() / "Downloads")

    def _choose_transcript(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "강의 STT 파일 선택", self._start_dir(), _FILTER)
        if path:
            self.transcriptChosen.emit(path)

    def _remove_transcript(self) -> None:
        if self.store.transcript is None:
            return
        answer = QMessageBox.question(self, "STT 제거", f"'{self.store.transcript.name}'을(를) 퀴즈 자료에서 뺄까요?")
        if answer == QMessageBox.StandardButton.Yes:
            self.store.clear_transcript()
            self.refresh_materials()

    def _choose_styles(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "교수님 이전 퀴즈 선택", self._start_dir(), _FILTER)
        if paths:
            self.stylesChosen.emit(list(paths))

    def _remove_style(self) -> None:
        item = self.style_list.currentItem() or (self.style_list.item(0) if self.style_list.count() == 1 else None)
        if item is None:
            self.materials_status.setText("뺄 참고 자료를 목록에서 고르세요.")
            return
        self.store.remove_style(str(item.data(Qt.ItemDataRole.UserRole)))
        self.refresh_materials()

    # -- which slide -------------------------------------------------------------

    def set_slide(self, slide: int) -> None:
        slide = max(1, min(self.slide_count, slide))
        if slide != self.slide:
            self.slide = slide
            self.refresh()

    def set_titles(self, titles: dict[int, str]) -> None:
        """Slide titles by number (from 1), once the slides have been read."""
        self._titles = dict(titles)
        self._refresh_head()

    def _next(self) -> None:
        if self.slide < self.slide_count:
            self.slideRequested.emit(self.slide + 1)

    def _refresh_head(self) -> None:
        quiz = self.store.get(self.slide)
        title = (quiz.title if quiz and quiz.title else "") or self._titles.get(self.slide, "")
        self.slide_label.setText(f"Slide {self.slide} / {self.slide_count}")
        self.title_label.setText(title)
        self.title_label.setVisible(bool(title))
        self.prev_button.setEnabled(self.slide > 1)
        self.next_button.setEnabled(self.slide < self.slide_count)
        self.pdf_button.setEnabled(bool(self.store.quizzes) and not self._pdf_busy)

    # -- generation state ------------------------------------------------------------

    def set_generating(self, slide: int | None) -> None:
        self._generating = slide
        if slide is not None:
            self._errors.pop(slide, None)
        self.refresh()

    def set_error(self, slide: int, message: str | None) -> None:
        if message:
            self._errors[slide] = message
        else:
            self._errors.pop(slide, None)
        self.refresh()

    def _request(self, *, confirm: bool = False) -> None:
        if self._generating is not None:
            return
        attempt = self.store.attempts.get(self.slide)
        if confirm and attempt is not None and attempt.answers:
            answer = QMessageBox.question(
                self, "Regenerate Quiz", "새 문제를 만들면 지금 문제와 풀이 기록이 사라집니다. 계속할까요?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.generateRequested.emit(self.slide)

    # -- drawing -----------------------------------------------------------------------

    def refresh(self) -> None:
        self._refresh_head()
        # Answering redraws the cards; the reader should stay where they were.
        bar = self._scroll.verticalScrollBar()
        keep = bar.value() if self._drawn_slide == self.slide else 0
        self._drawn_slide = self.slide
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detached at once, deleted later: the card whose button was
                # just clicked may still be inside its signal.
                widget.setParent(None)
                widget.deleteLater()
        quiz = self.store.get(self.slide)
        error = self._errors.get(self.slide)
        if self._generating == self.slide:
            self._draw_generating()
        elif error:
            self._draw_error(error)
        elif quiz is None:
            self._draw_empty()
        else:
            self._draw_quiz(quiz, self.store.attempt(self.slide))
        self._body_layout.addStretch(1)
        QTimer.singleShot(0, bar, lambda: bar.setValue(keep))

    def _add(self, widget: QWidget) -> QWidget:
        self._body_layout.addWidget(widget)
        return widget

    def _button(self, text: str, handler: Callable[[], None], *, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("quizPrimary")
        button.clicked.connect(handler)
        return button

    def _row(self, *widgets: QWidget, stretch_first: bool = False) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        if not stretch_first:
            row.addStretch(1)
        for index, widget in enumerate(widgets):
            row.addWidget(widget, 1 if stretch_first and index == 0 else 0)
        return holder

    def _draw_empty(self) -> None:
        self._add(_label("이 슬라이드의 O/X 퀴즈 3문제(Easy · Medium · Hard)를 만듭니다.", "quizLead"))
        busy = self._generating is not None
        if busy:
            self._add(_label(f"Slide {self._generating}의 퀴즈를 만드는 중입니다. 끝나면 이 슬라이드도 만들 수 있습니다.", "muted"))
        button = self._button("Generate Quiz", self._request, primary=True)
        button.setEnabled(not busy)
        self._add(self._row(button))
        if self.store.transcript is None:
            self._add(_label("STT를 등록하지 않았습니다. 슬라이드만으로 문제를 만듭니다.", "muted"))

    def _draw_generating(self) -> None:
        self._add(_label("Generating quiz…", "quizLead"))
        self._add(_label("Codex가 슬라이드와 강의 내용을 읽고 문제를 쓰고 있습니다. (보통 30초–2분)", "muted"))
        self._add(self._row(self._button("중지", self.stopRequested.emit)))

    def _draw_error(self, message: str) -> None:
        self._add(_label("Quiz generation failed.", "quizError"))
        self._add(_label(message, "muted"))
        retry = self._button("Retry", self._request, primary=True)
        retry.setEnabled(self._generating is None)
        self._add(self._row(retry))

    def _draw_quiz(self, quiz: SlideQuiz, attempt: QuizAttempt) -> None:
        correct, answered = attempt.score(quiz)
        total = len(quiz.questions)
        self._add(_label(f"맞힌 문제 {correct} / {total}   ·   푼 문제 {answered} / {total}", "quizScore"))
        for index, question in enumerate(quiz.questions, start=1):
            card = _CARDS.get(question.type)
            if card is None:
                self._add(_label(f"Question {index}: 이 버전에서 지원하지 않는 형식({question.type})입니다.", "muted"))
                continue
            self._add(card(self, question, index, attempt))
        if answered == total:
            self._add(self._result(correct, total))
        source = quiz.questions[0].source if quiz.questions else {}
        where = f"근거: 슬라이드 {quiz.slide}"
        section = source.get("transcriptSection") if isinstance(source, dict) else None
        where += f" · STT {section}" if section else " · STT 없이 생성"
        self._add(_label(where, "muted"))
        more = QToolButton(text="JSON ▾")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        menu.addAction("퀴즈 JSON 내보내기…", self._export)
        menu.addAction("퀴즈 JSON 가져오기…", self._import)
        more.setMenu(menu)
        regenerate = self._button("Regenerate Quiz", lambda: self._request(confirm=True))
        regenerate.setEnabled(self._generating is None)
        self._add(self._row(more, regenerate))

    def _result(self, correct: int, total: int) -> QFrame:
        box = QFrame()
        box.setObjectName("quizResult")
        layout = QVBoxLayout(box)
        verdict = "모두 맞혔습니다!" if correct == total else "틀린 문제의 해설을 다시 확인해 보세요."
        layout.addWidget(_label(f"결과: {total}문제 중 {correct}문제 정답", "quizLead"))
        layout.addWidget(_label(verdict))
        again = self._button("다시 풀기", self._retry)
        buttons = [again]
        if self.slide < self.slide_count:
            buttons.append(self._button("다음 슬라이드 ▶", self._next, primary=True))
        layout.addWidget(self._row(*buttons))
        return box

    # -- answering ---------------------------------------------------------------------

    def choose(self, question: QuizQuestion, answer: Any) -> None:
        if self.store.choose(self.slide, question.id, answer):
            self.refresh()

    def reveal_hint(self, question: QuizQuestion) -> None:
        self.store.reveal_hint(self.slide, question.id)
        self.refresh()

    def _retry(self) -> None:
        self.store.reset(self.slide)
        self.refresh()

    # -- import / export ---------------------------------------------------------------

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "퀴즈 내보내기", str(self.exports_dir / "quiz.json"), "JSON (*.json)")
        if path:
            count = self.store.export(Path(path), self.lecture_title)
            self.materials_status.setText(f"퀴즈 {count}개를 저장했습니다: {path}")

    def export_pdf(self, layout: str = "quiz") -> None:
        """Ask where, then hand the PDF to the reader view to write."""
        if not self.store.quizzes:
            QMessageBox.information(self, "퀴즈 PDF", "내보낼 퀴즈가 없습니다. 먼저 슬라이드에서 퀴즈를 만들어 주세요.")
            return
        if self._pdf_busy:
            return
        suffix = "퀴즈" if layout == "quiz" else "슬라이드+퀴즈"
        name = f"{self.lecture_title or 'quiz'} - {suffix}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "퀴즈 PDF로 내보내기", str(self.exports_dir / name), "PDF (*.pdf)")
        if path:
            self.pdfExportRequested.emit(path, layout)

    def set_pdf_busy(self, busy: bool, message: str = "") -> None:
        self._pdf_busy = busy
        if message:
            self.materials_status.setText(message)
        self._refresh_head()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "퀴즈 가져오기", str(self.exports_dir), "JSON (*.json)")
        if not path:
            return
        try:
            count = self.store.import_file(Path(path))
        except (OSError, ValueError, QuizFormatError) as exc:
            QMessageBox.warning(self, "퀴즈 가져오기", f"가져오지 못했습니다.\n\n{exc}")
            return
        self.materials_status.setText(f"퀴즈 {count}개를 가져왔습니다.")
        self.refresh()


# -- cards, one per question type --------------------------------------------------------


def _true_false_card(panel: QuizPanel, question: QuizQuestion, index: int, attempt: QuizAttempt) -> QFrame:
    chosen = attempt.answers.get(question.id)
    answered = question.id in attempt.answers
    right = answered and question.is_correct(chosen)

    card = QFrame()
    card.setObjectName("quizCard")
    card.setProperty("result", "" if not answered else "right" if right else "wrong")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)

    name, color, background = _LEVELS.get(question.difficulty, (question.difficulty.title(), "#444", "#eee"))
    badge = QLabel(name)
    badge.setStyleSheet(
        f"color:{color}; background:{background}; border-radius:8px; padding:1px 8px; font-weight:600;"
    )
    number = QLabel(f"Question {index}")
    number.setObjectName("quizNumber")
    hint_button = QToolButton(text="Hint")
    hint_shown = question.id in attempt.hints
    hint_button.setEnabled(not hint_shown)
    hint_button.clicked.connect(lambda: panel.reveal_hint(question))
    top = QHBoxLayout()
    top.addWidget(badge)
    top.addWidget(number)
    top.addStretch(1)
    top.addWidget(hint_button)
    layout.addLayout(top)

    layout.addWidget(_label(question.question, "quizQuestion"))
    if hint_shown:
        layout.addWidget(_label("Hint · " + question.hint, "quizHint"))

    choices = QHBoxLayout()
    for value in (True, False):
        button = QPushButton(_mark(value))
        button.setObjectName("quizChoice")
        if answered:
            button.setEnabled(False)
            if value == question.answer:
                button.setStyleSheet(f"QPushButton:disabled {{ {_RIGHT} }}")
            elif value == chosen:
                button.setStyleSheet(f"QPushButton:disabled {{ {_WRONG} }}")
            else:
                button.setStyleSheet(f"QPushButton:disabled {{ {_IDLE} }}")
        else:
            button.clicked.connect(lambda _=False, v=value: panel.choose(question, v))
        choices.addWidget(button)
    choices.addStretch(1)
    layout.addLayout(choices)

    if answered:
        verdict = _label("✓ 정답입니다" if right else "✗ 오답입니다", "quizRight" if right else "quizWrong")
        layout.addWidget(verdict)
        layout.addWidget(
            _label(
                f"Your Answer: <b>{_mark(chosen)}</b> &nbsp;·&nbsp; Correct Answer: <b>{_mark(question.answer)}</b>",
                rich=True,
            )
        )
        layout.addWidget(_label("Explanation", "quizExplainTitle"))
        layout.addWidget(_label(question.explanation, "quizExplain"))
    return card


#: question type -> the function that draws it.
_CARDS: dict[str, Callable[[QuizPanel, QuizQuestion, int, QuizAttempt], QFrame]] = {
    "true_false": _true_false_card,
}
