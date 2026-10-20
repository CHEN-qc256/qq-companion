@echo off
rem Install the qq-companion autostart entry (HKCU Run key).
rem Usage: install_autostart.bat [path\to\pythonw.exe]
rem Prefer the Microsoft Store Python: on this machine the uv-managed
rem interpreter runs with a write-restricted token (cannot write HKCU or
rem TEMP, and children inherit it, which kills GPT-SoVITS).
setlocal
set "ROOT=%~dp0"
if not "%~1"=="" set "PYW=%~1"
if not "%~1"=="" goto havepw
set "PYW=%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe"
if exist "%PYW%" goto havepw
set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
if exist "%PYW%" goto havepw
for /f "delims=" %%i in ('where pythonw 2^>nul') do set "PYW=%%i"
:havepw
if not exist "%PYW%" goto nopyw
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v qq-companion /t REG_SZ /d "\"%PYW%\" \"%ROOT%boot.py\" --auto" /f
if errorlevel 1 goto fail
reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v qq-companion
echo.
echo OK: qq-companion will start at next logon with:
echo   %PYW%
goto end
:nopyw
echo ERROR: pythonw.exe not found. Install Python first, or pass its path.
goto end
:fail
echo ERROR: reg add failed. Run this file as the same user, not as admin.
:end
echo.
