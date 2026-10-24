@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 tools\continue_selfplay.py
pause
