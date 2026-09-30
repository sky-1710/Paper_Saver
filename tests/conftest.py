"""Shared fixtures: an offscreen Qt application and a synthetic two-column paper."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

LOREM = (
    "Graph neural networks have been applied to many problems. "
    "Zhong et al. proposed a regularized model in 2020, as shown in Fig. 3 of their paper. "
    "The adjacency matrix is learnable, i.e. it is updated during training. "
    "We report an accuracy of 79.37 percent on the SEED dataset. "
)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def two_column_pdf(tmp_path: Path) -> Path:
    """A letter-size page: a full-width title and abstract, then two columns."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(60, 50, 552, 90), "A Study of Synthetic Papers", fontsize=22, align=1)
    page.insert_textbox(pymupdf.Rect(80, 100, 532, 170), "Abstract. " + LOREM, fontsize=8)
    for index, x in enumerate((48, 318)):
        page.insert_textbox(
            pymupdf.Rect(x, 200, x + 246, 720), (LOREM * 5) + f" Column {index + 1} ends here.", fontsize=9.5
        )
    target = tmp_path / "synthetic.pdf"
    doc.save(str(target))
    doc.close()
    return target
