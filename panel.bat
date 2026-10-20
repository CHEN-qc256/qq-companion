@echo off
rem ===================================================================
rem  qq-companion management panel -- one-click launcher
rem
rem  NOTE: this file must stay PURE ASCII and use CRLF line endings.
rem  cmd.exe mis-parses LF-only .bat files (it starts executing from the
rem  middle of a line, so "python" becomes "thon"), and non-ASCII bytes
rem  get mangled because cmd reads the file as GBK. All Chinese UI text
rem  lives in Python instead.
rem
rem  Usage:  panel.bat            (port 8899)
rem          panel.bat 9000       (custom port)
rem ===================================================================

chcp 65001 >nul
cd /d "%~dp0"

echo.
echo   qq-companion  management panel
echo   ==========================================
echo.

where python >nul 2>&1
if errorlevel 1 goto nopython

echo   [1/3] checking python dependencies ...
python -c "import yaml, httpx, websockets" >nul 2>&1
if errorlevel 1 (
    echo         missing packages, installing from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 goto failed
) else (
    echo         all present.
)

echo.
echo   [2/3] checking config.yaml ...
if exist "config.yaml" goto havecfg
echo         not found, running the setup wizard first ...
echo.
python setup.py
if errorlevel 1 goto failed

:havecfg
echo         ok.

set "PANELPORT=8899"
if not "%~1"=="" set "PANELPORT=%~1"

echo.
echo   [3/3] starting panel ...
echo.
echo   Browser will open automatically.
echo   Address : http://127.0.0.1:%PANELPORT%/
echo   Stop    : press Ctrl+C in this window
echo.
echo   Closing the panel does NOT stop the bot.
echo.

python -m panel --port %PANELPORT%
goto end

:nopython
echo   [X] python was not found in PATH.
echo.
echo       Install Python 3.10 or newer from https://www.python.org/downloads/
echo       and tick "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1

:failed
echo.
echo   [X] Something went wrong. Please screenshot the messages above.
echo.
pause
exit /b 1

:end
echo.
echo   Panel stopped.
echo.
pause
