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
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_DISPLAY_NAME, Settings, dictionary_cache
from ..core.library import Library, LibraryEntry
from ..core.models import PaperLayout
from ..core.project import HIGHLIGHT_COLORS, Project
from ..pdf.document import PaperDocument, PaperOpenError
from ..pdf.extract import extract_paper
from ..render.export import translated_pdf, write_notes_markdown
from ..render.fonts import FontBook
from ..translate import RECOMMENDED_MODELS, engine_from_settings
from ..translate.codex_cli import available_models
from ..translate.dictionary import Dictionary
from ..translate.engine import EngineRateLimited, TranslationCancelled, TranslationEngine
from ..translate.runner import Progress, translate_paper
from .keys import CAPTURE_KEY, CAPTURE_LABEL
from .note_panel import NotionDialog
from .quiz_panel import PDF_LAYOUTS
from .reader_view import ReaderView
from .session import PaperSession
from .workers import BackgroundTask

log = logging.getLogger(__name__)

__all__ = ["MainWindow"]


# -- opening a paper, off the UI thread ------------------------------------------


def _prepare(pdf: Path | None, root: Path | None, projects_dir: Path, folder: str = ""):  # type: ignore[no-untyped-def]
    def work(_cancel: threading.Event, report) -> tuple[Project, PaperLayout]:  # type: ignore[no-untyped-def]
        if root is not None:
            project = Project(root)
        else:
            assert pdf is not None
            report("논문을 여는 중…")
            with PaperDocument(pdf) as document:
                title = document.title()
            project = Project.find_or_create(pdf, projects_dir, title)
            if folder:
                Library(projects_dir).set_folder(project.root, folder)
                project = Project(project.root)
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


#: Tree item roles: what an item is ("folder" / "paper"), and its folder or path.
_KIND = Qt.ItemDataRole.UserRole
_VALUE = Qt.ItemDataRole.UserRole + 1
#: The group of papers in no folder.
NO_FOLDER = "폴더 없음"


class _LibraryTree(QTreeWidget):
    """The papers by folder; a paper is dragged onto a folder to file it,
    and a PDF dropped on a folder is opened into it."""

    def __init__(self, view: StartView) -> None:
        super().__init__()
        self.view = view
        self.setHeaderHidden(True)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def _folder_at(self, event) -> str | None:  # type: ignore[no-untyped-def]
        item = self.itemAt(event.position().toPoint())
        if item is None:
            return None
        if item.data(0, _KIND) == "paper":
            item = item.parent()
        if item is None or item.data(0, _KIND) != "folder":
            return None
        return str(item.data(0, _VALUE))

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        if event.source() is self or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        if self._folder_at(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def] # noqa: N802
        folder = self._folder_at(event)
        # The tree is redrawn from the library; Qt's own move is not wanted.
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if folder is None:
            return
        if event.source() is self:
            roots = [Path(i.data(0, _VALUE)) for i in self.selectedItems() if i.data(0, _KIND) == "paper"]
            self.view.move_papers(roots, folder)
            return
        pdfs = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.toLocalFile().lower().endswith(".pdf")]
        if pdfs:
            self.view.window.open_pdf(pdfs[0], folder=folder)


class StartView(QWidget):
    """The papers, by folder, and the way to open another."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__()
        self.window = window
        title = QLabel(APP_DISPLAY_NAME)
        title.setStyleSheet("font-size: 26pt; font-weight: 700; color: #202124;")
        subtitle = QLabel("논문 PDF를 끌어다 놓거나 열면, 같은 레이아웃의 한국어 직역이 옆에 나타납니다.")
        subtitle.setStyleSheet("color: #5f6368; font-size: 11pt;")
        button = QPushButton("PDF 열기…")
        button.setObjectName("primary")
        button.setToolTip("왼쪽 목록에서 폴더를 골라 두면 그 폴더에 넣어 엽니다.")
        button.clicked.connect(lambda: window.choose_pdf(folder=self.selected_folder()))
        #: Back to the papers still open in tabs, when there are any.
        self.back = QPushButton("← 열려 있는 논문으로")
        self.back.clicked.connect(window.show_tabs)

        self.search = QLineEdit()
        self.search.setPlaceholderText("논문 제목 검색")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self._fill())
        new_folder = QPushButton("+ 새 폴더")
        new_folder.clicked.connect(lambda: self.new_folder())
        tools = QHBoxLayout()
        tools.addWidget(self.search, 1)
        tools.addWidget(new_folder)

        self.tree = _LibraryTree(self)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemActivated.connect(self._open_item)
        self.tree.itemClicked.connect(self._open_item)
        self.tree.itemExpanded.connect(lambda item: self._remember_expanded(item, True))
        self.tree.itemCollapsed.connect(lambda item: self._remember_expanded(item, False))
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        library_label = QLabel("내 논문")
        library_label.setObjectName("sectionTitle")
        hint = QLabel("논문을 폴더로 끌어다 놓거나 우클릭 → 폴더로 이동. PDF를 폴더 위에 놓으면 그 폴더로 열립니다.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)

        library = QWidget()
        library.setMaximumWidth(640)
        library.setMinimumWidth(420)
        library_layout = QVBoxLayout(library)
        library_layout.setContentsMargins(0, 0, 0, 0)
        library_layout.addWidget(library_label)
        library_layout.addLayout(tools)
        library_layout.addWidget(self.tree, 1)
        library_layout.addWidget(hint)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        for widget in (title, subtitle):
            layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(self.back, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(18)
        layout.addWidget(library, 6, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self._entries: list[LibraryEntry] = []
        self._folders: list[str] = []

    @property
    def library(self) -> Library:
        return Library(self.window.settings.projects_dir)

    def refresh(self, settings: Settings) -> None:
        self.back.setVisible(bool(self.window.readers()))
        library = self.library
        self._entries = library.entries(settings.recent_projects())
        self._folders = list(library.folders)
        self._fill()

    def _fill(self) -> None:
        settings = self.window.settings
        # Within a folder, the papers opened lately come first.
        recent = {p.resolve(): i for i, p in enumerate(settings.recent_projects())}
        entries = sorted(self._entries, key=lambda e: (recent.get(e.root, 1 << 30), e.title.lower()))
        query = self.search.text().strip().lower()
        if query:
            entries = [e for e in entries if query in e.title.lower()]
        collapsed = set(settings.get("collapsed_folders", []) or [])

        self.tree.clear()
        folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        for folder in [*self._folders, ""]:
            papers = [e for e in entries if e.folder == folder]
            if not papers and (query or not folder):
                continue
            item = QTreeWidgetItem([f"{folder or NO_FOLDER}  ({len(papers)})"])
            item.setIcon(0, folder_icon)
            item.setData(0, _KIND, "folder")
            item.setData(0, _VALUE, folder)
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
            self.tree.addTopLevelItem(item)
            for entry in papers:
                child = QTreeWidgetItem([entry.title])
                child.setData(0, _KIND, "paper")
                child.setData(0, _VALUE, str(entry.root))
                child.setToolTip(0, f"{entry.title}\n{entry.root}")
                child.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                )
                item.addChild(child)
            item.setExpanded(bool(query) or folder not in collapsed)
        if not self.tree.topLevelItemCount():
            empty = QTreeWidgetItem(["검색 결과가 없습니다." if query else "아직 연 논문이 없습니다."])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)

    def _remember_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if item.data(0, _KIND) != "folder" or self.search.text().strip():
            return
        folder = str(item.data(0, _VALUE))
        collapsed = set(self.window.settings.get("collapsed_folders", []) or [])
        if (folder not in collapsed) == expanded:
            return
        if expanded:
            collapsed.discard(folder)
        else:
            collapsed.add(folder)
        self.window.settings.set("collapsed_folders", sorted(collapsed))

    def selected_folder(self) -> str:
        """The folder picked in the tree, where a newly opened PDF goes."""
        item = self.tree.currentItem()
        if item is not None and item.isSelected() and item.data(0, _KIND) == "folder":
            return str(item.data(0, _VALUE))
        return ""

    def _open_item(self, item: QTreeWidgetItem) -> None:
        if item.data(0, _KIND) == "paper" and len(self.tree.selectedItems()) <= 1:
            self.window.open_project(Path(item.data(0, _VALUE)))

    # -- folders -------------------------------------------------------------------

    def _reload(self) -> None:
        self.refresh(self.window.settings)

    def new_folder(self, papers: list[Path] | None = None) -> str:
        name, ok = QInputDialog.getText(self, "새 폴더", "폴더 이름:")
        name = name.strip() if ok else ""
        if not name:
            return ""
        library = self.library
        name = library.add_folder(name)
        for root in papers or []:
            library.set_folder(root, name)
        self._reload()
        return name

    def move_papers(self, roots: list[Path], folder: str) -> None:
        library = self.library
        for root in roots:
            try:
                library.set_folder(root, folder)
            except FileNotFoundError:
                continue
        self._reload()

    def _menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        kind = item.data(0, _KIND) if item is not None else None
        if item is not None and not item.isSelected():
            self.tree.setCurrentItem(item)
        if kind == "paper":
            roots = [Path(i.data(0, _VALUE)) for i in self.tree.selectedItems() if i.data(0, _KIND) == "paper"]
            current = str(item.parent().data(0, _VALUE)) if item.parent() is not None else ""
            root = Path(item.data(0, _VALUE))
            menu.addAction("열기", lambda: self.window.open_project(root))
            move = menu.addMenu(f"폴더로 이동 ({len(roots)}개)" if len(roots) > 1 else "폴더로 이동")
            for name in self._folders:
                action = move.addAction(name, lambda n=name: self.move_papers(roots, n))
                action.setCheckable(True)
                action.setChecked(name == current and len(roots) == 1)
            if self._folders:
                move.addSeparator()
            move.addAction("새 폴더…", lambda: self.new_folder(roots))
            move.addAction("폴더에서 빼기", lambda: self.move_papers(roots, ""))
            menu.addSeparator()
            menu.addAction("탐색기에서 보기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(root))))
        elif kind == "folder":
            folder = str(item.data(0, _VALUE))
            menu.addAction("이 폴더에 PDF 열기…", lambda: self.window.choose_pdf(folder=folder))
            if folder:
                index = self._folders.index(folder) if folder in self._folders else -1
                menu.addSeparator()
                menu.addAction("이름 바꾸기…", lambda: self._rename(folder))
                up = menu.addAction("위로", lambda: self._shift(folder, index - 1))
                up.setEnabled(index > 0)
                down = menu.addAction("아래로", lambda: self._shift(folder, index + 1))
                down.setEnabled(0 <= index < len(self._folders) - 1)
                menu.addSeparator()
                menu.addAction("폴더 삭제 (논문은 남음)", lambda: self._remove(folder))
            menu.addAction("새 폴더…", lambda: self.new_folder())
        else:
            menu.addAction("새 폴더…", lambda: self.new_folder())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _rename(self, folder: str) -> None:
        name, ok = QInputDialog.getText(self, "폴더 이름 바꾸기", "새 이름:", text=folder)
        if not ok or not name.strip():
            return
        settings = self.window.settings
        new = self.library.rename_folder(folder, name, settings.recent_projects())
        collapsed = set(settings.get("collapsed_folders", []) or [])
        if folder in collapsed:
            settings.set("collapsed_folders", sorted((collapsed - {folder}) | {new}))
        self._reload()

    def _shift(self, folder: str, index: int) -> None:
        self.library.move_folder(folder, index)
        self._reload()

    def _remove(self, folder: str) -> None:
        answer = QMessageBox.question(
            self,
            "폴더 삭제",
            f"'{folder}' 폴더를 지울까요?\n안에 있던 논문은 지워지지 않고 '{NO_FOLDER}'으로 옮겨집니다.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.library.remove_folder(folder, self.window.settings.recent_projects())
            self._reload()


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.settings = settings
        self.codex = QLineEdit(settings.get("codex_path", "") or "")
        self.codex.setPlaceholderText("비워두면 자동으로 찾습니다")
        # Two models: translation and look-ups are many small runs, where a
        # light model saves most of the ChatGPT usage; questions need more.
        self.model = _model_box(settings.get("translate_model", RECOMMENDED_MODELS["translate_model"]) or "")
        self.model.setToolTip("번역과 사전 검색에 쓰는 모델. 실행 횟수가 많아 가벼운 모델(예: gpt-6-luna)이 사용량을 크게 아낍니다.")
        self.chat_model = _model_box(settings.get("chat_model", RECOMMENDED_MODELS["chat_model"]) or "")
        self.chat_model.setToolTip("GPT 탭 질문과 퀴즈 만들기에 쓰는 모델.")
        self.parallel = QSpinBox(minimum=1, maximum=6, value=int(settings.get("parallel", 3)))
        self.auto = QCheckBox("논문을 열면 자동으로 번역 시작")
        self.auto.setChecked(bool(settings.get("auto_translate", True)))
        # Read-ahead: translate the page in view and a few after it, following
        # the reader, instead of the whole paper at once. 0 means whole paper.
        ahead = int(settings.get("translate_ahead", 0) or 0)
        self.ahead = QCheckBox("읽는 곳 근처만 번역 (사용량 절약 — 안 읽는 부록·뒷부분은 번역하지 않음)")
        self.ahead.setChecked(ahead > 0)
        self.ahead.setToolTip("보고 있는 쪽과 그 뒤 몇 쪽만 먼저 번역하고, 넘기는 대로 이어서 번역합니다.\n"
                              "툴바의 '번역 시작'(Ctrl+T)을 누르면 언제든 전체를 번역합니다.")
        self.ahead_pages = QSpinBox(minimum=1, maximum=10, value=ahead or 3, suffix="쪽 앞까지")
        self.ahead_pages.setEnabled(ahead > 0)
        self.ahead.toggled.connect(self.ahead_pages.setEnabled)
        self.consent =QCheckBox("번역·사전·질문·퀴즈에 GPT(Codex) 사용 — 논문·강의 텍스트와 질문에 첨부한 캡처가 OpenAI로 전송됩니다")
        self.consent.setChecked(settings.translation_enabled)
        self.obsidian = QLineEdit(settings.get("obsidian_vocab_dir", "") or "")
        self.obsidian.setPlaceholderText("dictionary / dictionary_ko 폴더가 있는 곳 (처음 등록할 때 물어봅니다)")
        form = QFormLayout(self)
        form.addRow("Codex 경로", self.codex)
        form.addRow("번역·사전 모델", self.model)
        form.addRow("질문·퀴즈 모델", self.chat_model)
        form.addRow("동시 요청 수", self.parallel)
        form.addRow("Obsidian 단어장", self.obsidian)
        notion = QPushButton("Notion 연결…" + (" (연결됨)" if settings.get("notion_token") else ""))
        notion.clicked.connect(lambda: NotionDialog(settings, self).exec())
        form.addRow("노트 → Notion", notion)
        form.addRow(self.auto)
        form.addRow(self.ahead)
        form.addRow("    미리 번역할 범위", self.ahead_pages)
        form.addRow(self.consent)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self) -> None:
        self.settings.set("codex_path", self.codex.text().strip())
        self.settings.set("translate_model", _model_of(self.model))
        self.settings.set("chat_model", _model_of(self.chat_model))
        self.settings.set("parallel", self.parallel.value())
        self.settings.set("auto_translate", self.auto.isChecked())
        self.settings.set("translate_ahead", self.ahead_pages.value() if self.ahead.isChecked() else 0)
        self.settings.set("obsidian_vocab_dir", self.obsidian.text().strip())
        self.settings.translation_enabled = self.consent.isChecked()
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.dictionary = Dictionary(dictionary_cache())
        self.fonts = FontBook()
        self._open_task: BackgroundTask | None = None
        #: Each open paper's translation, and how far it has come.
        self._translations: dict[ReaderView, BackgroundTask] = {}
        self._progress: dict[ReaderView, Progress] = {}
        #: Read-ahead: pages already asked for in each tab, and tabs where it
        #: waits for "번역 시작" (the reader stopped it, or it failed).
        self._ahead_tried: dict[ReaderView, set[int]] = {}
        self._ahead_paused: set[ReaderView] = set()
        #: Tabs where the whole paper is to be translated once the run under
        #: way (a read-ahead one) finishes.
        self._full_next: set[ReaderView] = set()
        self._export_task: BackgroundTask | None = None
        self._engine: TranslationEngine | None = None

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setAcceptDrops(True)
        self.resize(1600, 980)
        geometry = settings.get("geometry")
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))

        self.stack = QStackedWidget()
        # Papers open side by side as tabs, like a browser's.
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabCloseRequested.connect(lambda index: self.close_tab(self.tabs.widget(index)))
        self.tabs.currentChanged.connect(self._tab_changed)
        self.start = StartView(self)
        self.stack.addWidget(self.start)
        self.stack.addWidget(self.tabs)
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
        home = QAction("처음 화면", self, triggered=self.show_start)
        home.setToolTip("다른 논문을 새 탭으로 엽니다. 열린 논문은 탭에 그대로 있습니다.")
        close_tab = QAction("탭 닫기", self, shortcut=QKeySequence.StandardKey.Close, triggered=self.close_paper)
        self.addAction(close_tab)
        next_tab = QAction("다음 탭", self, shortcut="Ctrl+Tab", triggered=lambda: self._step_tab(1))
        previous_tab = QAction("이전 탭", self, shortcut="Ctrl+Shift+Tab", triggered=lambda: self._step_tab(-1))
        self.addActions([next_tab, previous_tab])
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
        side = QAction("옆 패널", self, shortcut="Ctrl+B", triggered=lambda: self.reader and self.reader.toggle_side())
        note = QAction("노트", self, shortcut="Ctrl+J", triggered=lambda: self.reader and self.reader.toggle_note())
        note.setToolTip("아래 노트 패널을 열고 닫습니다 (Ctrl+J)")
        self.capture_action = QAction("✂ 캡처", self, shortcut=CAPTURE_KEY, checkable=True)
        self.capture_action.setToolTip(f"영역을 잘라 GPT에게 묻거나 노트에 붙입니다 ({CAPTURE_LABEL})")
        self.capture_action.triggered.connect(lambda: self.reader and self.reader.toggle_capture())

        export_menu = QMenu(self)
        export_menu.addAction("번역본 PDF…", lambda: self.export_pdf(side_by_side=False))
        export_menu.addAction("원문·번역 나란히 PDF…", lambda: self.export_pdf(side_by_side=True))
        export_menu.addAction("메모·단어장 Markdown…", self.export_notes)
        export_menu.addSeparator()
        for layout_name, text in PDF_LAYOUTS.items():
            export_menu.addAction(
                f"퀴즈 PDF: {text}", lambda name=layout_name: self.reader and self.reader.quiz_panel.export_pdf(name)
            )
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
        bar.addAction(note)
        bar.addAction(self.capture_action)
        self._pen_widget = bar.addWidget(self.pen_button)
        self._export_button = bar.addWidget(export_button)
        bar.addAction(settings_action)
        self._reading_actions = [home, self.translate_action, zoom_out, zoom_in, fit, side, note, self.capture_action]

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

    def choose_pdf(self, *, folder: str = "") -> None:
        start = self.settings.get("last_dir") or str(Path.home() / "Downloads")
        title = f"논문 PDF 열기 → {folder}" if folder else "논문 PDF 열기"
        path, _ = QFileDialog.getOpenFileName(self, title, start, "PDF (*.pdf)")
        if path:
            self.settings.set("last_dir", str(Path(path).parent))
            self.open_pdf(Path(path), folder=folder)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if any(url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.suffix.lower() == ".pdf":
                self.open_pdf(path)
                return

    def open_pdf(self, pdf: Path, *, folder: str = "") -> None:
        """Open a PDF; with ``folder``, file the paper there as well."""
        self._begin_open(_prepare(pdf, None, self.settings.projects_dir, folder))

    def open_project(self, root: Path) -> None:
        if self._activate_open(root):
            return
        self._begin_open(_prepare(None, root, self.settings.projects_dir))

    def _activate_open(self, root: Path) -> bool:
        """Switch to the tab a paper is already open in, if it is."""
        for reader in self.readers():
            if reader.session.project.root.resolve() == Path(root).resolve():
                self.activate(reader)
                return True
        return False

    def _begin_open(self, work) -> None:  # type: ignore[no-untyped-def]
        if self._open_task is not None and self._open_task.running:
            return
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
        # A PDF dropped again is the paper already open: show that tab.
        if self._activate_open(project.root):
            self.statusBar().clearMessage()
            return
        try:
            document = PaperDocument(project.pdf_path)
        except PaperOpenError as exc:
            self._open_failed(exc)
            return
        session = PaperSession(project, document, layout, self.fonts)
        reader = ReaderView(session, self.settings, self.dictionary, self.engine)
        reader.peers = lambda r=reader: [other for other in self.readers() if other is not r]
        reader.show_peer = self.activate
        reader.statusMessage.connect(self._status)
        reader.pageChanged.connect(lambda number, r=reader: r is self.reader and self._show_page(number))
        reader.pageChanged.connect(lambda _number, r=reader: self._read_ahead(r))
        reader.captureModeChanged.connect(lambda on, r=reader: r is self.reader and self.capture_action.setChecked(on))
        reader.evidenceLit.connect(lambda count, r=reader: self._mark_tab(r, count))
        reader.set_pen(self.settings.get("pen_color", "#ffd400"))
        index = self.tabs.addTab(reader, _tab_title(project.title))
        self.tabs.setTabToolTip(index, project.title)
        self.tabs.setCurrentIndex(index)
        self.show_tabs()
        self.settings.add_recent_project(project.root)
        total = layout.sentence_count()
        done = sum(1 for _, _, s in layout.sentences() if project.translations.settled(s.id))
        self._status(f"{layout.page_count}쪽 · 번역 {done}/{total} 문장", 6000)
        if done < total and self.settings.get("auto_translate", True):
            if self._ahead_pages():
                self._read_ahead(reader, ask=True)
            else:
                self.start_translation(ask=False)

    # -- tabs --------------------------------------------------------------------------

    @property
    def reader(self) -> ReaderView | None:
        """The paper in the current tab."""
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, ReaderView) else None

    @property
    def session(self) -> PaperSession | None:
        return self.reader.session if self.reader is not None else None

    def readers(self) -> list[ReaderView]:
        return [w for i in range(self.tabs.count()) if isinstance(w := self.tabs.widget(i), ReaderView)]

    def activate(self, reader: ReaderView) -> None:
        self.tabs.setCurrentWidget(reader)
        self.show_tabs()

    def _step_tab(self, step: int) -> None:
        if self.tabs.count() > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % self.tabs.count())

    def show_tabs(self) -> None:
        if not self.readers():
            self.show_start()
            return
        self.stack.setCurrentWidget(self.tabs)
        self._set_reading(True)
        self._tab_changed(self.tabs.currentIndex())

    def show_start(self) -> None:
        if self.reader is not None:
            self.reader.cancel_capture()
        self._set_reading(False)
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.start.refresh(self.settings)
        self.stack.setCurrentWidget(self.start)

    def _tab_changed(self, _index: int) -> None:
        reader = self.reader
        for other in self.readers():
            if other is not reader:
                other.cancel_capture()
        if reader is None:
            return
        self._mark_tab(reader, 0)
        self.capture_action.setChecked(False)
        self.setWindowTitle(f"{reader.session.project.title} — {APP_DISPLAY_NAME}")
        self._show_page(reader.current_page)
        running = reader in self._translations and self._translations[reader].running
        self.translate_action.setText("번역 중지" if running else "번역 시작")
        self.progress.setVisible(running)
        if running and reader in self._progress:
            self._show_progress(self._progress[reader])

    def _mark_tab(self, reader: ReaderView, count: int) -> None:
        """A ◆ on a tab that holds sentences an answer just cited."""
        index = self.tabs.indexOf(reader)
        if index < 0:
            return
        title = _tab_title(reader.session.project.title)
        marked = count > 0 and reader is not self.reader
        self.tabs.setTabText(index, f"◆ {title}" if marked else title)

    def close_tab(self, reader: QWidget | None) -> None:
        if not isinstance(reader, ReaderView):
            return
        task = self._translations.pop(reader, None)
        if task is not None and task.running:
            task.cancel()
        self._progress.pop(reader, None)
        self._ahead_tried.pop(reader, None)
        self._ahead_paused.discard(reader)
        self._full_next.discard(reader)
        reader.shutdown()
        self.tabs.removeTab(self.tabs.indexOf(reader))
        reader.session.close()
        reader.deleteLater()
        if not self.readers():
            self.show_start()

    def set_pen(self, color: str) -> None:
        self.settings.set("pen_color", color)
        names = {c: n for n, c in HIGHLIGHT_COLORS.items()}
        if color:
            self.pen_button.setIcon(_swatch(color))
            self.pen_button.setText(f"형광펜: {names.get(color, '')}")
        else:
            self.pen_button.setIcon(QIcon())
            self.pen_button.setText("형광펜: 끔")
        for reader in self.readers():
            reader.set_pen(color)

    def _show_page(self, number: int) -> None:
        if self.session is not None:
            self.page_label.setText(f"{number + 1} / {self.session.page_count} 쪽")

    def close_paper(self) -> None:
        """Close the paper in the current tab."""
        self.close_tab(self.reader)

    # -- engine and consent (§5.3) ------------------------------------------------

    def engine(self, *, ask: bool = True) -> TranslationEngine | None:
        if not self.settings.translation_enabled:
            if not ask:
                return None
            answer = QMessageBox.question(
                self,
                "GPT 사용 동의",
                "번역·사전·GPT 질문은 이 컴퓨터의 Codex CLI(ChatGPT 계정 로그인)를 통해 GPT를 사용합니다.\n\n"
                "• 논문의 <텍스트>가 OpenAI로 전송됩니다.\n"
                "• GPT 탭에서 질문할 때 직접 첨부한 캡처 이미지와 파일도 함께 전송됩니다.\n"
                "• GPT 대화는 같은 내용을 다시 보내지 않도록 Codex 세션으로 이어 가며, 그 기록은\n"
                "  이 컴퓨터에 저장되었다가 '새 대화'를 누르면 지워집니다.\n"
                "• 퀴즈를 만들 때는 그 슬라이드의 텍스트, 등록한 STT 중 해당 부분, 이전 퀴즈 예시가 전송됩니다.\n"
                "• PDF 파일 자체와 메모·노트는 전송되지 않습니다.\n"
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
        task = self._translations.get(self.reader) if self.reader is not None else None
        if task is not None and task.running:
            self.stop_translation()
        else:
            if self.reader is not None:
                self._ahead_paused.discard(self.reader)
            self.start_translation(ask=True)

    def start_translation(
        self, *, ask: bool, reader: ReaderView | None = None, pages: list[int] | None = None
    ) -> None:
        """Translate what is missing in a paper (the current tab's by
        default), from the page in view on; only ``pages`` when given."""
        reader = reader or self.reader
        if reader is None:
            return
        task = self._translations.get(reader)
        if task is not None and task.running:
            return
        engine = self.engine(ask=ask or not self.settings.translation_enabled)
        if engine is None:
            return
        session = reader.session
        start_page = reader.current_page
        parallel = int(self.settings.get("parallel", 3))

        def work(cancel: threading.Event, report) -> Progress:  # type: ignore[no-untyped-def]
            return translate_paper(
                session.layout, session.project.translations, engine,
                title=session.project.title, start_page=start_page, pages=pages,
                parallel=parallel, cancel=cancel, on_progress=report,
            )

        # Each tab translates on its own; the toolbar shows the current one's.
        task = BackgroundTask(work, name="translate")
        task.progress.connect(lambda progress, r=reader: self._translation_progress(r, progress))
        task.succeeded.connect(lambda p, r=reader: r is self.reader and self._status(p.message, 8000))
        task.failed.connect(lambda exc, r=reader: self._translation_failed(r, exc))
        task.done.connect(lambda r=reader: self._translation_done(r))
        self._translations[reader] = task.start()
        if reader is self.reader:
            self.translate_action.setText("번역 중지")
            self.progress.show()

    # -- read-ahead (an option that saves usage) ------------------------------------

    def _ahead_pages(self) -> int:
        """How many pages past the one in view to translate; 0 for all."""
        try:
            return max(0, int(self.settings.get("translate_ahead", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _read_ahead(self, reader: ReaderView, *, ask: bool = False) -> None:
        """With read-ahead on, translate the pages around the one in view
        that still need it. Each page is tried once this session, so a
        sentence that keeps failing is not asked for again on every scroll;
        "번역 시작" retries everything."""
        ahead = self._ahead_pages()
        if not ahead or not self.settings.get("auto_translate", True):
            return
        if not ask and not self.settings.translation_enabled:
            return  # never a consent prompt for scrolling
        if reader in self._ahead_paused:
            return
        task = self._translations.get(reader)
        if task is not None and task.running:
            return  # picked up again when it finishes
        session = reader.session
        current = reader.current_page
        tried = self._ahead_tried.setdefault(reader, set())
        window = range(max(0, current - 1), min(session.page_count, current + ahead + 1))
        store = session.project.translations
        pages = [
            n for n in window
            if n not in tried and any(not store.settled(s.id) for s in _page_sentences(session.layout, n))
        ]
        if pages:
            tried.update(pages)
            self.start_translation(ask=ask, reader=reader, pages=pages)

    def _show_progress(self, progress: Progress) -> None:
        self.progress.setMaximum(max(1, progress.total))
        self.progress.setValue(progress.done)

    def _translation_progress(self, reader: ReaderView, progress: Progress) -> None:
        if reader not in self._translations:
            return  # its tab was closed
        self._progress[reader] = progress
        if progress.page is not None:
            reader.session.invalidate(progress.page)
        if reader is self.reader:
            self._show_progress(progress)
            if progress.message:
                self._status(progress.message)

    def _translation_failed(self, reader: ReaderView, exc: Exception) -> None:
        if reader not in self._translations:
            return
        # A usage limit or a sign-in problem would only fail again on the
        # next page; read-ahead waits until the reader presses "번역 시작".
        self._ahead_paused.add(reader)
        self._full_next.discard(reader)
        if isinstance(exc, TranslationCancelled):
            if reader is self.reader:
                self._status("번역을 멈췄습니다. 다시 시작하면 이어서 번역합니다.")
            return
        title = "사용량 한도" if isinstance(exc, EngineRateLimited) else "번역 실패"
        QMessageBox.warning(self, title, f"{reader.session.project.title}\n\n{exc}")

    def _translation_done(self, reader: ReaderView) -> None:
        if reader not in self._translations:
            return
        self._progress.pop(reader, None)
        reader.session.invalidate(-1)
        if reader is self.reader:
            self.translate_action.setText("번역 시작")
            self.progress.hide()
        if reader in self._full_next:
            self._full_next.discard(reader)
            self.start_translation(ask=False, reader=reader)
            return
        # The reader may have moved on while these pages were translating.
        self._read_ahead(reader)

    def translate_whole_paper(self) -> None:
        """Everything still missing in the current tab, now or straight after
        the read-ahead run under way."""
        reader = self.reader
        if reader is None:
            return
        self._ahead_paused.discard(reader)
        task = self._translations.get(reader)
        if task is not None and task.running:
            self._full_next.add(reader)
        else:
            self.start_translation(ask=True)

    def stop_translation(self) -> None:
        task = self._translations.get(self.reader) if self.reader is not None else None
        if task is not None and task.running:
            # Stopped on purpose: read-ahead waits for "번역 시작".
            self._ahead_paused.add(self.reader)
            self._full_next.discard(self.reader)
            task.cancel()

    # -- export -------------------------------------------------------------------

    def export_pdf(self, *, side_by_side: bool) -> None:
        if self.session is None:
            return
        project, layout = self.session.project, self.session.layout
        # With read-ahead on, pages never reached are not translated yet.
        missing = sum(1 for _, _, s in layout.sentences() if not project.translations.settled(s.id))
        if missing:
            box = QMessageBox(self)
            box.setWindowTitle("번역본 PDF")
            box.setText(f"아직 번역되지 않은 문장이 {missing}개 있습니다. 그 부분은 영어 원문 그대로 들어갑니다.")
            translate_all = box.addButton("전체 번역 먼저", QMessageBox.ButtonRole.AcceptRole)
            box.addButton("그대로 내보내기", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is cancel:
                return
            if box.clickedButton() is translate_all:
                self.translate_whole_paper()
                self._status("전체 번역을 시작했습니다. 끝나면 다시 내보내기를 눌러 주세요.", 8000)
                return
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
        for reader in self.readers():
            self.close_tab(reader)
        super().closeEvent(event)


def _page_sentences(layout: PaperLayout, number: int):  # type: ignore[no-untyped-def]
    """The sentences translation would ask for on one page."""
    for block in layout.pages[number].blocks:
        if block.translatable:
            yield from block.sentences


def _tab_title(title: str) -> str:
    return title if len(title) <= 28 else title[:27] + "…"


def _model_box(current: str) -> QComboBox:
    """A model picker: Codex's default, then the models Codex lists for this
    account; a name can also be typed, for one the list does not have yet."""
    box = QComboBox()
    box.setEditable(True)
    box.addItem("Codex 기본 모델", "")
    for slug, name, note in available_models():
        box.addItem(f"{slug} — {note}" if note else slug, slug)
        box.setItemData(box.count() - 1, f"{name}: {note}", Qt.ItemDataRole.ToolTipRole)
    index = box.findData(current)
    if index >= 0:
        box.setCurrentIndex(index)
    else:
        box.setEditText(current)
    return box


def _model_of(box: QComboBox) -> str:
    """The model id picked, or typed; "" for Codex's default."""
    index = box.findText(box.currentText())
    if index >= 0:
        return str(box.itemData(index) or "")
    return box.currentText().split(" — ")[0].strip()
