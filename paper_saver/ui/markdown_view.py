"""Small pieces the GPT and note tabs share: Markdown shown with its images,
an editor that accepts pasted pictures, and saving a QImage as PNG."""

from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, Qt, QUrl, Signal
from PySide6.QtGui import QImage, QImageReader, QKeyEvent, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit

from ..core.atomic_io import write_bytes_atomic
from ..core.notebook import IMAGE_SUFFIXES, image_refs, resolve_image
from ..render.markdown_display import center_display_maths, prepare_markdown

__all__ = ["ImagePasteEdit", "markdown_html", "png_bytes", "save_png"]


def png_bytes(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data.data())


def save_png(image: QImage, path: Path) -> Path:
    write_bytes_atomic(path, png_bytes(image))
    return path


def _file_url(path: Path) -> str:
    # Percent-encoded: Qt's Markdown reader drops a link with a space in it,
    # and paper folders are named after titles, which have spaces.
    return bytes(QUrl.fromLocalFile(str(path.resolve())).toEncoded().data()).decode("ascii")


def markdown_html(text: str, base_dir: Path | None, max_image_width: int) -> str:
    """Markdown as the HTML body a QTextBrowser shows, images fitted to width.

    Qt's own Markdown reader does the text, after LaTeX and bold-beside-Korean
    are rewritten into something it shows (see ``render.markdown_display``);
    images are pointed at absolute
    file URLs first (so the result does not depend on the browser's search
    path) and given a width, because a 2400-pixel capture drawn at its natural
    size would push everything else off the side of a narrow panel.
    """
    if base_dir is not None:
        for ref in reversed(image_refs(text)):
            if ref.remote:
                continue
            path = resolve_image(ref, base_dir)
            replacement = f"![{ref.alt}]({_file_url(path)})" if path else f"*(이미지 없음: {ref.target})*"
            text = text[: ref.start] + replacement + text[ref.end :]
    document = QTextDocument()
    document.setMarkdown(prepare_markdown(text))
    body = document.toHtml()
    match = re.search(r"<body[^>]*>(.*)</body>", body, re.S)
    body = center_display_maths(match.group(1) if match else body)

    def sized(match: re.Match[str]) -> str:
        src = html.unescape(match.group(1))
        width = max_image_width
        local = QUrl(src).toLocalFile()
        if local:
            natural = QImageReader(local).size()
            if natural.isValid():
                width = min(max_image_width, natural.width())
        return f'<img src="{html.escape(src)}" width="{width}" />'

    return re.sub(r'<img src="([^"]+)"[^>]*/?>', sized, body)


class ImagePasteEdit(QPlainTextEdit):
    """Plain text editing, plus: a pasted or dropped picture becomes a signal.

    Screenshots from the clipboard and image files dragged in both arrive as
    :attr:`imagePasted`; the owner decides where the picture is saved and
    what text (if any) stands for it. Everything else pastes as plain text.
    """

    imagePasted = Signal(QImage)
    #: Enter without Shift, when ``submit_on_enter`` is set.
    submitted = Signal()
    focusedIn = Signal()

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.submit_on_enter = False

    def _images(self, source: QMimeData) -> list[QImage]:
        images: list[QImage] = []
        for url in source.urls() if source.hasUrls() else []:
            path = Path(url.toLocalFile())
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                image = QImage(str(path))
                if not image.isNull():
                    images.append(image)
        # Word, OneNote and browsers put a picture of copied text on the
        # clipboard alongside the text itself; text wins, so only a bare
        # picture (a screenshot) pastes as an image.
        if not images and source.hasImage() and not source.text().strip():
            image = QImage(source.imageData())
            if not image.isNull():
                images.append(image)
        return images

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802
        return bool(self._images(source)) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802
        images = self._images(source)
        if not images:
            super().insertFromMimeData(source)
            return
        for image in images:
            self.imagePasted.emit(image)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if self.submit_on_enter and enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def focusInEvent(self, event: object) -> None:  # noqa: N802
        super().focusInEvent(event)  # type: ignore[arg-type]
        self.focusedIn.emit()
