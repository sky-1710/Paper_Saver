@echo off
rem Paper Saver - Windows launcher.
rem The first run creates a private Python environment and installs the
rem dependencies into it. The environment lives in %LOCALAPPDATA%\PaperSaver\venv
rem rather than next to this file: PySide6 contains files nested so deeply that
rem inside a long folder path they exceed Windows' 260-character limit and the
rem install fails. It is reinstalled whenever requirements.txt changes.
chcp 65001 >nul
cd /d "%~dp0"
set "VENV=%LOCALAPPDATA%\PaperSaver\venv"

if not exist "%VENV%\Scripts\pythonw.exe" goto install
fc /b requirements.txt "%VENV%\requirements.txt" >nul 2>&1 || goto install
goto run

:install
set "PY="
for %%V in (3.13 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
  start "" "https://www.python.org/downloads/windows/"
  pause
  exit /b 1
)

echo 처음 실행: 필요한 패키지를 설치합니다. 몇 분 걸립니다...
if exist "%VENV%" rmdir /s /q "%VENV%"
%PY% -m venv "%VENV%" || goto failed
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip || goto failed
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto failed
copy /y requirements.txt "%VENV%\requirements.txt" >nul

:run
start "" "%VENV%\Scripts\pythonw.exe" run.py %*
exit /b 0

:failed
echo.
echo 설치에 실패했습니다. 위 메시지를 확인하세요.
if exist "%VENV%" rmdir /s /q "%VENV%"
pause
exit /b 1
