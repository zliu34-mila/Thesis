@echo off
cd /d "%~dp0"
set "PY="
python --version >nul 2>nul && set "PY=python"
if not defined PY (
  py --version >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo Python was not found or is broken.
  echo Install Python from python.org, tick "Add python.exe to PATH",
  echo and choose an install folder with no Chinese characters, e.g. D:\Python312
  pause
  exit /b
)
echo Using Python: %PY%
echo Close the Arduino Serial Monitor first.
set /p PORT=Enter the COM port (e.g. COM3), or press Enter to run without hardware: 
if "%PORT%"=="" (
  %PY% agent.py --dry-run
) else (
  %PY% agent.py --port %PORT%
)
pause