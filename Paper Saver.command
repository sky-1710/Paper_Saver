#!/bin/bash
# Paper Saver - macOS launcher. Double-click in Finder (first time: right-click > Open).
# The first run creates a private Python environment next to this file and
# installs the dependencies into it (a few minutes, needs internet once).

cd "$(dirname "$0")" || exit 1
# Finder starts this without the shell's PATH, so Homebrew's folders are added.
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Python 3.10 이상이 필요합니다 (macOS 기본 python3는 3.9라 사용할 수 없습니다)."
  echo "python.org에서 설치하거나, Homebrew가 있다면: brew install python@3.12"
  osascript -e 'display dialog "Paper Saver를 실행하려면 Python 3.10 이상이 필요합니다.\n\npython.org에서 macOS용 Python을 설치한 뒤 다시 실행해 주세요." buttons {"python.org 열기"} default button 1' >/dev/null 2>&1 \
    && open "https://www.python.org/downloads/macos/"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "처음 실행: 필요한 패키지를 설치합니다 ($("$PY" --version))..."
  "$PY" -m venv .venv &&
    .venv/bin/python -m pip install --upgrade pip &&
    .venv/bin/python -m pip install -r requirements.txt
  if [ $? -ne 0 ]; then
    echo ""
    echo "설치에 실패했습니다. 위 메시지를 확인하고, .venv 폴더를 지운 뒤 다시 실행하세요."
    read -r -p "Enter 키를 누르면 닫힙니다."
    rm -rf .venv
    exit 1
  fi
fi

.venv/bin/python run.py "$@" >/dev/null 2>&1 &
disown
exit 0
