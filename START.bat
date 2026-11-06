@echo off
cd /d "%~dp0"
echo Starting Cardiac Monitor on localhost...
python agent.py
if errorlevel 1 (
  echo.
  echo Python not found? Install it from https://www.python.org/downloads/ and re-run.
  pause
)
