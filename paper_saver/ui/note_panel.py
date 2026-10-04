"""The 노트 tab: the paper's Markdown note, pictures and all.

The note is a ``.md`` file the reader can put anywhere - the project folder
until they choose otherwise, or an Obsidian vault, a synced folder. A paper
can have several; the ones it has had are listed at the top. Captures and
pasted screenshots are saved into ``assets/`` beside the file and linked
where the cursor is, so the note reads the same in any Markdown editor.

There is no preview: the editor formats as it is typed (``# `` becomes a
heading, ``**`` bold, an image link the picture) while the text stays the
Markdown the file holds - see ``live_markdown``.

It saves itself 0.7 s after the last keystroke and when the paper closes
(§4.3). If another program changed the file meanwhile, that version is kept
beside it before this one is written, and reloaded quietly when this side has
nothing unsaved.

"Notion으로" sends the note, images included, to a page in the reader's
Notion - only when pressed.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QImage, QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..core.chat import capture_name
from ..core.notebook import ASSETS_DIR, copy_linked_images, image_link, read_note, write_note
from ..core.notion import NotionClient, NotionError, export_markdown, page_id_from
from ..core.project import Project
from .keys import CAPTURE_LABEL, PASTE_LABEL
from .live_markdown import LiveMarkdownEdit
from .markdown_view import save_png
from .workers import BackgroundTask

log = logging.getLogger(__name__)

__all__ = ["NotePanel", "NotionDialog"]

DEFAULT_NOTE = "note.md"


class NotionDialog(QDialog):
    """Where notes go in Notion: an integration secret and a parent page."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Notion 연결")
        self.settings = settings
        help_text = QLabel(
            "1) <a href='https://www.notion.so/profile/integrations'>notion.so/profile/integrations</a>에서 "
            "'새 통합(internal)'을 만들고 시크릿을 복사합니다.<br>"
            "2) 노트를 넣을 Notion 페이지에서 <b>•••</b> → <b>연결</b> → 만든 통합을 추가합니다.<br>"
            "3) 그 페이지의 링크를 아래에 붙여 넣습니다. 노트는 그 아래 새 페이지로 만들어집니다.<br>"
            "<span style='color:#888'>보내기를 누를 때만 노트와 이미지가 Notion으로 전송됩니다.</span>"
        )
        help_text.setOpenExternalLinks(True)
        help_text.setWordWrap(True)
        self.token = QLineEdit(settings.get("notion_token", "") or "")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("ntn_… 또는 secret_…")
        self.page = QLineEdit(settings.get("notion_parent", "") or "")
        self.page.setPlaceholderText("https://www.notion.so/…")
        form = QFormLayout(self)
        form.addRow(help_text)
        form.addRow("통합 시크릿", self.token)
        form.addRow("대상 페이지 링크", self.page)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.setMinimumWidth(520)

    def accept(self) -> None:
        if not self.token.text().strip() or page_id_from(self.page.text()) is None:
            QMessageBox.warning(self, "Notion 연결", "시크릿과, 페이지 id가 들어 있는 페이지 링크를 모두 넣어 주세요.")
            return
        self.settings.set("notion_token", self.token.text().strip())
        self.settings.set("notion_parent", self.page.text().strip())
        super().accept()


class NotePanel(QWidget):
    statusMessage = Signal(str)

    def __init__(self, project: Project, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.settings = settings
        self.path = self._initial_path()
        self._mtime: int | None = None
        self._dirty = False
        self._loading = False
        self._notion_task: BackgroundTask | None = None
        self._closed = False

        self.files = QComboBox()
        self.files.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.files.setMinimumContentsLength(12)
        self.files.activated.connect(self._pick)

        more = QMenu(self)
        more.addAction("새 노트…", self.new_note)
        more.addAction("다른 노트 열기…", self.open_note)
        more.addAction("다른 곳에 저장(옮기기)…", self.save_as)
        more.addSeparator()
        more.addAction("노트 폴더 열기", self.open_folder)
        more.addAction("Notion 연결 설정…", self.configure_notion)
        more_button = QToolButton()
        more_button.setText("파일 ▾")
        more_button.setMenu(more)
        more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)

        self.notion_button = QPushButton("Notion으로")
        self.notion_button.setToolTip("이 노트를 이미지까지 Notion 페이지로 보냅니다")
        self.notion_button.clicked.connect(self.export_notion)

        top = QHBoxLayout()
        top.addWidget(self.files, 1)
        top.addWidget(more_button)
        top.addWidget(self.notion_button)

        self.editor = LiveMarkdownEdit()
        self.editor.setPlaceholderText(
            "Markdown으로 적으면 바로 서식이 됩니다: # 제목, **굵게**, *기울임*, - 목록, - [ ] 할 일, > 인용, `코드`.\n"
            f"캡처({CAPTURE_LABEL}) → '노트에 붙이기', 또는 스크린샷을 {PASTE_LABEL}로 붙여 넣으면 그림이 들어갑니다.\n"
            "자동 저장됩니다. 저장 위치는 '파일 ▾ → 다른 곳에 저장'으로 자유롭게 바꿀 수 있습니다."
        )
        self.editor.edited.connect(self._changed)
        self.editor.imagePasted.connect(lambda image: self.insert_image(image, "붙여 넣은 이미지"))
        self.editor.focusedIn.connect(self.check_external)

        self.status = QLabel()
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        self._timer = QTimer(self, singleShot=True, interval=700)
        self._timer.timeout.connect(self.save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(top)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.status)
        self._load(self.path)

    # -- which file ------------------------------------------------------------------

    def _initial_path(self) -> Path:
        stored = self.project.meta.get("note_file")
        return Path(stored) if stored else self.project.root / DEFAULT_NOTE

    def _known(self) -> list[Path]:
        paths = [Path(p) for p in self.project.meta.get("note_files", [])]
        default = self.project.root / DEFAULT_NOTE
        if default not in paths and (default.is_file() or not paths):
            paths.insert(0, default)
        if self.path not in paths:
            paths.append(self.path)
        return [p for p in paths if p.is_file() or p == self.path]

    def _refresh_files(self) -> None:
        self.files.blockSignals(True)
        self.files.clear()
        for path in self._known():
            folder = "프로젝트 폴더" if path.parent == self.project.root else path.parent.name
            label = f"{path.name} — {folder}"
            self.files.addItem(label, str(path))
            self.files.setItemData(self.files.count() - 1, str(path), Qt.ItemDataRole.ToolTipRole)
        self.files.setCurrentIndex(max(0, self.files.findData(str(self.path))))
        self.files.blockSignals(False)
        self.files.setToolTip(str(self.path))

    def _remember(self) -> None:
        known = [str(p) for p in self._known()]
        self.project.set_meta(note_file=str(self.path), note_files=known)

    def _pick(self, index: int) -> None:
        path = Path(self.files.itemData(index))
        if path != self.path:
            self.switch_to(path)

    def switch_to(self, path: Path) -> None:
        self.save()
        self._load(path)
        self._remember()

    def _load(self, path: Path) -> None:
        text, self._mtime = read_note(path)
        self.path = path
        self._loading = True
        self.editor.base_dir = path.parent
        self.editor.setPlainText(text)
        self._loading = False
        self._dirty = False
        self._refresh_files()
        self._say(f"{path}" if path.is_file() else "새 노트 — 입력하면 저장됩니다")

    # -- saving ------------------------------------------------------------------------

    def _changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self._timer.start()

    def save(self) -> None:
        self._timer.stop()
        if not self._dirty:
            return
        try:
            self._mtime, kept = write_note(self.path, self.editor.toPlainText(), known_mtime=self._mtime)
        except OSError as exc:
            self._say(f"저장하지 못했습니다: {exc}")
            return
        first = self.project.meta.get("note_file") != str(self.path)
        self._dirty = False
        if first:
            self._remember()
            self._refresh_files()
        if kept is not None:
            self._say(f"다른 프로그램에서 고친 내용은 '{kept.name}'에 따로 보관했습니다.")
            QMessageBox.information(
                self, "노트",
                f"노트 파일이 다른 프로그램에서 바뀌어 있었습니다.\n그 내용은 지우지 않고 여기에 보관했습니다:\n\n{kept}",
            )
        else:
            self._say(f"저장됨 · {self.path}")

    def check_external(self) -> None:
        """Pick up an edit made elsewhere, when there is nothing unsaved here."""
        if self._dirty or not self.path.is_file():
            return
        if self.path.stat().st_mtime_ns != self._mtime:
            cursor = self.editor.textCursor().position()
            self._load(self.path)
            moved = self.editor.textCursor()
            moved.setPosition(min(cursor, len(self.editor.toPlainText())))
            self.editor.setTextCursor(moved)
            self._say("다른 프로그램에서 바뀐 내용을 다시 불러왔습니다.")

    def _say(self, text: str) -> None:
        self.status.setText(text)

    # -- files -------------------------------------------------------------------------

    def _start_dir(self) -> str:
        return self.settings.get("note_dir") or str(self.path.parent)

    def new_note(self) -> None:
        name = f"{_safe_name(self.project.title)}.md"
        target, _ = QFileDialog.getSaveFileName(self, "새 노트 만들 위치", str(Path(self._start_dir()) / name), "Markdown (*.md)")
        if not target:
            return
        path = Path(target)
        self.settings.set("note_dir", str(path.parent))
        self.save()
        if path.is_file():
            # Choosing an existing file opens it; nothing is overwritten.
            self.switch_to(path)
            return
        self._load(path)
        self.editor.setPlainText(f"# {self.project.title}\n\n")
        self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self.save()
        self._remember()

    def open_note(self) -> None:
        target, _ = QFileDialog.getOpenFileName(self, "노트 열기", self._start_dir(), "Markdown (*.md *.markdown)")
        if target:
            self.settings.set("note_dir", str(Path(target).parent))
            self.switch_to(Path(target))

    def save_as(self) -> None:
        """Write the note somewhere else, images included, and carry on there."""
        target, _ = QFileDialog.getSaveFileName(
            self, "노트를 저장할 위치", str(Path(self._start_dir()) / self.path.name), "Markdown (*.md)"
        )
        if not target:
            return
        path = Path(target)
        if path == self.path:
            return
        self.settings.set("note_dir", str(path.parent))
        self.save()  # the old file keeps whatever was typed up to now
        text = self.editor.toPlainText()
        try:
            copied = copy_linked_images(text, self.path.parent, path.parent)
            # An existing file there is kept aside by write_note, never lost.
            self._mtime, kept = write_note(path, text, known_mtime=None)
        except OSError as exc:
            QMessageBox.warning(self, "노트", f"저장하지 못했습니다:\n{exc}")
            return
        self.path, self._dirty = path, False
        self.editor.base_dir = path.parent
        self._remember()
        self._refresh_files()
        extra = f" · 이미지 {len(copied)}개 함께 복사" if copied else ""
        if kept is not None:
            extra += f" · 원래 있던 파일은 '{kept.name}'로 보관"
        self._say(f"옮겼습니다: {path}{extra}")

    def open_folder(self) -> None:
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -- pictures ----------------------------------------------------------------------

    def _assets(self) -> Path:
        folder = self.path.parent / ASSETS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def insert_image(self, image: QImage, alt: str = "", page: int | None = None) -> Path | None:
        """Save a picture beside the note and link it at the cursor."""
        try:
            path = save_png(image, self._assets() / capture_name(page))
        except OSError as exc:
            self._say(f"이미지를 저장하지 못했습니다: {exc}")
            return None
        self._insert_block(image_link(self.path, path, alt))
        return path

    def link_file(self, source: Path, alt: str = "") -> str:
        """Copy a picture into the note's assets and return the link to it."""
        target = self._assets() / source.name
        if not target.exists() and source.is_file():
            shutil.copy2(source, target)
        return image_link(self.path, target, alt)

    def _insert_block(self, markdown: str) -> None:
        """Put a block of Markdown on lines of its own at the cursor."""
        cursor = self.editor.textCursor()
        before = self.editor.toPlainText()[: cursor.position()]
        prefix = "" if not before or before.endswith("\n\n") else ("\n" if before.endswith("\n") else "\n\n")
        cursor.insertText(f"{prefix}{markdown}\n\n")
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()

    def append(self, markdown: str) -> None:
        """Add a block at the end of the note (an answer from the GPT tab)."""
        self.editor.moveCursor(QTextCursor.MoveOperation.End)
        self._insert_block(markdown.strip())

    # -- Notion ------------------------------------------------------------------------

    def configure_notion(self) -> bool:
        return bool(NotionDialog(self.settings, self).exec())

    def export_notion(self) -> None:
        if self._notion_task is not None and self._notion_task.running:
            self._notion_task.cancel()
            return
        self.save()
        text = self.editor.toPlainText()
        if not text.strip():
            self._say("노트가 비어 있습니다.")
            return
        if not (self.settings.get("notion_token") and self.settings.get("notion_parent")) and not self.configure_notion():
            return
        pages: dict[str, str] = dict(self.project.meta.get("notion_pages") or {})
        previous = pages.get(str(self.path))
        replace: str | None = None
        if previous:
            box = QMessageBox(self)
            box.setWindowTitle("Notion으로 보내기")
            box.setText("이 노트는 전에 Notion으로 보낸 적이 있습니다.")
            box.setInformativeText("이전 페이지를 새 내용으로 바꿀까요? (이전 페이지는 Notion 휴지통으로 갑니다)")
            swap = box.addButton("바꾸기", QMessageBox.ButtonRole.AcceptRole)
            fresh = box.addButton("새 페이지로", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is swap:
                replace = previous
            elif box.clickedButton() is not fresh:
                return
        client = NotionClient(str(self.settings.get("notion_token")))
        parent = str(self.settings.get("notion_parent"))
        base = self.path.parent
        title = self.project.title
        # The note that was sent, even if another is open by the time it lands.
        note_key = str(self.path)

        def work(cancel: threading.Event, report) -> tuple[str, str]:  # type: ignore[no-untyped-def]
            return export_markdown(
                client, parent, text, base, fallback_title=title, replace=replace, report=report, cancel=cancel
            )

        # The page is recorded even when the paper was closed meanwhile; the
        # widgets are only touched while they still exist.
        def done(result: tuple[str, str]) -> None:
            page_id, url = result
            pages = dict(self.project.meta.get("notion_pages") or {})
            pages[note_key] = page_id
            self.project.set_meta(notion_pages=pages)
            if not self._closed:
                self.status.setText(f"Notion에 보냈습니다: <a href='{url}'>페이지 열기</a>" if url else "Notion에 보냈습니다.")

        def failed(exc: Exception) -> None:
            if self._closed:
                return
            message = str(exc) if isinstance(exc, NotionError) else f"Notion으로 보내지 못했습니다: {exc}"
            self._say(message.split("\n")[0])
            QMessageBox.warning(self, "Notion", message)
            if isinstance(exc, NotionError) and "시크릿" in message:
                self.configure_notion()

        task = BackgroundTask(work, name="notion")
        task.progress.connect(lambda m: None if self._closed else self._say(str(m)))
        task.succeeded.connect(done)
        task.failed.connect(failed)
        task.done.connect(lambda: None if self._closed else self.notion_button.setText("Notion으로"))
        self.notion_button.setText("보내기 중지")
        self._say("Notion으로 보내는 중…")
        self._notion_task = task.start()

    # -- lifetime ------------------------------------------------------------------------

    def shutdown(self) -> None:
        self._closed = True
        self.save()
        if self._notion_task is not None:
            self._notion_task.cancel()


def _safe_name(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    return re.sub(r"\s+", " ", cleaned).strip(" .")[:80] or "노트"
