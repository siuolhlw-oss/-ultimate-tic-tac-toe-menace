@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 tools\extend_compact_selfplay.py
pause
