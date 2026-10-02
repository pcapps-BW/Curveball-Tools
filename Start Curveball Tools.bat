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
REM NOT --quiet. This step enforces "paramiko<5" -- paramiko 5 removed the
REM SHA-1 algorithms the Cisco MUX requires, so a pip failure here silently
REM takes out the MUX and GSX hops at every site. If pip cannot downgrade
REM (no network, or paramiko installed machine-wide without admin rights),
REM that has to be visible rather than scrolling past behind --quiet.
%PYCMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ####################################################################
    echo   pip FAILED. Required package versions are NOT installed.
    echo.
    echo   If paramiko stays at 5.x the MUX and GSX hops will fail at every
    echo   site -- the app still starts and the 1250/1800 hops still work.
    echo.
    echo   Common causes: no network / proxy, or paramiko installed
    echo   machine-wide and this window is not elevated. Try:
    echo       %PYCMD% -m pip install --user -r requirements.txt
    echo  ####################################################################
    echo.
    pause
)

REM Show what actually ended up installed, so "I installed the fix" can be
REM checked rather than assumed.
%PYCMD% -c "import paramiko,sys;print('paramiko',paramiko.__version__,'|',sys.executable)"

REM Open the browser ~3s after the server starts, in the background.
start "" cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"

echo.
echo Starting Curveball Tools -- leave this window open.
echo.
%PYCMD% app.py

echo.
echo Server stopped.
pause
