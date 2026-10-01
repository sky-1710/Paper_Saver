"""Writing the translation and the notes out of the application.

``translated_pdf``
    The right-hand pane as a PDF: every page composed at print resolution and
    saved at the original page size, so it prints and paginates exactly like
    the paper. With ``side_by_side`` each page is original and translation on
    one landscape sheet.

    The text survives, so the file can be searched and copied from in any PDF
    viewer. The original half is the paper's own page, vectors and text and
    all. The translated half is the composed picture with an invisible text
    layer over it, the way a scanned-and-OCR'd PDF is made: the Korean on the
    lines it was set into, and the original words of everything left as the
    original's pixels - figure labels, equations, the reference list.
``notes_markdown``
    The memo, then every anchored note under the sentence it belongs to with
    that sentence's translation, then the wordbook.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pymupdf
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from ..core.atomic_io import write_text_atomic
from ..core.models import PageLayout, PaperLayout
from ..core.project import HIGHLIGHT_COLORS, Project
from ..pdf.document import PaperDocument
from .fonts import FontBook
from .page import TranslatedPage, layout_page, paint_page, rendered_to_qimage

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


def _text_font() -> pymupdf.Font:
    """A face with Hangul and Latin for the invisible text layer.

    Malgun Gothic when Windows has it; MuPDF's own CJK fallback otherwise,
    which every PyMuPDF carries. The layer is never seen, so the face only has
    to have the glyphs.
    """
    if sys.platform == "win32":
        malgun = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgun.ttf"
        if malgun.is_file():
            try:
                return pymupdf.Font(fontfile=str(malgun))
            except (RuntimeError, ValueError):
                pass
    return pymupdf.Font("cjk")


def _write(
    writer: pymupdf.TextWriter,
    font: pymupdf.Font,
    text: str,
    x: float,
    baseline: float,
    width: float,
    size: float,
    *,
    space: bool = False,
) -> None:
    """``text`` from ``x`` on ``baseline``, sized to span ``width``.

    Fitting the width keeps a viewer's search highlight and selection over the
    word that is shown, however the two faces differ. ``space`` writes the
    space that follows as a character, so "합성 논문" is found as two words
    rather than run together.
    """
    natural = font.text_length(text, fontsize=size)
    if natural > 0 and width > 0:
        size = max(1.0, min(size * width / natural, size * 3))
    try:
        writer.append((x, baseline), text + (" " if space else ""), font=font, fontsize=size)
    except (RuntimeError, ValueError):
        pass  # one string the face cannot set; the rest of the page still goes in


def _text_layer(
    sheet: pymupdf.Page, source: pymupdf.Page, page: PageLayout, translated: TranslatedPage, font: pymupdf.Font, dx: float
) -> None:
    """The translated half's words, invisible, over its picture at ``dx``."""
    writer = pymupdf.TextWriter(sheet.rect)
    for laid in translated.layouts.values():
        for line in laid.lines:
            for piece, after in zip(line.pieces, [*line.pieces[1:], None]):
                if piece.text.strip() and not piece.math:
                    # Words are set as separate pieces; a gap before the next one is a space.
                    gap = after is not None and after.x > piece.x + piece.width + 0.5
                    _write(writer, font, piece.text, dx + piece.x, line.baseline, piece.width, laid.size, space=gap)

    # What was not repainted keeps the original's words - the same rule as
    # paint_page: translated paragraphs out, protected regions back in.
    blocks = {block.id: block for block in page.blocks}
    blanked = [blocks[i].rect.inflated(0.8, 0.6) for i in translated.layouts if i in blocks]
    for x0, y0, x1, y1, word, *_ in source.get_text("words", sort=True):
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(r.contains(cx, cy) for r in blanked) and not any(r.contains(cx, cy) for r in page.protected):
            continue
        _write(writer, font, word, dx + x0, y1 - (y1 - y0) * 0.22, x1 - x0, (y1 - y0) * 0.9)
    # Render mode 3 is neither fill nor stroke: there to find, not to see.
    writer.write_text(sheet, render_mode=3)


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
    text_font = _text_font()
    translations = project.translations.snapshot()
    out = pymupdf.open()
    with PaperDocument(project.pdf_path) as document:
        for page in layout.pages:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("내보내기를 취소했습니다.")
            rendered = document.render(page.number, EXPORT_SCALE)
            original = rendered_to_qimage(rendered.samples, rendered.width, rendered.height, rendered.stride)
            translated = layout_page(page, translations, fonts)
            painted = paint_page(original, page, translated, fonts, EXPORT_SCALE)
            dx = page.width + 18 if side_by_side else 0.0
            sheet = out.new_page(width=page.width + dx, height=page.height)
            if side_by_side:
                # The paper's own page: sharp at any zoom, its text intact.
                sheet.show_pdf_page(pymupdf.Rect(0, 0, page.width, page.height), document.raw, page.number)
            sheet.insert_image(pymupdf.Rect(dx, 0, dx + page.width, page.height), stream=_jpeg(painted))
            _text_layer(sheet, document.page(page.number), page, translated, text_font, dx)
            report((page.number + 1, layout.page_count))
    out.subset_fonts()  # the text layer's face, cut down to the glyphs it uses
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

