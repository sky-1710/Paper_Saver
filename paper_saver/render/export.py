"""Writing the translation and the notes out of the application.

``translated_pdf``
    The right-hand pane as a PDF: every page composed at print resolution and
    saved at the original page size, so it prints and paginates exactly like
    the paper. With ``side_by_side`` each page is original and translation on
    one landscape sheet.
``notes_markdown``
    The memo, then every anchored note under the sentence it belongs to with
    that sentence's translation, then the wordbook.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from ..core.atomic_io import write_text_atomic
from ..core.models import PaperLayout
from ..core.project import HIGHLIGHT_COLORS, Project
from ..pdf.document import PaperDocument
from .fonts import FontBook
from .page import layout_page, paint_page, rendered_to_qimage

__all__ = ["notes_markdown", "translated_pdf", "write_notes_markdown"]

#: Pixels per point for exported pages: 2.5 is 180 DPI, sharp in print and a
#: few hundred kilobytes per page as JPEG.
EXPORT_SCALE = 2.5


def _jpeg(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.convertToFormat(QImage.Format.Format_RGB888).save(buffer, "JPEG", 88)
    buffer.close()
    return bytes(data.data())


def translated_pdf(
    project: Project,
    layout: PaperLayout,
    target: Path,
    *,
    side_by_side: bool = False,
    cancel: threading.Event | None = None,
    report: Callable[[tuple[int, int]], None] = lambda _p: None,
) -> Path:
    fonts = FontBook()  # per thread: fonts are not shared across threads
    translations = project.translations.snapshot()
    out = pymupdf.open()
    with PaperDocument(project.pdf_path) as document:
        for page in layout.pages:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("내보내기를 취소했습니다.")
            rendered = document.render(page.number, EXPORT_SCALE)
            original = rendered_to_qimage(rendered.samples, rendered.width, rendered.height, rendered.stride)
            painted = paint_page(original, page, layout_page(page, translations, fonts), fonts, EXPORT_SCALE)
            if side_by_side:
                sheet = out.new_page(width=page.width * 2 + 18, height=page.height)
                sheet.insert_image(pymupdf.Rect(0, 0, page.width, page.height), stream=_jpeg(original))
                sheet.insert_image(
                    pymupdf.Rect(page.width + 18, 0, page.width * 2 + 18, page.height),
                    stream=_jpeg(painted),
                )
            else:
                sheet = out.new_page(width=page.width, height=page.height)
                sheet.insert_image(sheet.rect, stream=_jpeg(painted))
            report((page.number + 1, layout.page_count))
    target.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(target), garbage=3, deflate=True)
    out.close()
    return target


def _plain(text: str) -> str:
    return re.sub("⟦m\\d+⟧", "[수식]", text or "")


def notes_markdown(project: Project, layout: PaperLayout) -> str:
    order = {s.id: i for i, (_, _, s) in enumerate(layout.sentences())}
    lines = [f"# {project.title}", ""]
    if project.notes.memo.strip():
        lines += ["## 메모", "", project.notes.memo.strip(), ""]
    anchored = sorted(project.notes.anchored, key=lambda n: (order.get(n.sentence_id, 1 << 30), n.created))
    if anchored:
        lines += ["## 문장 메모", ""]
        for note in anchored:
            translation = project.translations.get(note.sentence_id) or ""
            lines.append(f"### p.{note.page + 1}")
            lines.append(f"> {_plain(note.quote)}")
            if translation:
                lines.append(">")
                lines.append(f"> {_plain(translation)}")
            lines += ["", note.text, ""]
    marks = sorted(project.highlights.items.values(), key=lambda h: (order.get(h.sentence_id, 1 << 30), h.created))
    if marks:
        names = {color: name for name, color in HIGHLIGHT_COLORS.items()}
        lines += ["## 형광펜", ""]
        for mark in marks:
            translation = project.translations.get(mark.sentence_id) or ""
            lines.append(f"- **p.{mark.page + 1} · {names.get(mark.color, mark.color)}** {_plain(mark.quote)}")
            if translation:
                lines.append(f"  - {_plain(translation)}")
        lines.append("")
    strokes = sorted(project.highlights.strokes, key=lambda st: (st.page, st.rects[0][1] if st.rects else 0))
    if strokes:
        names = {color: name for name, color in HIGHLIGHT_COLORS.items()}
        if not marks:
            lines += ["## 형광펜", ""]
        for stroke in strokes:
            where = "번역" if stroke.side == "translated" else "원문"
            lines.append(f"- **p.{stroke.page + 1} · {names.get(stroke.color, stroke.color)} · {where}** {stroke.text}")
        lines.append("")
    if project.wordbook.entries:
        lines += ["## 단어장", "", "| 단어 | 뜻(문맥) | 예문 |", "|---|---|---|"]
        for entry in project.wordbook.entries:
            cells = [entry.lemma or entry.word, entry.in_context, _plain(entry.sentence)]
            lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
        lines.append("")
    return "\n".join(lines)


def write_notes_markdown(project: Project, layout: PaperLayout, target: Path) -> Path:
    write_text_atomic(target, notes_markdown(project, layout))
    return target

