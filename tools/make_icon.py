"""Draw the application icon: ``python tools/make_icon.py``.

Two overlapping pages - the original (A) behind, the translation (가) in
front - which is the whole application in one picture. Written as PNG for the
window, ICO for Windows and ICNS for macOS into ``paper_saver/resources``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "paper_saver" / "resources"

from paper_saver.render.fonts import resolve_family  # noqa: E402


def page(painter: QPainter, rect: QRectF, fill: QColor, edge: QColor, glyph: str, font: QFont, ink: QColor) -> None:
    path = QPainterPath()
    path.addRoundedRect(rect, rect.width() * 0.08, rect.width() * 0.08)
    painter.setPen(QPen(edge, rect.width() * 0.025))
    painter.setBrush(fill)
    painter.drawPath(path)
    # A few ruled "lines of text" under the glyph.
    rule = QPen(QColor(ink.red(), ink.green(), ink.blue(), 70), rect.width() * 0.035)
    rule.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(rule)
    for index in range(3):
        y = rect.top() + rect.height() * (0.66 + index * 0.1)
        right = rect.right() - rect.width() * (0.18 if index < 2 else 0.4)
        painter.drawLine(int(rect.left() + rect.width() * 0.18), int(y), int(right), int(y))
    painter.setPen(ink)
    painter.setFont(font)
    painter.drawText(QRectF(rect.left(), rect.top() + rect.height() * 0.06, rect.width(), rect.height() * 0.55),
                     Qt.AlignmentFlag.AlignCenter, glyph)


def draw(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    s = float(size)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    tile = QPainterPath()
    tile.addRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.2, s * 0.2)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1a5fd0"))
    painter.drawPath(tile)

    serif = QFont("Times New Roman")
    serif.setPixelSize(int(s * 0.3))
    serif.setBold(True)
    korean = QFont(resolve_family(False))
    korean.setPixelSize(int(s * 0.27))
    korean.setBold(True)

    page(painter, QRectF(s * 0.16, s * 0.15, s * 0.44, s * 0.58), QColor("#eef3ff"), QColor("#9db8ea"), "A", serif, QColor("#2a3d66"))
    page(painter, QRectF(s * 0.40, s * 0.28, s * 0.44, s * 0.58), QColor("#ffffff"), QColor("#f2c230"), "가", korean, QColor("#1a5fd0"))
    painter.end()
    return image


def main() -> int:
    QGuiApplication(sys.argv[:1])
    OUT.mkdir(parents=True, exist_ok=True)
    big = draw(1024)
    big.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(str(OUT / "icon.png"))
    big.scaled(256, 256, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation).save(str(OUT / "icon.ico"), "ICO")
    big.save(str(OUT / "icon.icns"), "ICNS")
    for name in ("icon.png", "icon.ico", "icon.icns"):
        print(OUT / name, (OUT / name).stat().st_size, "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
