@echo off
rem Stops the background BoolBot process started by start.bat.
cd /d "%~dp0"
if not exist boolbot.pid (
    echo BoolBot is not running ^(no boolbot.pid^).
    exit /b 0
)
set /p BOOLBOT_PID=<boolbot.pid
taskkill /PID %BOOLBOT_PID% /T /F
del boolbot.pid
