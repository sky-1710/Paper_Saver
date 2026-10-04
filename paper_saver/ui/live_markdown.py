"""A Markdown editor that formats as it is typed, the way Notion does.

The text in the editor *is* the Markdown: ``toPlainText()`` is exactly what
the file holds. What changes is only how it is drawn. ``# `` makes the line a
heading and disappears, ``**word**`` turns bold and its asterisks disappear,
``- `` becomes a bullet, ``- [ ] `` a checkbox, ``> `` a quote bar, ``---`` a
rule, ``$x^2$`` a formula, a ``| a | b |`` table a grid, a ``$$`` block a
centred formula, and a line that is one image link is drawn as the picture.
The line the cursor is on - or the whole table or ``$$`` block it is in -
shows its source again (dimmed markers), so it can be edited.

Hidden markers are drawn in a 1-pixel transparent font, so they take no
room. An image line, a table row or a formula gets its size from one spacer
character: set in a font as tall as the picture (or row, or formula) and
letter-spaced to the width wanted, with what it stands for painted into that
space. The document itself is never touched, so undo and the file stay clean.

Enter continues a list or quote (and ends it on an empty item), Tab and
Shift+Tab indent list items, Ctrl+click opens a link, and clicking a
checkbox ticks it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetricsF,
    QImage,
    QImageReader,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QSyntaxHighlighter,
    QTextBlock,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import QTextEdit

from ..core.notebook import ImageRef, resolve_image
from ..render.live_markdown import (
    MATH,
    NORMAL,
    Line,
    inline_html,
    is_separator,
    math_html,
    parse_line,
    table_alignments,
    table_cells,
)
from .markdown_view import pasted_images

__all__ = ["LiveMarkdownEdit"]

_INK = QColor("#202124")
_DIM = QColor("#9aa0a6")
_ACCENT = QColor("#1a73e8")
_MATH = QColor("#8e24aa")
_QUOTE = QColor("#5f6368")
_CODE_BG = QColor("#f1f3f4")
_RULE = QColor("#dadce0")
_CLEAR = QColor(0, 0, 0, 0)
_MONO = ["Consolas", "Menlo", "D2Coding", "monospace"]
_HEADING_SCALE = {1: 1.7, 2: 1.4, 3: 1.2, 4: 1.08, 5: 1.0, 6: 1.0}
_HEADER_BG = QColor("#f8f9fa")
#: Space kept above and below a picture.
_IMAGE_GAP = 6
#: Inside a table cell, around its text.
_CELL_PAD = 5
#: A table column is never drawn narrower than this.
_MIN_COLUMN = 36


def _spacer_font(base: QFont, char: str, height: float, width: float) -> QFont:
    """``base`` sized so a line holding ``char`` is ``height`` tall, and
    letter-spaced so the character is ``width`` wide."""
    probe = QFont(base)
    probe.setPixelSize(100)
    tall = QFont(base)
    tall.setPixelSize(max(1, round(height / max(0.01, QFontMetricsF(probe).height() / 100))))
    tall.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, width - QFontMetricsF(tall).horizontalAdvance(char))
    return tall


class _Table:
    """A table laid out to a width: columns, and a cell document per cell."""

    def __init__(self, rows: list[str], room: float, font: QFont) -> None:
        header = table_cells(rows[0])
        self.columns = len(header)
        aligns = (table_alignments(rows[1]) + ["left"] * self.columns)[: self.columns]
        body = [rows[0]] + rows[2:]
        self.cells: list[list[QTextDocument]] = []
        for index, row in enumerate(body):
            texts = (table_cells(row) + [""] * self.columns)[: self.columns]
            docs = []
            for text, align in zip(texts, aligns):
                doc = QTextDocument()
                doc.setDefaultFont(font)
                doc.setDocumentMargin(0)
                html = inline_html(text)
                doc.setHtml(f'<div align="{align}">{"<b>" + html + "</b>" if index == 0 else html}</div>')
                docs.append(doc)
            self.cells.append(docs)
        natural = [
            max(_MIN_COLUMN, max(row[c].idealWidth() for row in self.cells) + 2 * _CELL_PAD + 1)
            for c in range(self.columns)
        ]
        total = sum(natural)
        if total > room:
            natural = [max(_MIN_COLUMN, w * room / total) for w in natural]
        self.widths = natural
        self.heights: list[float] = []
        for row in self.cells:
            for doc, width in zip(row, self.widths):
                doc.setTextWidth(width - 2 * _CELL_PAD)
            self.heights.append(max(doc.size().height() for doc in row) + 2 * _CELL_PAD)

    def height_of(self, index: int) -> float:
        """The height of source row ``index`` (row 1 is the separator)."""
        if index == 1:
            return 0.0
        return self.heights[0 if index == 0 else index - 1]


class _Highlighter(QSyntaxHighlighter):
    def __init__(self, editor: LiveMarkdownEdit) -> None:
        super().__init__(editor.document())
        self.editor = editor
        self._formats: dict[tuple, QTextCharFormat] = {}

    def forget_formats(self) -> None:
        self._formats.clear()

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        previous = self.previousBlockState()
        line = parse_line(text, NORMAL if previous < 0 else previous)
        self.setCurrentBlockState(line.state)
        block = self.currentBlock()
        active = self.editor.is_active(block)
        if line.kind == "image" and self._image(text, line, active):
            return
        if line.kind in ("table", "math") and not active and self._group(block, text, line):
            return
        roles: list[set[str]] = [set() for _ in text]
        if line.marker is not None:
            for i in range(*line.marker):
                roles[i].add("block_marker")
        for span in line.spans:
            for i in range(span.start, min(span.end, len(text))):
                roles[i].add(span.role)
        start = 0
        for i in range(1, len(text) + 1):
            if i == len(text) or roles[i] != roles[start]:
                self.setFormat(start, i - start, self._format(line, frozenset(roles[start]), active))
                start = i
        if not active:
            self._inline_maths(line)

    def _inline_maths(self, line: Line) -> None:
        """``$...$`` drawn as a formula: the first ``$`` is spaced as wide
        as the formula, the rest hidden; the painter draws it there."""
        base = self.editor.document().defaultFont()
        tiny = QFont(base)
        tiny.setPixelSize(1)
        hidden = self._format(line, frozenset({"hidden"}), False)
        text = self.currentBlock().text()
        for start, end, expression in line.maths:
            spacer = QTextCharFormat(self.format(start))
            width = self.editor.math_doc(expression).idealWidth()
            # The hidden source still takes a sliver per character; the
            # spacer gives that much back.
            used = QFontMetricsF(base).horizontalAdvance("$") + QFontMetricsF(tiny).horizontalAdvance(text[start + 1 : end])
            spacer.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
            spacer.setFontLetterSpacing(width - used)
            spacer.setForeground(_CLEAR)
            self.setFormat(start, 1, spacer)
            self.setFormat(start + 1, end - start - 1, hidden)

    def _group(self, block: QTextBlock, text: str, line: Line) -> bool:
        """A table row or a ``$$`` line off the edited block: one spacer as
        tall as what is painted there, everything else hidden."""
        group = self.editor.group_of(block)
        if group is None or not text:
            return False
        first, last, kind = group
        if kind == "table":
            height = self.editor.table(first, last).height_of(block.blockNumber() - first.blockNumber())
        elif block == first:
            height = self.editor.display_doc(first, last).size().height() + 2 * _IMAGE_GAP
        else:
            height = 0.0
        hidden = self._format(line, frozenset({"hidden"}), False)
        if height <= 0:
            self.setFormat(0, len(text), hidden)
            return True
        spacer = QTextCharFormat()
        spacer.setFont(_spacer_font(self.editor.document().defaultFont(), text[0], height, 0.0))
        spacer.setForeground(_CLEAR)
        self.setFormat(0, 1, spacer)
        self.setFormat(1, len(text) - 1, hidden)
        return True

    def _image(self, text: str, line: Line, active: bool) -> bool:
        size = self.editor.image_size(line)
        if size is None:
            return False
        height = size[1]
        base = self.editor.document().defaultFont()
        metrics = QFontMetricsF(base)
        # Off the edited line the picture fills the line; on it, the source
        # sits under the picture, so the tall character's ascent is what has
        # to hold the picture.
        if active:
            probe = QFont(base)
            probe.setPixelSize(100)
            ratio = QFontMetricsF(probe).height() / max(0.01, QFontMetricsF(probe).ascent())
            need = (height + 2 * _IMAGE_GAP + metrics.height()) * ratio
        else:
            need = height + 2 * _IMAGE_GAP
        # As wide as the character in the normal font: on the edited line it
        # is painted there, at normal size (see _paint_image).
        keep = metrics.horizontalAdvance(text[0]) if active else 0.0
        spacer = QTextCharFormat()
        spacer.setFont(_spacer_font(base, text[0], need, keep))
        spacer.setForeground(_CLEAR)
        self.setFormat(0, 1, spacer)
        rest = self._format(line, frozenset({"dim"} if active else {"hidden"}), active)
        self.setFormat(1, len(text) - 1, rest)
        return True

    def _format(self, line: Line, roles: frozenset[str], active: bool) -> QTextCharFormat:
        key = (line.kind, line.level, roles, active)
        cached = self._formats.get(key)
        if cached is not None:
            return cached
        fmt = QTextCharFormat()
        base = self.editor.document().defaultFont()
        size = base.pointSizeF() if base.pointSizeF() > 0 else 10.0
        if line.kind == "heading":
            fmt.setFontPointSize(size * _HEADING_SCALE[line.level])
            fmt.setFontWeight(QFont.Weight.Bold)
        elif line.kind in ("code", "table"):
            fmt.setFontFamilies(_MONO)
            if line.kind == "code":
                fmt.setBackground(_CODE_BG)
        elif line.kind == "quote":
            fmt.setForeground(_QUOTE)
        if "bold" in roles:
            fmt.setFontWeight(QFont.Weight.Bold)
        if "italic" in roles:
            fmt.setFontItalic(True)
        if "strike" in roles:
            fmt.setFontStrikeOut(True)
            fmt.setForeground(_DIM)
        if "code" in roles:
            fmt.setFontFamilies(_MONO)
            fmt.setBackground(_CODE_BG)
        if "math" in roles:
            # The Korean UI font draws a backslash as a won sign.
            fmt.setFontFamilies(_MONO)
            fmt.setForeground(_MATH)
        if "link" in roles:
            fmt.setForeground(_ACCENT)
            fmt.setFontUnderline(True)
        if "dim" in roles:
            fmt.setForeground(_DIM)
        hide = "hidden" in roles or (not active and "marker" in roles)
        if "block_marker" in roles:
            if line.kind == "ordered":
                fmt.setForeground(_DIM if active else _ACCENT)
            elif active:
                fmt.setForeground(_DIM)
            elif line.kind in ("heading", "rule"):
                hide = True
            else:
                # Bullets, boxes and quote bars are painted over the space.
                fmt.setForeground(_CLEAR)
        elif "marker" in roles and active:
            fmt.setForeground(_DIM)
        if line.kind == "rule" and not active:
            hide = True
        if hide:
            tiny = QFont()
            tiny.setPixelSize(1)
            fmt.setFont(tiny, QTextCharFormat.FontPropertiesInheritanceBehavior.FontPropertiesSpecifiedOnly)
            fmt.setForeground(_CLEAR)
            fmt.clearBackground()
        self._formats[key] = fmt
        return fmt


class LiveMarkdownEdit(QTextEdit):
    """Markdown source, drawn formatted. See the module docstring."""

    imagePasted = Signal(QImage)
    focusedIn = Signal()
    #: The text itself changed (not just how it is drawn).
    edited = Signal()

    def __init__(self, parent: object = None) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self.setObjectName("liveMarkdown")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)
        self.document().setDocumentMargin(10)
        #: Where relative image links are resolved from; no pictures if None.
        self.base_dir: Path | None = None
        #: First and last block number of what is being edited - the line,
        #: or the table or ``$$`` block the cursor is in - whose source shows.
        self.active = (-1, -1)
        self._tables: dict[tuple, _Table] = {}
        self._maths: dict[tuple, QTextDocument] = {}
        self._parsed: dict[tuple[str, int], Line] = {}
        self._text = ""
        self._sizes: dict[Path, tuple[int, int, int]] = {}
        self._pictures: dict[tuple[Path, int], QImage] = {}
        self._width = 0
        self._highlighter = _Highlighter(self)
        self._resized = QTimer(self, singleShot=True, interval=120)
        self._resized.timeout.connect(self._refit)
        self.textChanged.connect(self._text_changed)
        self.cursorPositionChanged.connect(self._cursor_moved)

    # -- text ------------------------------------------------------------------------

    def setPlainText(self, text: str) -> None:  # noqa: N802
        self.active = (-1, -1)
        self._parsed.clear()
        self._text = text
        super().setPlainText(text)
        self._cursor_moved()

    def _text_changed(self) -> None:
        # Redrawing a line (moving the cursor) also emits textChanged; only
        # a change to the characters counts as an edit.
        text = self.toPlainText()
        if text != self._text:
            self._text = text
            self.edited.emit()

    def _cursor_moved(self) -> None:
        block = self.textCursor().block()
        group = self.group_of(block)
        span = (group[0].blockNumber(), group[1].blockNumber()) if group else (block.blockNumber(),) * 2
        if span == self.active:
            return
        old, self.active = self.active, span
        for first, last in (old, span):
            for n in range(max(0, first), last + 1):
                found = self.document().findBlockByNumber(n)
                if found.isValid():
                    self._highlighter.rehighlightBlock(found)

    def is_active(self, block: QTextBlock) -> bool:
        return self.active[0] <= block.blockNumber() <= self.active[1]

    def _state_before(self, block: QTextBlock) -> int:
        previous = block.previous()
        return previous.userState() if previous.isValid() and previous.userState() >= 0 else NORMAL

    def group_of(self, block: QTextBlock) -> tuple[QTextBlock, QTextBlock, str] | None:
        """The table or ``$$`` block a line belongs to, when it is a whole
        one: a header, a separator row and rows; or an opened and closed
        formula."""
        line = self.line_of(block)
        if line.kind == "table":
            first = last = block
            while first.previous().isValid() and self.line_of(first.previous()).kind == "table":
                first = first.previous()
            following = last.next()
            while following.isValid() and parse_line(following.text(), NORMAL).kind == "table":
                last, following = following, following.next()
            if first == last or not is_separator(first.next().text()):
                return None
            return first, last, "table"
        if line.kind == "math":
            first = block
            while first.previous().isValid() and self._state_before(first) == MATH:
                first = first.previous()
            state, last = self._state_before(first), first
            # Read forward from the text: lines below may not be drawn yet.
            while True:
                state = parse_line(last.text(), state).state
                if state != MATH:
                    return first, last, "math"
                if not last.next().isValid():
                    return None
                last = last.next()
        return None

    def _room(self) -> float:
        return max(60.0, self.viewport().width() - 2 * self.document().documentMargin() - 8)

    def table(self, first: QTextBlock, last: QTextBlock) -> _Table:
        rows = []
        block = first
        while True:
            rows.append(block.text())
            if block == last:
                break
            block = block.next()
        font = self.document().defaultFont()
        key = (tuple(rows), round(self._room()), font.key())
        found = self._tables.get(key)
        if found is None:
            if len(self._tables) > 64:
                self._tables.clear()
            found = self._tables[key] = _Table(rows, self._room(), font)
        return found

    def math_doc(self, expression: str, scale: float = 1.1) -> QTextDocument:
        """A formula laid out; the maths face runs small beside Korean, so
        it is set a little larger."""
        font = QFont(self.document().defaultFont())
        font.setPointSizeF(font.pointSizeF() * scale)
        key = (expression, font.key())
        doc = self._maths.get(key)
        if doc is None:
            if len(self._maths) > 512:
                self._maths.clear()
            doc = QTextDocument()
            doc.setDefaultFont(font)
            doc.setDocumentMargin(0)
            doc.setHtml(f'<span style="color: {_INK.name()}">{math_html(expression)}</span>')
            self._maths[key] = doc
        return doc

    def display_doc(self, first: QTextBlock, last: QTextBlock) -> QTextDocument:
        lines = []
        block = first
        while True:
            lines.append(block.text())
            if block == last:
                break
            block = block.next()
        source = " ".join(lines).strip()
        source = source[2:] if source.startswith("$$") else source
        source = source[:-2] if source.endswith("$$") else source
        return self.math_doc(source, 1.3)

    def line_of(self, block: QTextBlock) -> Line:
        """What a line is, read from its text and the state of the one above."""
        previous = block.previous()
        state = previous.userState() if previous.isValid() and previous.userState() >= 0 else NORMAL
        key = (block.text(), state)
        line = self._parsed.get(key)
        if line is None:
            if len(self._parsed) > 4000:
                self._parsed.clear()
            line = self._parsed[key] = parse_line(*key)
        return line

    # -- pictures ----------------------------------------------------------------------

    def _image_path(self, line: Line) -> Path | None:
        if self.base_dir is None or not line.target:
            return None
        return resolve_image(ImageRef(0, 0, line.target, line.alt, line.wiki), self.base_dir)

    def image_size(self, line: Line) -> tuple[int, int] | None:
        """The size a picture is drawn at: its own, or narrowed to fit."""
        path = self._image_path(line)
        if path is None:
            return None
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        known = self._sizes.get(path)
        if known is None or known[2] != mtime:
            natural = QImageReader(str(path)).size()
            if not natural.isValid():
                return None
            known = self._sizes[path] = (natural.width(), natural.height(), mtime)
            self._pictures = {k: v for k, v in self._pictures.items() if k[0] != path}
        width, height, _ = known
        room = max(60, self.viewport().width() - 2 * int(self.document().documentMargin()) - 8)
        if width > room:
            height, width = round(height * room / width), room
        return width, height

    def _picture(self, path: Path, width: int) -> QImage | None:
        picture = self._pictures.get((path, width))
        if picture is None:
            image = QImage(str(path))
            if image.isNull():
                return None
            picture = image.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
            self._pictures[(path, width)] = picture
        return picture

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            # A stylesheet applied on first show changes the font after the
            # text was drawn: every size measured from the old one is stale.
            self._tables.clear()
            self._maths.clear()
            self._highlighter.forget_formats()
            self._highlighter.rehighlight()

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)  # type: ignore[arg-type]
        if self.viewport().width() != self._width:
            self._resized.start()

    def _refit(self) -> None:
        self._width = self.viewport().width()
        block = self.document().begin()
        while block.isValid():
            if self.line_of(block).kind in ("image", "table"):
                self._highlighter.rehighlightBlock(block)
            block = block.next()

    def refresh_images(self) -> None:
        """Draw image lines again (a picture was saved after its link)."""
        self._sizes.clear()
        self._pictures.clear()
        self._refit()

    # -- drawing -----------------------------------------------------------------------

    def _visible_blocks(self) -> list[QTextBlock]:
        blocks = []
        block = self.cursorForPosition(self.viewport().rect().topLeft()).block()
        bottom = self.viewport().height()
        offset = self.verticalScrollBar().value()
        layout = self.document().documentLayout()
        while block.isValid():
            rect = layout.blockBoundingRect(block)
            if rect.top() - offset > bottom:
                break
            blocks.append(block)
            block = block.next()
        return blocks

    def _char_rect(self, block: QTextBlock, index: int) -> QRectF:
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + index)
        return QRectF(self.cursorRect(cursor))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        blocks = self._visible_blocks()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        right = self.viewport().width() - self.document().documentMargin()
        margin = self.document().documentMargin()
        for block in blocks:  # backgrounds, under the text
            if self.line_of(block).kind in ("code", "fence"):
                top = self._char_rect(block, 0).top()
                rect = self._block_rect(block)
                painter.fillRect(QRectF(margin - 4, top, right - margin + 8, rect.height()), _CODE_BG)
        painter.end()
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for block in blocks:
            line = self.line_of(block)
            active = self.is_active(block)
            if not active and line.maths:
                self._paint_maths(painter, block, line)
            if line.kind in ("table", "math") and not active:
                self._paint_group(painter, block)
            elif line.kind == "image":
                self._paint_image(painter, block, line)
            elif line.kind == "rule" and not active:
                rect = self._block_rect(block)
                pen = QPen(_RULE, 1.5)
                painter.setPen(pen)
                y = rect.center().y()
                painter.drawLine(QPointF(margin, y), QPointF(right, y))
            elif line.kind == "quote" and line.marker is not None:
                rect = self._block_rect(block)
                x = self._char_rect(block, line.marker[0]).left() + 2
                painter.fillRect(QRectF(x, rect.top(), 3, rect.height()), _RULE)
            elif line.kind == "bullet" and not active and line.marker is not None:
                first = self._char_rect(block, line.marker[0])
                r = max(2.0, first.height() * 0.1)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_INK)
                painter.drawEllipse(QPointF(first.left() + r + 2, first.center().y()), r, r)
            elif line.kind == "task" and not active:
                self._paint_box(painter, block, line)
        painter.end()

    def _paint_maths(self, painter: QPainter, block: QTextBlock, line: Line) -> None:
        rect = self._block_rect(block)
        layout = block.layout()
        for start, _end, expression in line.maths:
            doc = self.math_doc(expression)
            at = self._char_rect(block, start)
            row = layout.lineForTextPosition(start)
            first = doc.firstBlock().layout()
            # On the text's baseline, not centred: sub- and superscripts make
            # the formula taller below and above it.
            ascent = first.lineAt(0).ascent() if first.lineCount() else doc.size().height() * 0.8
            baseline = rect.top() + layout.position().y() - self.document().documentLayout().blockBoundingRect(block).top()
            baseline += row.y() + row.ascent() if row.isValid() else at.center().y() - rect.top()
            painter.save()
            painter.translate(at.left(), baseline - ascent)
            doc.drawContents(painter)
            painter.restore()

    def _paint_group(self, painter: QPainter, block: QTextBlock) -> None:
        group = self.group_of(block)
        if group is None:
            return
        first, last, kind = group
        rect = self._block_rect(block)
        if kind == "math":
            if block == first:
                doc = self.display_doc(first, last)
                x = max(rect.left(), (self.viewport().width() - doc.idealWidth()) / 2)
                painter.save()
                painter.translate(x, rect.top() + _IMAGE_GAP)
                doc.drawContents(painter)
                painter.restore()
            return
        table = self.table(first, last)
        index = block.blockNumber() - first.blockNumber()
        x = rect.left() + self.document().documentMargin()
        total = sum(table.widths)
        painter.setPen(QPen(_RULE, 1))
        if index == 1:  # the separator: the header's lower rule
            painter.fillRect(QRectF(x, rect.top(), total, max(1.0, rect.height())), _RULE)
            return
        cells = table.cells[0 if index == 0 else index - 1]
        for doc, width in zip(cells, table.widths):
            cell = QRectF(x, rect.top(), width, rect.height())
            if index == 0:
                painter.fillRect(cell, _HEADER_BG)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(cell)
            painter.save()
            painter.translate(x + _CELL_PAD, rect.top() + _CELL_PAD)
            doc.drawContents(painter)
            painter.restore()
            x += width

    def _block_rect(self, block: QTextBlock) -> QRectF:
        rect = self.document().documentLayout().blockBoundingRect(block)
        return rect.translated(-self.horizontalScrollBar().value(), -self.verticalScrollBar().value())

    def _box_rect(self, block: QTextBlock, line: Line) -> QRectF:
        assert line.marker is not None
        first = self._char_rect(block, line.marker[0])
        side = min(first.height() * 0.75, 16.0)
        return QRectF(first.left() + 1, first.center().y() - side / 2, side, side)

    def _paint_box(self, painter: QPainter, block: QTextBlock, line: Line) -> None:
        box = self._box_rect(block, line)
        painter.setPen(QPen(_ACCENT if line.checked else _DIM, 1.4))
        painter.setBrush(_ACCENT if line.checked else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 3, 3)
        if line.checked:
            painter.setPen(QPen(QColor("white"), 1.8))
            w, h = box.width(), box.height()
            painter.drawPolyline([QPointF(box.left() + w * 0.22, box.top() + h * 0.52),
                                  QPointF(box.left() + w * 0.42, box.top() + h * 0.72),
                                  QPointF(box.left() + w * 0.78, box.top() + h * 0.3)])

    def _paint_image(self, painter: QPainter, block: QTextBlock, line: Line) -> None:
        path = self._image_path(line)
        size = self.image_size(line)
        if path is None or size is None:
            return
        picture = self._picture(path, size[0])
        if picture is None:
            return
        rect = self._block_rect(block)
        x = self._char_rect(block, 0).left()
        painter.drawImage(QPointF(x, rect.top() + _IMAGE_GAP), picture)
        if self.is_active(block) and block.text():
            # The tall spacer character, drawn as itself.
            row = block.layout().lineAt(0)
            painter.setFont(self.document().defaultFont())
            painter.setPen(_DIM)
            painter.drawText(QPointF(x, rect.top() + row.y() + row.ascent()), block.text()[0])

    # -- typing ------------------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key, mods = event.key(), event.modifiers()
        plain = not mods & ~Qt.KeyboardModifier.KeypadModifier
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and plain and self._continue_block():
            return
        if key == Qt.Key.Key_Tab and plain and self._indent(+1):
            return
        if key == Qt.Key.Key_Backtab and self._indent(-1):
            return
        super().keyPressEvent(event)

    def _current_line(self) -> tuple[QTextCursor, str, Line]:
        cursor = self.textCursor()
        return cursor, cursor.block().text(), self.line_of(cursor.block())

    def _continue_block(self) -> bool:
        """Enter on a list item or quote starts the next one; on an empty
        one it ends the list."""
        cursor, text, line = self._current_line()
        if cursor.hasSelection() or line.marker is None or line.kind not in ("bullet", "ordered", "task", "quote"):
            return False
        start, end = line.marker
        if cursor.positionInBlock() < end:
            return False
        if not text[end:].strip():
            cursor.beginEditBlock()
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            return True
        marker = text[:end]
        if line.kind == "ordered":
            digits = text[start:end].rstrip()
            number = int(digits[:-1]) + 1
            marker = text[:start] + f"{number}{digits[-1]} "
        elif line.kind == "task":
            marker = text[:start] + text[start] + " [ ] "
        cursor.insertText("\n" + marker)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        return True

    def _indent(self, step: int) -> bool:
        cursor, text, line = self._current_line()
        if cursor.hasSelection() or line.kind not in ("bullet", "ordered", "task"):
            return False
        edit = QTextCursor(cursor.block())
        if step > 0:
            edit.insertText("  ")
        else:
            spaces = len(text) - len(text.lstrip(" "))
            if not spaces:
                return True
            edit.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, min(2, spaces))
            edit.removeSelectedText()
        return True

    # -- mouse -------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._click_box(event.position()):
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not self.textCursor().hasSelection()
            and self._open_link(event.position())
        ):
            return
        super().mouseReleaseEvent(event)

    def _click_box(self, pos: QPointF) -> bool:
        block = self.cursorForPosition(pos.toPoint()).block()
        line = self.line_of(block)
        if line.kind != "task" or self.is_active(block):
            return False
        if not self._box_rect(block, line).adjusted(-3, -3, 3, 3).contains(pos):
            return False
        edit = QTextCursor(block)
        edit.setPosition(block.position() + line.box + 1)
        edit.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor)
        edit.insertText(" " if line.checked else "x")
        return True

    def _open_link(self, pos: QPointF) -> bool:
        cursor = self.cursorForPosition(pos.toPoint())
        line = self.line_of(cursor.block())
        at = cursor.positionInBlock()
        for span in line.spans:
            if span.role == "link" and span.start <= at <= span.end and span.start in line.links:
                target = line.links[span.start]
                url = QUrl(target)
                if url.isRelative() and self.base_dir is not None:
                    url = QUrl.fromLocalFile(str(self.base_dir / target))
                QDesktopServices.openUrl(url)
                return True
        return False

    # -- pasting -----------------------------------------------------------------------

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802
        return bool(pasted_images(source)) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802
        images = pasted_images(source)
        if not images:
            # Plain text only: formatting is the Markdown itself.
            self.textCursor().insertText(source.text())
            self.ensureCursorVisible()
            return
        for image in images:
            self.imagePasted.emit(image)

    def focusInEvent(self, event: object) -> None:  # noqa: N802
        super().focusInEvent(event)  # type: ignore[arg-type]
        self.focusedIn.emit()
