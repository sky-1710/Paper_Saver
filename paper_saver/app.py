"""Entry point: ``python run.py [paper.pdf]`` or ``python run.py --inspect paper.pdf [page]``."""

from __future__ import annotations

import sys
from pathlib import Path


def _inspect(args: list[str]) -> int:
    from .pdf.diagnose import inspect_paper

    if not args:
        print("usage: run.py --inspect paper.pdf [page ...]  (pages are 1-based)")
        return 2
    pages = [int(p) - 1 for p in args[1:]] or None
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(inspect_paper(args[0], pages))
    return 0


def _selftest(args: list[str]) -> int:
    """Check a built application can load everything it needs, then exit.

    A windowed build has no console, so the report goes to a file:
    ``"Paper Saver.exe" --selftest report.txt``. Used after every build.
    """
    import os
    import traceback

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    lines: list[str] = []
    code = 0
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication(sys.argv[:1])
        import pymupdf

        from .pdf.layout_model import model_available
        from .render.fonts import FontBook
        from .translate.codex_cli import find_codex

        fonts = FontBook()
        lines.append(f"python {sys.version.split()[0]} frozen={getattr(sys, 'frozen', False)}")
        lines.append(f"pymupdf {pymupdf.VersionBind}")
        lines.append(f"layout model: {model_available()}")
        lines.append(f"fonts: {fonts.serif_family} / {fonts.sans_family}")
        lines.append(f"codex: {find_codex()}")
        lines.append(f"icon: {icon_path()} exists={icon_path().is_file()}")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "Self test. The model reads this sentence.", fontsize=11)
        from .pdf.extract import extract_paper

        layout = extract_paper(doc)
        lines.append(f"extract: {layout.sentence_count()} sentence(s)")
        del app
    except Exception:
        lines.append(traceback.format_exc())
        code = 1
    report = "\n".join(lines) + "\n"
    if args:
        from pathlib import Path as _Path

        _Path(args[0]).write_text(report, encoding="utf-8")
    else:
        print(report)
    return code


def icon_path() -> Path:
    return Path(__file__).resolve().parent / "resources" / ("icon.ico" if sys.platform == "win32" else "icon.png")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--inspect":
        return _inspect(args[1:])
    if args and args[0] == "--selftest":
        return _selftest(args[1:])

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from .config import APP_DISPLAY_NAME, APP_NAME, Settings
    from .logging_setup import configure_logging
    from .ui.main_window import MainWindow
    from .ui.theme import STYLE

    configure_logging()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setStyleSheet(STYLE)
    from PySide6.QtGui import QIcon

    app.setWindowIcon(QIcon(str(icon_path())))

    window = MainWindow(Settings())
    window.show()
    pdfs = [Path(a) for a in args if a.lower().endswith(".pdf") and Path(a).is_file()]
    if pdfs:
        window.open_pdf(pdfs[0])
    return app.exec()
