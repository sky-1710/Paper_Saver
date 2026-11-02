"""The 퀴즈 tab: three O/X questions on the slide in view (§4c).

The tab follows the reader: the slide is the page on screen, and "다음
슬라이드" scrolls the reader on. Nothing is generated until the reader asks,
and a slide's quiz is kept, answers and all, until they ask for a new one.

Above the questions, the materials the quiz is made from, kept visibly apart:
the slides (the PDF already open - there is nothing to upload), the lecture's
STT, and the professor's earlier quizzes as style references. The STT is
linked from Lecture Saver (the recordings named like this PDF are offered at
once) or picked as files; the style references are ticked in the library,
which holds them once for every lecture.

An answered question lights the sentences it rests on (§4c.5) - on its slide
and on any other page it applies - and its card lists those pages. Going to
one keeps the tab on the question until a page is clicked.

This widget draws and reports; it runs nothing. Generating a quiz and reading
a material file happen in the reader view's workers (§7.4). Each question is
drawn by a card function chosen by its type, so a new question type is a new
card, not a change here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
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

from ..core.lecture_saver import LectureProject
from ..core.quiz import (
    QuizAttempt,
    QuizFormatError,
    QuizQuestion,
    QuizStore,
    SlideQuiz,
    StyleLibrary,
)
from ..pdf.text import MATERIAL_SUFFIXES

__all__ = ["PDF_LAYOUTS", "LectureChoice", "QuizPanel", "fill_lecture_menu"]

#: A Lecture Saver recording as a menu offers it: (project, named like this
#: PDF, linked already).
LectureChoice = tuple[LectureProject, bool, bool]


def fill_lecture_menu(
    menu: QMenu, choices: list[LectureChoice], link: Callable[[list], None], unlink: Callable[[str], None]
) -> None:
    """Each Lecture Saver recording, ticked when linked: a press links or unlinks it."""
    if not choices:
        menu.addAction("Lecture Saver 프로젝트를 찾지 못했습니다").setEnabled(False)
        return
    for index, (project, near, linked) in enumerate(choices):
        if index and choices[index - 1][1] and not near:
            menu.addSeparator()
        action = menu.addAction(("★ " if near else "") + project.name)
        action.setCheckable(True)
        action.setChecked(linked)
        action.triggered.connect(lambda on, key=project.key: link([key]) if on else unlink(key))

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
    #: Transcript files were picked; the reader view reads and registers them.
    transcriptsChosen = Signal(list)
    #: Take this transcript off (its file, relative to the project).
    transcriptRemoved = Signal(str)
    #: Move this transcript earlier (-1) or later (+1) in the lecture.
    transcriptMoved = Signal(str, int)
    #: Read the STT from these Lecture Saver projects (their folders).
    lecturesLinked = Signal(list)
    #: Look through Lecture Saver's projects again.
    lecturesRescan = Signal()
    #: Style reference files were picked: copied into the library and linked.
    stylesChosen = Signal(list)
    #: Every quiz, in one PDF at this path, laid out as "quiz" (questions
    #: only), "right" or "below" (each slide with its questions there); the
    #: reader view writes it.
    pdfExportRequested = Signal(str, str)
    #: Light these sentences; with a page (from 1), go there - 0 stays put.
    evidenceRequested = Signal(list, int)

    def __init__(
        self,
        store: QuizStore,
        slide_count: int,
        slides_name: str,
        exports_dir: Path,
        lecture_title: str = "",
        parent: QWidget | None = None,
        *,
        library: StyleLibrary | None = None,
        evidence: Callable[[QuizQuestion], dict[int, list[str]]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = store
        self.library = library
        #: The sentences a question rests on, {page index: [sentence id]}.
        self._evidence = evidence
        #: Lecture Saver's recordings, and those named like this PDF (their keys).
        self._lectures: list[LectureProject] = []
        self._suggested: list[str] = []
        self._filling = False
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
        # Shown while the reader is on another page for a question's evidence.
        self.away_bar = QFrame()
        self.away_bar.setObjectName("quizAway")
        away = QHBoxLayout(self.away_bar)
        away.setContentsMargins(8, 4, 4, 4)
        self.away_label = _label("")
        self.back_button = QPushButton()
        self.back_button.clicked.connect(lambda: self.slideRequested.emit(self.slide))
        away.addWidget(self.away_label, 1)
        away.addWidget(self.back_button)
        self.away_bar.hide()
        layout.addWidget(self.away_bar)

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

        # Shown, folded or not, while there is no STT and Lecture Saver has
        # recordings named like this PDF: one press links them.
        self.lecture_offer = QWidget()
        offer = QHBoxLayout(self.lecture_offer)
        offer.setContentsMargins(0, 2, 0, 2)
        self.lecture_offer_label = _label("")
        link_offered = QPushButton("STT로 연결")
        link_offered.setObjectName("quizPrimary")
        link_offered.clicked.connect(lambda: self.lecturesLinked.emit(list(self._suggested)))
        offer.addWidget(self.lecture_offer_label, 1)
        offer.addWidget(link_offered)
        outer.addWidget(self.lecture_offer)

        self._materials_body = QWidget()
        body = QVBoxLayout(self._materials_body)
        body.setContentsMargins(0, 2, 0, 0)
        body.setSpacing(3)

        body.addWidget(_label("① Lecture Slides", "materialKind"))
        body.addWidget(_label(f"지금 열려 있는 PDF · {slides_name} ({self.slide_count}장) — 각 쪽이 한 슬라이드입니다.", "muted"))

        body.addWidget(_label("② Lecture Transcript (STT) · 선택 · 여러 개 가능", "materialKind"))
        self.stt_list = QListWidget()
        self.stt_list.setMaximumHeight(78)
        self.stt_none = _label("없음")
        body.addWidget(self.stt_list)
        body.addWidget(self.stt_none)
        stt_row = QHBoxLayout()
        self.lecture_button = QPushButton("Lecture Saver")
        self.lecture_button.setToolTip("Lecture Saver의 녹화에서 STT를 바로 읽습니다 (그쪽에서 고친 내용까지)")
        self._lecture_menu = QMenu(self.lecture_button)
        self._lecture_menu.aboutToShow.connect(self._fill_lecture_menu)
        self.lecture_button.setMenu(self._lecture_menu)
        self.stt_button = QPushButton("파일…")
        self.stt_button.clicked.connect(self._choose_transcripts)
        self.stt_up = QToolButton(text="▲")
        self.stt_up.setToolTip("강의에서 앞쪽으로")
        self.stt_up.clicked.connect(lambda: self._move_transcript(-1))
        self.stt_down = QToolButton(text="▼")
        self.stt_down.setToolTip("강의에서 뒤쪽으로")
        self.stt_down.clicked.connect(lambda: self._move_transcript(1))
        self.stt_remove = QPushButton("제거")
        self.stt_remove.clicked.connect(self._remove_transcript)
        for widget in (self.lecture_button, self.stt_button, self.stt_up, self.stt_down, self.stt_remove):
            stt_row.addWidget(widget)
        stt_row.addStretch(1)
        body.addLayout(stt_row)
        body.addWidget(_label(
            "Lecture Saver 녹화는 연결만 하면 그 프로젝트에서 바로 읽습니다. 파일은 PDF·txt·md·srt·vtt. 각 슬라이드를 "
            "설명한 부분을 찾아 문제에 함께 씁니다. 나눠 찍은 영상은 강의 순서대로 두세요. 한 장 요약·코넬 노트도 "
            "이 STT를 함께 씁니다.", "muted",
        ))

        body.addWidget(_label("③ Professor Quiz Style References", "materialKind"))
        self.style_list = QListWidget()
        self.style_list.setMaximumHeight(110)
        self.style_list.itemChanged.connect(self._style_ticked)
        self.style_none = _label("", "muted")
        body.addWidget(self.style_list)
        body.addWidget(self.style_none)
        style_row = QHBoxLayout()
        add = QPushButton("추가…")
        add.setToolTip("고른 파일을 퀴즈 스타일 폴더에 넣고 이 강의에 연결합니다")
        add.clicked.connect(self._choose_styles)
        self.style_folder = QPushButton("폴더 열기")
        self.style_folder.clicked.connect(self._open_style_folder)
        again = QToolButton(text="↻")
        again.setToolTip("폴더를 다시 읽기")
        again.clicked.connect(self.refresh_materials)
        style_row.addWidget(add)
        style_row.addWidget(self.style_folder)
        style_row.addWidget(again)
        style_row.addStretch(1)
        body.addLayout(style_row)
        where = f" ({library.root})" if (library := self.library) is not None else ""
        body.addWidget(_label(
            f"퀴즈 스타일 폴더{where}에 한 번만 넣어 두고 강의마다 체크만 하세요. 과목 폴더를 체크하면 그 안의 "
            "파일이 모두 (나중에 넣는 것까지) 쓰입니다. 문체·함정 방식만 참고하고, 내용은 가져오지 않습니다.", "muted",
        ))

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
        self.materials_toggle.setChecked(not self.store.transcripts and not self.store.styles and not self.store.style_links)
        self._show_materials(self.materials_toggle.isChecked())
        return frame

    def _show_materials(self, shown: bool) -> None:
        self._materials_body.setVisible(shown)
        self.materials_toggle.setText("접기 ▴" if shown else "자료 ▾")

    def refresh_materials(self) -> None:
        store = self.store
        current = self.stt_list.currentItem()
        selected = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.stt_list.clear()
        for number, material in enumerate(store.transcripts, start=1):
            name = f"🎙 {material.name} · Lecture Saver" if material.lecture else material.name
            item = QListWidgetItem(f"{number}. {name}")
            item.setData(Qt.ItemDataRole.UserRole, material.key)
            if material.lecture:
                item.setToolTip(material.lecture)
            self.stt_list.addItem(item)
            if material.key == selected:
                self.stt_list.setCurrentItem(item)
        heard = bool(store.transcripts)
        self.stt_list.setVisible(heard)
        self.stt_none.setVisible(not heard)
        idle = not self._material_busy
        self.lecture_button.setEnabled(idle)
        self.stt_button.setEnabled(idle)
        self.stt_remove.setEnabled(heard and idle)
        self.stt_up.setEnabled(len(store.transcripts) > 1 and idle)
        self.stt_down.setEnabled(len(store.transcripts) > 1 and idle)
        offered = [p.name for p in self._lectures if p.key in self._suggested]
        self.lecture_offer.setVisible(bool(offered) and not heard and idle)
        self.lecture_offer_label.setText("Lecture Saver에 이 강의의 녹화가 있습니다: " + ", ".join(offered))
        styles = self._fill_styles()
        summary = [f"STT {len(store.transcripts)}개" if store.transcripts else "STT 없음"]
        summary.append(f"스타일 참고 {styles}개")
        self.materials_summary.setText(" · ".join(summary))

    def _fill_styles(self) -> int:
        """The library's folders and files, ticked when linked, then what this
        lecture still holds copied in. Returns how many files are in use."""
        store, library = self.store, self.library
        entries = library.entries() if library is not None else []
        in_use = 0
        self._filling = True
        self.style_list.clear()
        for entry in entries:
            text = f"📁 {entry.link} — {entry.count}개" if entry.folder else entry.link
            self._style_item(text, ("link", entry.link), entry.link in store.style_links)
            in_use += entry.count if entry.link in store.style_links else 0
        shown = {entry.link for entry in entries}
        for link in store.style_links:
            if link not in shown:
                self._style_item(f"{link} (폴더에 없음)", ("link", link), True)
        for material in store.styles:
            self._style_item(f"{material.name} · 이 강의에 복사됨", ("copy", material.file), True)
        in_use += len(store.styles)
        self._filling = False
        any_shown = self.style_list.count() > 0
        self.style_list.setVisible(any_shown)
        self.style_none.setVisible(not any_shown)
        self.style_none.setText("퀴즈 스타일 폴더가 비어 있습니다. '추가…'로 교수님의 이전 퀴즈를 넣으세요.")
        self.style_folder.setEnabled(library is not None)
        return in_use

    def _style_item(self, text: str, data: tuple[str, str], ticked: bool) -> None:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if ticked else Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, data)
        self.style_list.addItem(item)

    def _style_ticked(self, item: QListWidgetItem) -> None:
        if self._filling:
            return
        kind, value = item.data(Qt.ItemDataRole.UserRole)
        on = item.checkState() == Qt.CheckState.Checked
        if kind == "link":
            (self.store.link_style if on else self.store.unlink_style)(value)
        elif not on:
            material = next((m for m in self.store.styles if m.file == value), None)
            answer = QMessageBox.question(
                self, "스타일 참고 빼기", f"이 강의에 복사해 둔 '{material.name if material else value}'을(를) 뺄까요?"
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.store.remove_style(value)
        # Redrawn after this signal returns: the item is still inside it.
        QTimer.singleShot(0, self, self.refresh_materials)

    def _open_style_folder(self) -> None:
        if self.library is None:
            return
        self.library.root.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.library.root)))

    # -- Lecture Saver -------------------------------------------------------------

    def set_lectures(self, projects: list[LectureProject], suggested: list[str]) -> None:
        """What Lecture Saver holds, and the keys of those named like this PDF."""
        self._lectures = list(projects)
        self._suggested = list(suggested)
        self.refresh_materials()

    @property
    def lectures(self) -> list[LectureProject]:
        return list(self._lectures)

    def lecture_choices(self) -> list[LectureChoice]:
        """Those named like this PDF first, each with whether it is linked."""
        linked = {m.lecture for m in self.store.transcripts if m.lecture}
        near = [p for p in self._lectures if p.key in self._suggested]
        rest = [p for p in self._lectures if p.key not in self._suggested]
        return [(p, p in near, p.key in linked) for p in near + rest]

    def _fill_lecture_menu(self) -> None:
        menu = self._lecture_menu
        menu.clear()
        fill_lecture_menu(menu, self.lecture_choices(), self.lecturesLinked.emit, self.transcriptRemoved.emit)
        menu.addSeparator()
        menu.addAction("다시 찾기", self.lecturesRescan.emit)

    def set_material_busy(self, busy: bool, message: str = "") -> None:
        self._material_busy = busy
        self.materials_status.setText(message)
        self.refresh_materials()

    def _start_dir(self) -> str:
        return str(Path.home() / "Downloads")

    def _choose_transcripts(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "강의 STT 파일 선택 (여러 개 가능)", self._start_dir(), _FILTER)
        if paths:
            self.transcriptsChosen.emit(list(paths))

    def _chosen_transcript(self) -> str | None:
        item = self.stt_list.currentItem() or (self.stt_list.item(0) if self.stt_list.count() == 1 else None)
        if item is None:
            self.materials_status.setText("STT를 목록에서 고르세요.")
            return None
        return str(item.data(Qt.ItemDataRole.UserRole))

    def _move_transcript(self, step: int) -> None:
        key = self._chosen_transcript()
        if key is not None:
            self.transcriptMoved.emit(key, step)

    def _remove_transcript(self) -> None:
        key = self._chosen_transcript()
        material = next((m for m in self.store.transcripts if m.key == key), None)
        if material is None:
            return
        answer = QMessageBox.question(
            self, "STT 제거", f"'{material.name}'을(를) 강의 자료에서 뺄까요? (퀴즈와 한 장 요약·코넬 노트 모두)"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.transcriptRemoved.emit(material.key)

    def _choose_styles(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "교수님 이전 퀴즈 선택", self._start_dir(), _FILTER)
        if paths:
            self.stylesChosen.emit(list(paths))

    # -- which slide -------------------------------------------------------------

    def set_slide(self, slide: int) -> None:
        slide = max(1, min(self.slide_count, slide))
        if slide != self.slide:
            self.slide = slide
            self.refresh()

    def set_away(self, page: int | None) -> None:
        """The reader is on ``page`` (from 1) for a question's evidence while
        this tab stays on its slide; None when it follows the pages again."""
        self.away_bar.setVisible(page is not None)
        if page is not None:
            self.away_label.setText(
                f"p.{page} 보는 중 — 퀴즈는 Slide {self.slide}에 머뭅니다. 페이지를 누르면 다시 따라갑니다."
            )
            self.back_button.setText(f"◀ Slide {self.slide}")

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
        if not self.store.transcripts:
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
            # The explanation is up: light what it rests on, here and elsewhere.
            self.show_evidence(question)

    def evidence_of(self, question: QuizQuestion) -> dict[int, list[str]]:
        """The sentences ``question`` rests on: ``{page index: [sentence id]}``."""
        return self._evidence(question) if self._evidence is not None else {}

    def show_evidence(self, question: QuizQuestion, page: int = 0) -> None:
        """Light the sentences ``question`` rests on, on every page; with
        ``page`` (from 1), go there."""
        ids = [i for group in self.evidence_of(question).values() for i in group]
        if ids:
            self.evidenceRequested.emit(ids, page)

    def _evidence_links(self, question: QuizQuestion) -> QLabel | None:
        """"근거 위치  이 슬라이드 · p.4 · p.7", each page a link."""
        pages = sorted((page + 1 for page in self.evidence_of(question)), key=lambda page: page != question.slide)
        if not pages:
            return None
        links = " · ".join(
            f'<a href="{page}" style="color:#137333; text-decoration:none;">'
            + ("이 슬라이드" if page == question.slide else f"p.{page}")
            + "</a>"
            for page in pages
        )
        label = QLabel(f"근거 위치 &nbsp;{links}")
        label.setObjectName("quizEvidence")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setToolTip("누르면 그 페이지로 가서 근거 문장을 표시합니다. 퀴즈는 이 문제에 머뭅니다.")
        label.linkActivated.connect(lambda href, q=question: self.show_evidence(q, int(href)))
        return label

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
        links = panel._evidence_links(question)
        if links is not None:
            layout.addWidget(links)
    return card


#: question type -> the function that draws it.
_CARDS: dict[str, Callable[[QuizPanel, QuizQuestion, int, QuizAttempt], QFrame]] = {
    "true_false": _true_false_card,
}
