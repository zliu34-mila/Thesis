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
echo [1/3] Installing Python packages...
%PY% -m pip install ollama pyserial
echo.
echo [2/3] Downloading base model qwen2.5:7b (about 4.7 GB, please wait)...
ollama pull qwen2.5:7b
echo.
echo [3/3] Creating the semaform model...
ollama create semaform -f Modelfile
echo.
ollama list
echo.
echo Done. If you see "semaform" in the list above, setup worked.
pause