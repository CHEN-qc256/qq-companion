@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo ==================================================
echo   qq-companion
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

if not exist "config.yaml" goto firstrun
goto check

:firstrun
echo [!] No config.yaml yet - running the setup wizard.
echo.
python setup.py
if errorlevel 1 goto failed
if not exist "config.yaml" goto failed
echo.

:check
echo [1/2] health check
echo.
python doctor.py
echo.
python main.py --check
if errorlevel 1 goto failed

echo.
echo [2/2] starting. DO NOT CLOSE THIS WINDOW.
echo       To stop: press Ctrl+C, or double-click stop.bat
echo.
python main.py
echo.
echo Program exited.
pause
exit /b 0

:nopython
echo [X] python not found.
echo     Install it from python.org and tick "Add python.exe to PATH".
echo.
pause
exit /b 1

:failed
echo.
echo [X] Something above is broken. Fix it before starting.
echo     If you are stuck, screenshot this whole window.
echo.
pause
exit /b 1
