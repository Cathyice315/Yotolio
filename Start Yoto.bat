@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
py -3 -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0yoto_ui.py"
  if errorlevel 1 pause
  exit /b
)
python -c "import sys; sys.exit(sys.version_info < (3,11))" >nul 2>nul
if not errorlevel 1 (
  python "%~dp0yoto_ui.py"
  if errorlevel 1 pause
  exit /b
)
echo Python 3.11 or newer is required. Install Python, then double-click this launcher again.
start "" "https://www.python.org/downloads/windows/"
pause
