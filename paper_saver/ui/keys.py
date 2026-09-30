"""Shortcuts shared by several panels, and how to write them for the reader.

Qt maps Ctrl to Cmd on a Mac by itself, so a shortcut is defined once; only
the text that tells the reader which keys to press differs per platform.
"""

from __future__ import annotations

import sys

__all__ = ["CAPTURE_KEY", "CAPTURE_LABEL", "PASTE_LABEL"]

MAC = sys.platform == "darwin"

CAPTURE_KEY = "Ctrl+Shift+X"
CAPTURE_LABEL = "⌘⇧X" if MAC else "Ctrl+Shift+X"
PASTE_LABEL = "⌘V" if MAC else "Ctrl+V"
