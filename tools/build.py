"""Build the standalone application for this computer's OS.

    python tools/build.py            build, self-test, zip
    python tools/build.py --install  (Windows) also install for this user and
                                     make Desktop and Start-menu shortcuts

Output:
    Windows  dist/PaperSaver-<version>-windows.zip   ("Paper Saver/Paper Saver.exe")
    macOS    dist/PaperSaver-<version>-macos-<arm64|intel>.zip  ("Paper Saver.app")

The result needs no Python on the machine it runs on. A macOS app can only be
built on a Mac (PyInstaller does not cross-compile); see README, "직접 빌드".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paper_saver import __version__  # noqa: E402

DIST = ROOT / "dist"
BUILD = ROOT / "build"
MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"


def run(cmd: list[str]) -> None:
    print("$", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def build() -> Path:
    shutil.rmtree(BUILD, ignore_errors=True)
    for stale in (DIST / "Paper Saver", DIST / "Paper Saver.app"):
        shutil.rmtree(stale, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD), str(ROOT / "packaging" / "PaperSaver.spec")])
    return DIST / ("Paper Saver.app" if MAC else "Paper Saver")


def executable(app: Path) -> Path:
    if MAC:
        return app / "Contents" / "MacOS" / "Paper Saver"
    return app / ("Paper Saver.exe" if WINDOWS else "Paper Saver")


def selftest(app: Path) -> None:
    report = Path(tempfile.mkdtemp()) / "selftest.txt"
    result = subprocess.run([str(executable(app)), "--selftest", str(report)], timeout=180)
    text = report.read_text(encoding="utf-8") if report.is_file() else "(no report)"
    print(text)
    if result.returncode != 0 or "layout model: True" not in text:
        raise SystemExit("self-test failed")


def package(app: Path) -> Path:
    import platform

    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "intel"
    label = f"macos-{arch}" if MAC else "windows" if WINDOWS else "linux"
    target = DIST / f"PaperSaver-{__version__}-{label}.zip"
    target.unlink(missing_ok=True)
    if MAC:
        # ditto keeps the symlinks and signatures inside the Qt frameworks,
        # which Python's zipfile would flatten into copies (and break).
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(target)])
    else:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in sorted(app.rglob("*")):
                if path.is_file():
                    archive.write(path, (Path(app.name) / path.relative_to(app)).as_posix())
            archive.writestr(f"{app.name}/읽어보세요.txt", READ_ME)
    print(f"{target}  {target.stat().st_size / 1e6:.0f} MB")
    return target


READ_ME = """Paper Saver

실행: 이 폴더의 "Paper Saver.exe"를 더블클릭하세요. Python 설치는 필요 없습니다.
      (폴더째로 원하는 곳에 두고, exe를 우클릭 → 바탕 화면에 바로 가기 만들기)
      처음 실행 때 Windows SmartScreen이 "PC 보호" 창을 띄우면 [추가 정보] → [실행].

번역·사전: 이 PC의 Codex CLI에 로그인된 "본인" ChatGPT 계정을 사용합니다.
      1) npm install -g @openai/codex   (또는 VS Code에 ChatGPT 확장 설치)
      2) codex login
      다른 사람의 계정 정보는 이 프로그램에 들어 있지 않습니다.
"""


def install_windows(app: Path) -> None:
    """Copy to %LOCALAPPDATA%\\Programs and make shortcuts, like any per-user app."""
    target = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Paper Saver"
    # A running Paper Saver stops the install: deleting under it left the
    # install half gone (its exe cannot be deleted, the rest can). Windows
    # lets a running exe be renamed, not opened for writing.
    for exe in target.glob("*.exe"):
        try:
            exe.open("r+b").close()
        except PermissionError:
            raise SystemExit(
                f"Paper Saver가 실행 중이라 설치할 수 없습니다. 앱을 닫고 다시 실행하세요.\n({exe})"
            ) from None
    # Moved aside, not deleted in place, so a copy that fails puts it back.
    old = target.with_name("Paper Saver.old")
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    try:
        shutil.copytree(app, target)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        if old.exists():
            old.rename(target)
        raise
    shutil.rmtree(old, ignore_errors=True)
    exe = target / "Paper Saver.exe"
    script = f"""
$s = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {{
  $l = $s.CreateShortcut((Join-Path $dir 'Paper Saver.lnk'))
  $l.TargetPath = '{exe}'
  $l.WorkingDirectory = '{target}'
  $l.IconLocation = '{exe},0'
  $l.Description = '논문 원문과 레이아웃 그대로의 한국어 직역'
  $l.Save()
  Write-Output (Join-Path $dir 'Paper Saver.lnk')
}}
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    print(f"installed: {exe}")


def main() -> int:
    app = build()
    selftest(app)
    package(app)
    if "--install" in sys.argv and WINDOWS:
        install_windows(app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
