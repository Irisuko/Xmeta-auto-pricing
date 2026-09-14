@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" launcher.py --stop
if errorlevel 1 pause
