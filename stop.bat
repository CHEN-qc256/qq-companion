@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo   Stopping qq-companion ...
echo.

where python >nul 2>nul
if errorlevel 1 goto nopython

python main.py --stop
echo.
pause
exit /b 0

:nopython
echo [X] python not found, cannot read the pid file.
echo     Stop it manually: Task Manager, end the python.exe running main.py.
echo.
pause
exit /b 1
