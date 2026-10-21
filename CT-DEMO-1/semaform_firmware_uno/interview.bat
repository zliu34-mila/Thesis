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
echo Interviewing semaform (no servos needed). This takes a few minutes...
%PY% interview.py %*
pause
