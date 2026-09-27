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
echo Re-creating semaform from Modelfile...
ollama create semaform -f Modelfile
if errorlevel 1 (
  echo.
  echo ollama create failed: check the MESSAGE lines you added last in Modelfile.
  pause
  exit /b
)
echo.
echo Scoring the model with test_cases.csv...
%PY% evaluate.py %*
pause