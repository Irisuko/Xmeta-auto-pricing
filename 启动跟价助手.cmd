@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto failed
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" launcher.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Failed to start. Please send the error text for help.
pause
exit /b 1
