# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Paper Saver. Build with:  python tools/build.py
#
# One folder, not one file: a one-file build unpacks ~250 MB of Qt and ONNX
# into a temp folder on every launch, which is slow and trips antivirus
# scanners. On macOS the folder is wrapped as "Paper Saver.app".

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))
from paper_saver import __version__  # noqa: E402

RES = ROOT / "paper_saver" / "resources"
datas = [(str(RES), "paper_saver/resources")]
binaries = []
# pymupdf.layout is imported lazily (layout_model.py) and ships its ONNX
# model as package data, so both have to be named explicitly.
hiddenimports = ["pymupdf.layout", *collect_submodules("pymupdf.layout")]
for package in ("pymupdf",):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Qt modules the application never uses; leaving them out saves ~100 MB.
excludes = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtXml",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtNetwork", "PySide6.QtDBus",
    "PySide6.QtConcurrent", "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtUiTools",
    "tkinter", "pytest", "ruff",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

mac = sys.platform == "darwin"
icon = str(RES / ("icon.icns" if mac else "icon.ico"))

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Paper Saver",
    console=False,
    icon=icon,
    # Lets a PDF dropped on the app's icon in the Dock arrive as an argument.
    argv_emulation=mac,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Paper Saver")

if mac:
    app = BUNDLE(
        coll,
        name="Paper Saver.app",
        icon=icon,
        bundle_identifier="app.papersaver",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "PDF",
                    "CFBundleTypeRole": "Viewer",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                }
            ],
        },
    )
