@echo off
REM ===========================================================
REM  Install the elevated "NapCatQQ" scheduled task.
REM
REM  Why a scheduled task: injecting NapCat into QQ needs
REM  administrator rights. Running start-napcat.bat at logon
REM  would pop a UAC dialog every single time. A task with
REM  RunLevel=HighestAvailable is the only way to get the
REM  privilege without the prompt -- but creating it needs
REM  elevation once, which is what this file asks for.
REM
REM  After it succeeds, boot.py triggers the task with
REM  "schtasks /run /tn NapCatQQ" and nothing pops up.
REM ===========================================================
chcp 65001 >nul
cd /d "%~dp0"

net session >nul 2>&1
if not errorlevel 1 goto run

echo Requesting administrator rights (one time only)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 0

:run
echo Administrator mode detected.
python -m app.napcat install-task
echo.
timeout /t 20
