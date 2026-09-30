"""Start Paper Saver.

    python run.py                  open the window
    python run.py paper.pdf        open the window with a paper already loaded
"""

from __future__ import annotations

import sys

from paper_saver.app import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
