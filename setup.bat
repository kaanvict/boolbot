@echo off
rem One-time setup: installs what BoolBot needs, asks for the token, starts the bot.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it with:  winget install Python.Python.3.13
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 if not exist "%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*" (
    echo Installing FFmpeg...
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
)

where deno >nul 2>nul
if not errorlevel 1 goto js_ok
where node >nul 2>nul
if not errorlevel 1 goto js_ok
echo Installing Deno (yt-dlp needs it for YouTube)...
winget install -e --id DenoLand.Deno --accept-source-agreements --accept-package-agreements
:js_ok

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python environment...
    python -m venv .venv
)
echo Installing Python packages...
".venv\Scripts\python.exe" -m pip install -q -U -r requirements.txt
if errorlevel 1 (
    echo Package install failed. See the messages above.
    pause
    exit /b 1
)

if exist .env goto have_env
set /p BOOLBOT_TOKEN=Paste the Discord bot token and press Enter: 
>.env echo DISCORD_TOKEN=%BOOLBOT_TOKEN%
:have_env

echo Starting BoolBot in the background...
if exist boolbot.pid call stop.bat >nul 2>nul
explorer "%~dp0start.bat"
timeout /t 12 /nobreak >nul
echo.
echo ---- boolbot.log ----
if exist boolbot.log (type boolbot.log) else (echo No log yet. Run start.bat, then open boolbot.log.)
echo.
echo If you see "BoolBot online" above, it is running. You can close this window.
pause
