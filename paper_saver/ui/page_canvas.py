"""One pane: a vertical strip of pages, with hover, click, drag and highlights.

The same widget is the original on the left and the translation on the right;
only ``translated`` differs. Page geometry comes from the session, so the two
panes have identical heights at every zoom and one scroll position means the
same place in both (§1.3).

Hit testing is in PDF points. A mouse position is turned into (page, x, y)
once, and everything after that - nearest sentence, word under the cursor,
the span a drag covers - works in the coordinates the layout was extracted in.

The left button does two things, told apart by distance: a click looks the
word up (§3.1), a drag highlights the text it passes over with the current pen.
In capture mode it does a third instead: a drag draws a rectangle on one page,
and that region is handed on to be asked about or pasted into the note.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..core.geometry import Rect
from .session import PaperSession

__all__ = ["MarginNote", "PageCanvas", "RangeMark"]

#: Space between pages and around the strip, in pixels.
GAP = 14
MARGIN = 16
#: How near, in points, the pointer must be to a sentence to light it (§2.3).
NEAR = 7.0
#: How far, in pixels, the pointer must travel with the button down before a
#: click becomes a highlighter stroke.
DRAG_START = 5

HOVER_FILL = QColor(255, 214, 0, 70)
HOVER_EDGE = QColor(230, 160, 0, 170)
FOCUS_FILL = QColor(66, 133, 244, 60)
#: Sentences a GPT answer cited: the GPT tab's green.
EVIDENCE_FILL = QColor(24, 128, 56, 48)
EVIDENCE_EDGE = QColor(24, 128, 56, 170)
NOTE_MARK = QColor(244, 180, 0)
#: The standing wash on a sentence with a note: a third of the hover strength.
NOTED_FILL = QColor(255, 214, 0, 34)
MARK_ALPHA = 92


@dataclass(frozen=True, slots=True)
class MarginNote:
    """A sentence note, as the pane needs it: which note, where, saying what."""

    note_id: str
    sentence_id: str
    text: str


@dataclass(frozen=True, slots=True)
class RangeMark:
    """A stretch of highlighter on this pane's pages."""

    mark_id: str
    page: int
    rects: tuple[Rect, ...]
    color: str


@dataclass(frozen=True, slots=True)
class _Placed:
    number: int
    x: float
    y: float
    width: float
    height: float


class _NotePopup(QFrame):
    """The little note window that appears beside a noted sentence (§4.2).

    A child of the canvas rather than a top-level window, so it scrolls with
    the page, never takes focus, and moving onto it does not count as leaving
    the canvas - the reader can move from the sentence to the note to click it
    without the note vanishing under the pointer.
    """

    clicked = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("notePopup")
        self.setStyleSheet(
            "#notePopup { background: #fff7c7; border: 1px solid #dfb42a; border-radius: 6px; }"
            "QLabel { color: #463808; font-size: 9pt; background: transparent; }"
            "QLabel#hint { color: #a08a3c; font-size: 8pt; }"
        )
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        hint = QLabel("클릭하면 수정")
        hint.setObjectName("hint")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 6, 9, 5)
        layout.setSpacing(2)
        layout.addWidget(self.label)
        layout.addWidget(hint)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sentence_id = ""
        self.note_id = ""
        self.hide()

    def show_notes(self, notes: list[MarginNote], anchor: QRectF, pointer_x: float, visible: QRectF) -> None:
        self.sentence_id = notes[0].sentence_id
        self.note_id = notes[0].note_id
        text = "\n\n".join(note.text.strip() for note in notes)
        self.label.setText(text)
        # A word-wrapped label shrinks to its narrowest possible width; give a
        # short note its natural width and a long one a readable column.
        natural = max(self.label.fontMetrics().horizontalAdvance(line) for line in text.split("\n"))
        self.label.setFixedWidth(min(280, natural + 6))
        self.adjustSize()
        width, height = self.width(), self.height()
        x = min(max(visible.left() + 4, pointer_x - width / 2), visible.right() - width - 4)
        y = anchor.top() - height - 6
        if y < visible.top() + 2:
            y = anchor.bottom() + 6
        self.move(int(x), int(y))
        self.show()
        self.raise_()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.note_id:
            self.clicked.emit(self.note_id)
            self.hide()


class PageCanvas(QWidget):
    #: The sentence under (near) the pointer changed; "" when none.
    hovered = Signal(str)
    #: A word was clicked: page, text, sentence id, sentence text, global pos.
    wordClicked = Signal(int, str, str, str, QPoint)
    #: Right click on a sentence: sentence id, global pos.
    sentenceMenu = Signal(str, QPoint)
    #: Right click on a stretch of highlighter: mark id, global pos.
    markMenu = Signal(str, QPoint)
    #: A highlighter stroke finished: page, rectangles, text covered.
    rangeHighlighted = Signal(int, list, str)
    #: The note popup was clicked: note id.
    noteClicked = Signal(str)
    #: A capture rectangle was drawn: page, rectangle in points.
    regionCaptured = Signal(int, object)
    #: Capture mode was left without capturing (right click).
    captureCancelled = Signal()
    #: The left button went down on the pages (not while capturing).
    pressed = Signal()

    def __init__(self, session: PaperSession, *, translated: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.translated = translated
        self.zoom = 1.0
        #: Colour a drag paints with; empty turns the highlighter off.
        self.pen_color = "#ffd400"
        self._placed: list[_Placed] = []
        self._hover = ""
        self._focus = ""
        self._evidence: list[str] = []
        self._notes: dict[str, list[MarginNote]] = {}
        #: Reader's sentence highlights: sentence id -> colour.
        self._marks: dict[str, QColor] = {}
        self._ranges: list[RangeMark] = []
        #: Where the left button went down: position, page, token index.
        self._press: tuple[QPointF, int, int] | None = None
        #: The stroke in progress: page, first token, last token.
        self._stroke: tuple[int, int, int] | None = None
        #: Capture mode, and the rectangle being drawn: page, start, end.
        self.capture_mode = False
        self._band: tuple[int, QPointF, QPointF] | None = None
        self._popup = _NotePopup(self)
        self._popup.clicked.connect(self.noteClicked)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._relayout()
        session.pageTranslated.connect(self._on_page_translated)

    # -- geometry ---------------------------------------------------------------

    def set_zoom(self, zoom: float) -> None:
        self.zoom = max(0.3, min(4.0, zoom))
        self._popup.hide()
        self._relayout()
        self.update()

    def _relayout(self) -> None:
        self._placed = []
        widest = max((self.session.page_size(n)[0] for n in range(self.session.page_count)), default=600)
        width = widest * self.zoom + MARGIN * 2
        y = float(MARGIN)
        for number in range(self.session.page_count):
            w, h = self.session.page_size(number)
            pw, ph = w * self.zoom, h * self.zoom
            self._placed.append(_Placed(number, (width - pw) / 2, y, pw, ph))
            y += ph + GAP
        self.setFixedSize(QSize(int(width), int(y - GAP + MARGIN)))

    def page_top(self, number: int) -> int:
        if 0 <= number < len(self._placed):
            return int(self._placed[number].y) - MARGIN // 2
        return 0

    def point_y(self, number: int, y: float) -> int:
        """Widget y of a point on a page, for scrolling a sentence into view."""
        if 0 <= number < len(self._placed):
            return int(self._placed[number].y + y * self.zoom)
        return 0

    def page_at_y(self, y: float) -> int:
        for placed in self._placed:
            if y < placed.y + placed.height + GAP / 2:
                return placed.number
        return max(0, len(self._placed) - 1)

    def _to_page(self, pos: QPointF, *, clamp_to: int | None = None) -> tuple[int, float, float] | None:
        """``(page, x, y)`` in points; with ``clamp_to``, always on that page."""
        if clamp_to is not None and 0 <= clamp_to < len(self._placed):
            placed = self._placed[clamp_to]
            x = min(max(pos.x(), placed.x), placed.x + placed.width)
            y = min(max(pos.y(), placed.y), placed.y + placed.height)
            return placed.number, (x - placed.x) / self.zoom, (y - placed.y) / self.zoom
        for placed in self._placed:
            if placed.y <= pos.y() <= placed.y + placed.height and placed.x <= pos.x() <= placed.x + placed.width:
                return placed.number, (pos.x() - placed.x) / self.zoom, (pos.y() - placed.y) / self.zoom
        return None

    def _to_widget(self, placed: _Placed, rect: Rect) -> QRectF:
        z = self.zoom
        return QRectF(placed.x + rect.x0 * z, placed.y + rect.y0 * z, rect.width * z, rect.height * z)

    # -- state set from outside -------------------------------------------------

    def set_hover(self, sentence_id: str) -> None:
        if sentence_id != self._hover:
            self._hover = sentence_id
            self.update()

    def set_focus_sentence(self, sentence_id: str) -> None:
        self._focus = sentence_id
        self.update()

    def set_evidence(self, sentence_ids: list[str]) -> None:
        """Sentences a GPT answer rests on; they stay lit until replaced."""
        if sentence_ids != self._evidence:
            self._evidence = list(sentence_ids)
            self.update()

    def set_highlights(self, colors: dict[str, str]) -> None:
        marks: dict[str, QColor] = {}
        for sentence_id, name in colors.items():
            color = QColor(name)
            color.setAlpha(MARK_ALPHA)
            marks[sentence_id] = color
        self._marks = marks
        self.update()

    def set_range_marks(self, marks: list[RangeMark]) -> None:
        self._ranges = list(marks)
        self.update()

    def set_margin_notes(self, notes: list[MarginNote]) -> None:
        grouped: dict[str, list[MarginNote]] = {}
        for note in notes:
            grouped.setdefault(note.sentence_id, []).append(note)
        self._notes = grouped
        self._popup.hide()
        self.update()

    def _on_page_translated(self, number: int) -> None:
        if self.translated:
            self.update()

    # -- painting ---------------------------------------------------------------

    def _scale(self) -> float:
        """Render scale: zoom times the screen's pixel ratio, in quarter steps."""
        raw = self.zoom * self.devicePixelRatioF()
        return max(0.5, round(raw * 4) / 4)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(0x52, 0x56, 0x5b))
        visible = QRectF(event.rect())
        scale = self._scale()
        for placed in self._placed:
            target = QRectF(placed.x, placed.y, placed.width, placed.height)
            if not target.intersects(visible):
                continue
            if self.translated:
                image = self.session.translated_image(placed.number, scale)
            else:
                image = self.session.original_image(placed.number, scale)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(target, image)
            self._paint_marks(painter, placed)
            if self.translated and not self.session.translated_page(placed.number).any:
                self._paint_pending(painter, target)
        if self._band is not None:
            self._paint_band(painter)
        painter.end()

    def set_capture_mode(self, on: bool) -> None:
        self.capture_mode = on
        self._band = None
        self._press = None
        self._stroke = None
        self._popup.hide()
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.update()

    def _band_rect(self) -> QRectF:
        assert self._band is not None
        number, start, end = self._band
        placed = self._placed[number]
        page = QRectF(placed.x, placed.y, placed.width, placed.height)
        return QRectF(start, end).normalized().intersected(page)

    def _paint_band(self, painter: QPainter) -> None:
        band = self._band_rect()
        painter.setPen(QPen(QColor(26, 115, 232), 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(QColor(26, 115, 232, 36))
        painter.drawRect(band)

    def _rects(self, sentence_id: str) -> list[Rect]:
        return self.session.rects_of(sentence_id, translated=self.translated)

    def _paint_marks(self, painter: QPainter, placed: _Placed) -> None:
        painter.setPen(Qt.PenStyle.NoPen)

        # Sentences that carry a note keep a faint wash all the time, so the
        # reader's annotations stay visible on the page. Drawn first: every
        # other mark paints over it.
        painter.setBrush(NOTED_FILL)
        for sentence_id in self._notes:
            ref = self.session.sentence(sentence_id)
            if ref is None or ref.page != placed.number or sentence_id in self._marks:
                continue
            for rect in self._rects(sentence_id):
                painter.drawRoundedRect(self._to_widget(placed, rect.inflated(0.8, 0.4)), 2, 2)

        # Whole-sentence highlights, on both pages at once.
        for sentence_id, color in self._marks.items():
            ref = self.session.sentence(sentence_id)
            if ref is None or ref.page != placed.number:
                continue
            painter.setBrush(color)
            for rect in self._rects(sentence_id):
                painter.drawRect(self._to_widget(placed, rect.inflated(0.6, 0.2)))

        # Free highlighter strokes, and the one being drawn.
        for mark in self._ranges:
            if mark.page != placed.number:
                continue
            color = QColor(mark.color)
            color.setAlpha(MARK_ALPHA)
            painter.setBrush(color)
            for rect in mark.rects:
                painter.drawRect(self._to_widget(placed, rect.inflated(0.4, 0.2)))
        if self._stroke is not None and self._stroke[0] == placed.number and self.pen_color:
            color = QColor(self.pen_color)
            color.setAlpha(MARK_ALPHA)
            painter.setBrush(color)
            for rect in self._stroke_rects()[0]:
                painter.drawRect(self._to_widget(placed, rect.inflated(0.4, 0.2)))

        for sentence_id, fill, edge in (
            *((s, EVIDENCE_FILL, EVIDENCE_EDGE) for s in self._evidence),
            (self._focus, FOCUS_FILL, None),
            (self._hover, HOVER_FILL, HOVER_EDGE),
        ):
            ref = self.session.sentence(sentence_id) if sentence_id else None
            if ref is None or ref.page != placed.number:
                continue
            painter.setPen(QPen(edge, 1) if edge else Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            for rect in self._rects(sentence_id):
                painter.drawRoundedRect(self._to_widget(placed, rect.inflated(0.8, 0.4)), 2, 2)

        # A tab in the page margin beside every sentence that carries a note.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(NOTE_MARK)
        for sentence_id in self._notes:
            ref = self.session.sentence(sentence_id)
            if ref is None or ref.page != placed.number:
                continue
            rects = self._rects(sentence_id)
            if rects:
                first = self._to_widget(placed, rects[0])
                painter.drawRoundedRect(QRectF(placed.x + 3, first.y(), 4, max(6.0, first.height())), 2, 2)

    def _paint_pending(self, painter: QPainter, target: QRectF) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 110))
        painter.drawRect(target)
        badge = QRectF(target.x() + 12, target.y() + 12, 170, 26)
        painter.setBrush(QColor(40, 44, 52, 215))
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "아직 번역되지 않은 쪽")

    # -- the note popup ---------------------------------------------------------

    def _update_popup(self, sentence_id: str, pointer: QPointF) -> None:
        notes = self._notes.get(sentence_id)
        if not notes:
            self._popup.hide()
            return
        if self._popup.isVisible() and self._popup.sentence_id == sentence_id:
            return
        ref = self.session.sentence(sentence_id)
        rects = self._rects(sentence_id)
        if ref is None or not rects or not 0 <= ref.page < len(self._placed):
            self._popup.hide()
            return
        placed = self._placed[ref.page]
        anchor = self._to_widget(placed, rects[0])
        for rect in rects[1:]:
            anchor = anchor.united(self._to_widget(placed, rect))
        self._popup.show_notes(notes, anchor, pointer.x(), QRectF(self.visibleRegion().boundingRect()))

    # -- the highlighter --------------------------------------------------------

    def _tokens(self, number: int) -> list[tuple[Rect, str]]:
        return self.session.page_tokens(number, translated=self.translated)

    def _token_at(self, number: int, x: float, y: float) -> int:
        """Index of the text token nearest a point on a page, or -1."""
        best, best_distance = -1, 1e9
        for index, (rect, _) in enumerate(self._tokens(number)):
            # Vertical distance counts double: a drag that wanders between
            # lines should stay on the line it is nearest to.
            dx = max(rect.x0 - x, 0.0, x - rect.x1)
            dy = max(rect.y0 - y, 0.0, y - rect.y1) * 2
            distance = dx + dy
            if distance < best_distance:
                best, best_distance = index, distance
                if distance == 0:
                    break
        return best

    def _stroke_rects(self) -> tuple[list[Rect], str]:
        if self._stroke is None:
            return [], ""
        number, first, last = self._stroke
        tokens = self._tokens(number)
        lo, hi = sorted((first, last))
        chosen = tokens[lo : hi + 1]
        return _merge_runs([rect for rect, _ in chosen]), " ".join(text for _, text in chosen if text)

    def _mark_at(self, number: int, x: float, y: float) -> str:
        for mark in reversed(self._ranges):
            if mark.page == number and any(rect.inflated(0.5).contains(x, y) for rect in mark.rects):
                return mark.mark_id
        return ""

    # -- pointer ----------------------------------------------------------------

    def _nearest_sentence(self, number: int, x: float, y: float) -> str:
        rects = (
            self.session.translated_rects(number)
            if self.translated
            else self.session.source_rects(number)
        )
        best, best_distance = "", NEAR
        for sentence_id, rect in rects:
            distance = rect.distance_to(x, y)
            if distance < best_distance:
                best, best_distance = sentence_id, distance
                if distance == 0:
                    break
        return best

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = event.position()
        if self.capture_mode:
            if self._band is not None:
                self._band = (self._band[0], self._band[1], pos)
                self.update()
            return
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            start, number, first = self._press
            if self.pen_color and first >= 0 and (
                self._stroke is not None or (pos - start).manhattanLength() >= DRAG_START
            ):
                hit = self._to_page(pos, clamp_to=number)
                if hit is not None:
                    last = self._token_at(*hit)
                    if last >= 0:
                        self._stroke = (number, first, last)
                        self._popup.hide()
                        self.setCursor(Qt.CursorShape.IBeamCursor)
                        self.update()
            return

        hit = self._to_page(pos)
        sentence_id = self._nearest_sentence(*hit) if hit else ""
        if sentence_id != self._hover:
            self.hovered.emit(sentence_id)
        self._update_popup(sentence_id, pos)
        self._set_cursor(hit)

    def _set_cursor(self, hit: tuple[int, float, float] | None) -> None:
        over_word = False
        if hit is not None:
            if self.translated:
                over_word = self.session.translated_piece_at(*hit) is not None
            else:
                over_word = self.session.word_at(*hit) is not None
        self.setCursor(Qt.CursorShape.PointingHandCursor if over_word else Qt.CursorShape.ArrowCursor)

    def leaveEvent(self, event: object) -> None:  # noqa: N802
        # Moving onto the popup is not leaving: it is a child of this widget.
        if not self._popup.underMouse():
            self._popup.hide()
            self.hovered.emit("")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        hit = self._to_page(event.position())
        if self.capture_mode:
            if event.button() == Qt.MouseButton.RightButton:
                self.captureCancelled.emit()
            elif event.button() == Qt.MouseButton.LeftButton and hit is not None:
                self._band = (hit[0], event.position(), event.position())
            return
        if hit is None:
            return
        number, x, y = hit
        global_pos = event.globalPosition().toPoint()
        if event.button() == Qt.MouseButton.RightButton:
            mark_id = self._mark_at(number, x, y)
            if mark_id:
                self.markMenu.emit(mark_id, global_pos)
                return
            sentence_id = self._nearest_sentence(number, x, y)
            if sentence_id:
                self.sentenceMenu.emit(sentence_id, global_pos)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.pressed.emit()
            self._press = (event.position(), number, self._token_at(number, x, y))
            self._stroke = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self.capture_mode and self._band is not None and event.button() == Qt.MouseButton.LeftButton:
            number = self._band[0]
            band = self._band_rect()
            self._band = None
            self.update()
            placed = self._placed[number]
            z = self.zoom
            rect = Rect(
                (band.left() - placed.x) / z, (band.top() - placed.y) / z,
                (band.right() - placed.x) / z, (band.bottom() - placed.y) / z,
            )
            # A click, or a sliver, is not a capture.
            if rect.width >= 4 and rect.height >= 4:
                self.regionCaptured.emit(number, rect)
            return
        if event.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        start, number, _ = self._press
        self._press = None
        if self._stroke is not None:
            rects, text = self._stroke_rects()
            self._stroke = None
            self.update()
            if rects:
                self.rangeHighlighted.emit(number, rects, text)
            return
        self._click(start, event.globalPosition().toPoint())

    def _click(self, pos: QPointF, global_pos: QPoint) -> None:
        """A press and release in place: look the word up (§3.1)."""
        hit = self._to_page(pos)
        if hit is None:
            return
        number, x, y = hit
        if self.translated:
            piece = self.session.translated_piece_at(number, x, y)
            if piece is not None:
                text, sentence_id = piece
                ref = self.session.sentence(sentence_id)
                self.wordClicked.emit(number, text, sentence_id, ref.sentence.text if ref else "", global_pos)
        else:
            word = self.session.word_at(number, x, y)
            if word is not None:
                self.wordClicked.emit(number, word.text, word.sentence_id, word.sentence_text, global_pos)


def _merge_runs(rects: list[Rect]) -> list[Rect]:
    """Join consecutive token boxes on the same line into one strip.

    Only neighbours in reading order are joined, and only when they are close
    horizontally: two tokens that share a height but sit in different columns
    must stay two strips, or a stroke ending at the foot of the left column
    would paint a bar straight across the gutter.
    """
    runs: list[Rect] = []
    for rect in rects:
        if runs:
            last = runs[-1]
            shorter = min(last.height, rect.height)
            overlap = min(last.y1, rect.y1) - max(last.y0, rect.y0)
            if shorter > 0 and overlap >= shorter * 0.5 and -2.0 <= rect.x0 - last.x1 <= 14.0:
                runs[-1] = last.union(rect)
                continue
        runs.append(rect)
    return runs
