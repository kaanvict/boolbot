@echo off
rem Starts BoolBot in the background with no console window.
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "%~dp0bot.py"
) else (
    start "" pythonw "%~dp0bot.py"
)
