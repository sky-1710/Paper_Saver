"""The window: open a paper, translate it, read it, export it.

Opening is: find (or make) the paper's project, load its cached layout or
read the PDF, and show the reader. Then, if the reader has agreed to
translation, translation starts on its own from the page in view (§5.4) - the
workflow is "give it a paper", not "give it a paper, then press six buttons".
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QIcon,
    QKeySequence,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_DISPLAY_NAME, Settings, dictionary_cache
from ..core.models import PaperLayout
from ..core.project import HIGHLIGHT_COLORS, Project
from ..pdf.document import PaperDocument, PaperOpenError
from ..pdf.extract import extract_paper
from ..render.export import translated_pdf, write_notes_markdown
from ..render.fonts import FontBook
from ..translate import engine_from_settings
from ..translate.dictionary import Dictionary
from ..translate.engine import EngineRateLimited, TranslationCancelled, TranslationEngine
from ..translate.runner import Progress, translate_paper
from .reader_view import ReaderView
from .session import PaperSession
from .workers import BackgroundTask

log = logging.getLogger(__name__)

__all__ = ["MainWindow"]


# -- opening a paper, off the UI thread ------------------------------------------


def _prepare(pdf: Path | None, root: Path | None, projects_dir: Path):  # type: ignore[no-untyped-def]
    def work(_cancel: threading.Event, report) -> tuple[Project, PaperLayout]:  # type: ignore[no-untyped-def]
        if root is not None:
            project = Project(root)
        else:
            assert pdf is not None
            report("논문을 여는 중…")
            with PaperDocument(pdf) as document:
                title = document.title()
            project = Project.find_or_create(pdf, projects_dir, title)
        layout = project.load_layout()
        if layout is None:
            report("레이아웃 분석 중… (처음 한 번만)")
            with PaperDocument(project.pdf_path) as document:
                layout = extract_paper(document.raw)
            project.save_layout(layout)
        project.bind(layout)
        return project, layout

    return work


def _swatch(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


class StartView(QWidget):
    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        title = QLabel(APP_DISPLAY_NAME)
        title.setStyleSheet("font-size: 26pt; font-weight: 700; color: #202124;")
        subtitle = QLabel("논문 PDF를 끌어다 놓거나 열면, 같은 레이아웃의 한국어 직역이 옆에 나타납니다.")
        subtitle.setStyleSheet("color: #5f6368; font-size: 11pt;")
        button = QPushButton("PDF 열기…")
        button.setObjectName("primary")
        button.clicked.connect(window.choose_pdf)
        self.recent = QListWidget()
        self.recent.setMaximumWidth(620)
        self.recent.itemActivated.connect(lambda item: window.open_project(Path(item.data(Qt.ItemDataRole.UserRole))))
        self.recent.itemClicked.connect(lambda item: window.open_project(Path(item.data(Qt.ItemDataRole.UserRole))))
        recent_label = QLabel("최근 논문")
        recent_label.setObjectName("sectionTitle")

        layout = QVBoxLayout(self)
        layout.addStretch(2)
        for widget in (title, subtitle):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(26)
        layout.addWidget(recent_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.recent, 3, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.window = window

    def refresh(self, settings: Settings) -> None:
        self.recent.clear()
        for root in settings.recent_projects():
            try:
                title = Project(root).title
            except FileNotFoundError:
                continue
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, str(root))
            item.setToolTip(str(root))
            self.recent.addItem(item)
        if not self.recent.count():
            self.recent.addItem("아직 연 논문이 없습니다.")
            self.recent.item(0).setFlags(Qt.ItemFlag.NoItemFlags)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.settings = settings
        self.codex = QLineEdit(settings.get("codex_path", "") or "")
        self.codex.setPlaceholderText("비워두면 자동으로 찾습니다")
        self.model = QLineEdit(settings.get("translate_model", "") or "")
        self.model.setPlaceholderText("비워두면 Codex 기본 모델")
        self.parallel = QSpinBox(minimum=1, maximum=6, value=int(settings.get("parallel", 3)))
        self.auto = QCheckBox("논문을 열면 자동으로 번역 시작")
        self.auto.setChecked(bool(settings.get("auto_translate", True)))
        self.consent = QCheckBox("번역·사전에 GPT(Codex) 사용 — 논문 텍스트가 OpenAI로 전송됩니다")
        self.consent.setChecked(settings.translation_enabled)
        self.obsidian = QLineEdit(settings.get("obsidian_vocab_dir", "") or "")
        self.obsidian.setPlaceholderText("dictionary / dictionary_ko 폴더가 있는 곳 (처음 등록할 때 물어봅니다)")
        form = QFormLayout(self)
        form.addRow("Codex 경로", self.codex)
        form.addRow("모델", self.model)
        form.addRow("동시 요청 수", self.parallel)
        form.addRow("Obsidian 단어장", self.obsidian)
        form.addRow(self.auto)
        form.addRow(self.consent)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        self.settings.set("codex_path", self.codex.text().strip())
        self.settings.set("translate_model", self.model.text().strip())
        self.settings.set("parallel", self.parallel.value())
        self.settings.set("auto_translate", self.auto.isChecked())
        self.settings.set("obsidian_vocab_dir", self.obsidian.text().strip())
        self.settings.translation_enabled = self.consent.isChecked()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.dictionary = Dictionary(dictionary_cache())
        self.fonts = FontBook()
        self.session: PaperSession | None = None
        self.reader: ReaderView | None = None
        self._open_task: BackgroundTask | None = None
        self._translate_task: BackgroundTask | None = None
        self._export_task: BackgroundTask | None = None
        self._engine: TranslationEngine | None = None

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setAcceptDrops(True)
        self.resize(1600, 980)
        geometry = settings.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))

        self.stack = QStackedWidget()
        self.start = StartView(self)
        self.stack.addWidget(self.start)
        self.setCentralWidget(self.stack)
        self._build_toolbar()
        self._set_reading(False)
        self.start.refresh(settings)

    # -- chrome -------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("main")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        open_action = QAction("열기", self, shortcut=QKeySequence.StandardKey.Open, triggered=self.choose_pdf)
        home = QAction("처음 화면", self, triggered=self.close_paper)
        self.translate_action = QAction("번역 시작", self, shortcut="Ctrl+T", triggered=self.toggle_translation)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(170)
        self.progress.setFormat("%v / %m 문장")
        self.progress.hide()
        self.page_label = QLabel()
        self.page_label.setStyleSheet("padding: 0 8px; color:#444;")
        zoom_out = QAction("－", self, triggered=lambda: self.reader and self.reader.zoom_step(-1))
        zoom_in = QAction("＋", self, triggered=lambda: self.reader and self.reader.zoom_step(1))
        fit = QAction("폭 맞춤", self, triggered=lambda: self.reader and self.reader.fit_width())
        side = QAction("사전·메모 패널", self, shortcut="Ctrl+B", triggered=lambda: self.reader and self.reader.toggle_side())

        export_menu = QMenu(self)
        export_menu.addAction("번역본 PDF…", lambda: self.export_pdf(side_by_side=False))
        export_menu.addAction("원문·번역 나란히 PDF…", lambda: self.export_pdf(side_by_side=True))
        export_menu.addAction("메모·단어장 Markdown…", self.export_notes)
        export_menu.addSeparator()
        export_menu.addAction("프로젝트 폴더 열기", self.open_folder)
        export_button = QToolButton()
        export_button.setText("내보내기")
        export_button.setMenu(export_menu)
        export_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        settings_action = QAction("설정", self, triggered=self.open_settings)

        # The highlighter: dragging across text paints it with this colour.
        self.pen_button = QToolButton()
        self.pen_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.pen_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        pen_menu = QMenu(self)
        for name, color in HIGHLIGHT_COLORS.items():
            pen_menu.addAction(_swatch(color), name, lambda c=color: self.set_pen(c))
        pen_menu.addSeparator()
        pen_menu.addAction("끄기 (드래그해도 칠하지 않음)", lambda: self.set_pen(""))
        self.pen_button.setMenu(pen_menu)

        for item in (open_action, home):
            bar.addAction(item)
        bar.addSeparator()
        bar.addAction(self.translate_action)
        bar.addWidget(self.progress)
        bar.addSeparator()
        for item in (zoom_out, zoom_in, fit):
            bar.addAction(item)
        bar.addWidget(self.page_label)
        bar.addSeparator()
        bar.addAction(side)
        self._pen_widget = bar.addWidget(self.pen_button)
        self._export_button = bar.addWidget(export_button)
        bar.addAction(settings_action)
        self._reading_actions = [home, self.translate_action, zoom_out, zoom_in, fit, side]

    def _set_reading(self, reading: bool) -> None:
        for action in self._reading_actions:
            action.setVisible(reading)
        self._export_button.setVisible(reading)
        self._pen_widget.setVisible(reading)
        self.page_label.setVisible(reading)
        if not reading:
            self.progress.hide()

    def _status(self, text: str, timeout: int = 4000) -> None:
        self.statusBar().showMessage(text, timeout)

    # -- opening -------------------------------------------------------------------

    def choose_pdf(self) -> None:
        start = self.settings.get("last_dir") or str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(self, "논문 PDF 열기", start, "PDF (*.pdf)")
        if path:
            self.settings.set("last_dir", str(Path(path).parent))
            self.open_pdf(Path(path))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if any(url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf":
                self.open_pdf(path)
                return

    def open_pdf(self, pdf: Path) -> None:
        self._begin_open(_prepare(pdf, None, self.settings.projects_dir))

    def open_project(self, root: Path) -> None:
        self._begin_open(_prepare(None, root, self.settings.projects_dir))

    def _begin_open(self, work) -> None:  # type: ignore[no-untyped-def]
        if self._open_task is not None and self._open_task.running:
            return
        self.close_paper()
        self._status("논문을 여는 중…", 0)
        task = BackgroundTask(work, name="open")
        task.progress.connect(lambda message: self._status(str(message), 0))
        task.succeeded.connect(self._opened)
        task.failed.connect(self._open_failed)
        self._open_task = task.start()

    def _open_failed(self, exc: Exception) -> None:
        self.statusBar().clearMessage()
        message = str(exc) if isinstance(exc, (PaperOpenError, FileNotFoundError)) else f"논문을 열지 못했습니다.\n\n{exc}"
        QMessageBox.warning(self, APP_DISPLAY_NAME, message)

    def _opened(self, result: tuple[Project, PaperLayout]) -> None:
        project, layout = result
        try:
            document = PaperDocument(project.pdf_path)
        except PaperOpenError as exc:
            self._open_failed(exc)
            return
        self.session = PaperSession(project, document, layout, self.fonts)
        self.reader = ReaderView(self.session, self.settings, self.dictionary, self.engine)
        self.reader.statusMessage.connect(self._status)
        self.reader.pageChanged.connect(self._show_page)
        self.stack.addWidget(self.reader)
        self.stack.setCurrentWidget(self.reader)
        self._set_reading(True)
        self.set_pen(self.settings.get("pen_color", "#ffd400"))
        self._show_page(0)
        self.setWindowTitle(f"{project.title} — {APP_DISPLAY_NAME}")
        self.settings.add_recent_project(project.root)
        total = layout.sentence_count()
        done = sum(1 for _, _, s in layout.sentences() if s.id in project.translations)
        self._status(f"{layout.page_count}쪽 · 번역 {done}/{total} 문장", 6000)
        if done < total and self.settings.get("auto_translate", True):
            self.start_translation(ask=False)

    def set_pen(self, color: str) -> None:
        self.settings.set("pen_color", color)
        names = {c: n for n, c in HIGHLIGHT_COLORS.items()}
        if color:
            self.pen_button.setIcon(_swatch(color))
            self.pen_button.setText(f"형광펜: {names.get(color, '')}")
        else:
            self.pen_button.setIcon(QIcon())
            self.pen_button.setText("형광펜: 끔")
        if self.reader is not None:
            self.reader.set_pen(color)

    def _show_page(self, number: int) -> None:
        if self.session is not None:
            self.page_label.setText(f"{number + 1} / {self.session.page_count} 쪽")

    def close_paper(self) -> None:
        self.stop_translation()
        if self.reader is not None:
            self.reader.shutdown()
            self.stack.removeWidget(self.reader)
            self.reader.deleteLater()
            self.reader = None
        if self.session is not None:
            self.session.close()
            self.session = None
        self._set_reading(False)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.start.refresh(self.settings)
        self.stack.setCurrentWidget(self.start)

    # -- engine and consent (§5.3) ------------------------------------------------

    def engine(self, *, ask: bool = True) -> TranslationEngine | None:
        if not self.settings.translation_enabled:
            if not ask:
                return None
            answer = QMessageBox.question(
                self,
                "GPT 사용 동의",
                "번역과 사전은 이 컴퓨터의 Codex CLI(ChatGPT 계정 로그인)를 통해 GPT를 사용합니다.\n\n"
                "• 논문의 <텍스트>가 OpenAI로 전송됩니다.\n"
                "• PDF 파일, 그림, 메모는 전송되지 않습니다.\n"
                "• API 키나 별도 요금은 필요 없고, ChatGPT 사용량이 차감됩니다.\n\n"
                "사용하시겠습니까? (설정에서 언제든 끌 수 있습니다)",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return None
            self.settings.translation_enabled = True
        if self._engine is None:
            # Checked once: finding Codex starts a process, and a dictionary
            # click should not pay for that every time.
            engine = engine_from_settings(self.settings)
            availability = engine.availability()
            if not availability.ready:
                QMessageBox.warning(self, "Codex를 찾을 수 없음", availability.remedy or availability.detail)
                return None
            self._engine = engine
        return self._engine

    # -- translation ---------------------------------------------------------------

    def toggle_translation(self) -> None:
        if self._translate_task is not None and self._translate_task.running:
            self.stop_translation()
        else:
            self.start_translation(ask=True)

    def start_translation(self, *, ask: bool) -> None:
        if self.session is None or (self._translate_task and self._translate_task.running):
            return
        engine = self.engine(ask=ask or not self.settings.translation_enabled)
        if engine is None:
            return
        session = self.session
        start_page = self.reader.current_page if self.reader else 0
        parallel = int(self.settings.get("parallel", 3))

        def work(cancel: threading.Event, report) -> Progress:  # type: ignore[no-untyped-def]
            return translate_paper(
                session.layout, session.project.translations, engine,
                title=session.project.title, start_page=start_page,
                parallel=parallel, cancel=cancel, on_progress=report,
            )

        task = BackgroundTask(work, name="translate")
        task.progress.connect(self._translation_progress)
        task.succeeded.connect(lambda p: self._status(p.message, 8000))
        task.failed.connect(self._translation_failed)
        task.done.connect(self._translation_done)
        self._translate_task = task.start()
        self.translate_action.setText("번역 중지")
        self.progress.show()

    def _translation_progress(self, progress: Progress) -> None:
        self.progress.setMaximum(max(1, progress.total))
        self.progress.setValue(progress.done)
        if progress.page is not None and self.session is not None:
            self.session.invalidate(progress.page)
        if progress.message:
            self._status(progress.message)

    def _translation_failed(self, exc: Exception) -> None:
        if isinstance(exc, TranslationCancelled):
            self._status("번역을 멈췄습니다. 다시 시작하면 이어서 번역합니다.")
            return
        title = "사용량 한도" if isinstance(exc, EngineRateLimited) else "번역 실패"
        QMessageBox.warning(self, title, str(exc))

    def _translation_done(self) -> None:
        self.translate_action.setText("번역 시작")
        self.progress.hide()
        if self.session is not None:
            self.session.invalidate(-1)

    def stop_translation(self) -> None:
        if self._translate_task is not None and self._translate_task.running:
            self._translate_task.cancel()

    # -- export -------------------------------------------------------------------

    def export_pdf(self, *, side_by_side: bool) -> None:
        if self.session is None:
            return
        project, layout = self.session.project, self.session.layout
        name = "translated_side_by_side.pdf" if side_by_side else "translated.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "번역본 PDF 저장", str(project.exports_dir / name), "PDF (*.pdf)")
        if not path:
            return

        def work(cancel: threading.Event, report) -> Path:  # type: ignore[no-untyped-def]
            return translated_pdf(project, layout, Path(path), side_by_side=side_by_side, cancel=cancel, report=report)

        task = BackgroundTask(work, name="export")
        task.progress.connect(lambda p: self._status(f"PDF 만드는 중… {p[0]}/{p[1]}쪽", 0))
        task.succeeded.connect(lambda target: self._exported(target))
        task.failed.connect(lambda exc: QMessageBox.warning(self, "내보내기 실패", str(exc)))
        self._export_task = task.start()

    def _exported(self, target: Path) -> None:
        self._status(f"저장했습니다: {target}", 8000)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def export_notes(self) -> None:
        if self.session is None:
            return
        if self.reader is not None:
            self.reader.notes_panel.save_memo()
        project = self.session.project
        path, _ = QFileDialog.getSaveFileName(self, "메모 내보내기", str(project.exports_dir / "notes.md"), "Markdown (*.md)")
        if path:
            write_notes_markdown(project, self.session.layout, Path(path))
            self._status(f"저장했습니다: {path}", 6000)

    def open_folder(self) -> None:
        if self.session is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.project.root)))

    def open_settings(self) -> None:
        if SettingsDialog(self.settings, self).exec():
            self._engine = None

    # -- closing -----------------------------------------------------------------

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        self.settings.set("geometry", bytes(self.saveGeometry().toBase64()).decode())
        self.close_paper()
        super().closeEvent(event)
