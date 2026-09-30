"""Logging to a rotating file in the app data folder, and to stderr."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import log_file


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file(), maxBytes=2_000_000, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except OSError:
        pass
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(logging.WARNING)
    root.addHandler(console)
    # PyMuPDF prints advice about an optional package on every table search.
    logging.getLogger("pymupdf").setLevel(logging.ERROR)
