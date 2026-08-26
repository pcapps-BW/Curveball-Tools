@echo off
REM Double-click this file to launch the Curveball Tools web app.
REM Opens the browser automatically after a short delay, then runs the
REM server in this window -- leave this window open while you use the site.
REM Close this window (or Ctrl+C) to stop the server.

cd /d "%~dp0"

set PYCMD=

where python >nul 2>nul
if not errorlevel 1 (
    set PYCMD=python
)

if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        set PYCMD=py
    )
)

if not defined PYCMD (
    echo.
    echo  Could not find "python" or "py" on this machine's PATH.
    echo  Install Python from https://www.python.org/downloads/
    echo  and check "Add python.exe to PATH" during setup, then
    echo  double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Using: %PYCMD%
echo Installing/updating required packages...
%PYCMD% -m pip install -r requirements.txt --quiet

REM Open the browser ~3s after the server starts, in the background.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"

echo.
echo Starting Curveball Tools -- leave this window open.
echo.
%PYCMD% app.py

echo.
echo Server stopped.
pause
