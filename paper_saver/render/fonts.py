"""Korean faces for the translated page, and their measurements.

The face follows the original: a serif paragraph becomes Myeongjo (the Korean
serif), a sans-serif heading becomes Gothic. That one decision is most of what
makes the translated page look like the same paper rather than a transcript
of it.

Widths are measured once, at 100pt, and scaled linearly - outline fonts scale
exactly, hinting is switched off so they really do, and a paragraph being
fitted is measured at up to sixteen sizes, so measuring each string once
rather than sixteen times matters.

Point sizes are made to mean points: text is painted on a surface declared as
72 DPI, where one point is one unit, and scaled by the zoom afterwards. On a
default 96-DPI surface a 9.5pt font would come out a third too large and not a
single paragraph would fit.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtGui import QFont, QFontDatabase, QFontMetricsF, QImage

__all__ = ["DPI72_DOTS_PER_METER", "FontBook", "QtMetrics", "resolve_family"]

#: 72 dots per inch, in the unit QImage wants.
DPI72_DOTS_PER_METER = round(72 / 0.0254)
#: Size at which widths are measured before scaling.
_REFERENCE = 100.0

SERIF_FAMILIES = (
    "HCR Batang", "함초롬바탕", "Noto Serif KR", "NanumMyeongjo", "나눔명조",
    "Batang", "바탕", "AppleMyungjo",
)
SANS_FAMILIES = (
    "Noto Sans KR", "Malgun Gothic", "맑은 고딕", "HCR Dotum", "함초롬돋움",
    "NanumGothic", "나눔고딕", "Apple SD Gothic Neo", "Gulim", "굴림",
)


def _device() -> QImage:
    image = QImage(1, 1, QImage.Format.Format_RGB32)
    image.setDotsPerMeterX(DPI72_DOTS_PER_METER)
    image.setDotsPerMeterY(DPI72_DOTS_PER_METER)
    return image


#: Font files tried when Qt's database does not already list a Korean face.
#: Qt's offscreen platform - used for tests and for background rendering -
#: starts with an empty font database on Windows, and every page it painted
#: came out in boxes until these were registered by hand.
_KOREAN_FONT_FILES = (
    "HANBatang.ttf", "HANBatangB.ttf", "batang.ttc", "NotoSansKR-VF.ttf",
    "malgun.ttf", "malgunbd.ttf", "HANDotum.ttf", "HANDotumB.ttf", "gulim.ttc",
)


@lru_cache(maxsize=1)
def _register_system_fonts() -> None:
    import os
    import sys
    from pathlib import Path

    if sys.platform == "darwin":
        # macOS ships AppleMyungjo and Apple SD Gothic Neo, which Qt's own
        # database already lists; nothing to register.
        return
    folders = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    for folder in folders:
        for name in _KOREAN_FONT_FILES:
            path = folder / name
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))


@lru_cache(maxsize=8)
def resolve_family(serif: bool, preferred: str = "") -> str:
    """The first installed family from the preference list.

    ``preferred`` - the user's own choice from Settings - wins when installed.
    Falls back to whatever Qt picks for Korean, which on any Windows with
    Korean support is Malgun Gothic.
    """
    installed = set(QFontDatabase.families())
    if not installed.intersection(SERIF_FAMILIES + SANS_FAMILIES):
        _register_system_fonts()
        installed = set(QFontDatabase.families())
    if preferred and preferred in installed:
        return preferred
    for family in SERIF_FAMILIES if serif else SANS_FAMILIES:
        if family in installed:
            return family
    for family in SANS_FAMILIES:
        if family in installed:
            return family
    return QFont().defaultFamily()


class QtMetrics:
    """:class:`paper_saver.render.fit.Metrics` backed by a real font."""

    def __init__(self, family: str, *, bold: bool = False) -> None:
        self.family = family
        self.bold = bold
        self._font = self._make(_REFERENCE)
        self._metrics = QFontMetricsF(self._font, _device())
        self._widths: dict[str, float] = {}

    def _make(self, size: float) -> QFont:
        font = QFont(self.family)
        font.setPointSizeF(size)
        font.setBold(self.bold)
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        font.setKerning(True)
        font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        return font

    def width(self, text: str, size: float) -> float:
        cached = self._widths.get(text)
        if cached is None:
            cached = self._metrics.horizontalAdvance(text)
            self._widths[text] = cached
        return cached * size / _REFERENCE

    def font(self, size: float) -> QFont:
        """The face at ``size`` points, for painting on a 72-DPI surface."""
        return self._make(size)

    @property
    def reference_font(self) -> QFont:
        """The face at the size widths were measured at.

        Painting uses this, scaled down, rather than :meth:`font` at the real
        size. At a real size of 9.5pt the rasteriser rounds each glyph's
        advance to whole device pixels, so a word comes out a pixel or two
        wider than it was measured, and on a justified line those pixels eat
        the spaces: measured on a real page, "관심을 받아 왔다" was drawn as
        "관심을받아왔다". Drawn at the reference size and scaled, the advances
        are exactly the ones the layout used.
        """
        return self._font

    @property
    def reference_size(self) -> float:
        return _REFERENCE


class FontBook:
    """One :class:`QtMetrics` per face in use, shared across pages."""

    def __init__(self, serif_family: str = "", sans_family: str = "") -> None:
        self.serif_family = resolve_family(True, serif_family)
        self.sans_family = resolve_family(False, sans_family)
        self._cache: dict[tuple[bool, bool], QtMetrics] = {}

    def metrics(self, *, serif: bool, bold: bool) -> QtMetrics:
        key = (serif, bold)
        found = self._cache.get(key)
        if found is None:
            family = self.serif_family if serif else self.sans_family
            found = QtMetrics(family, bold=bold)
            self._cache[key] = found
        return found
