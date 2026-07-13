@echo off
REM ============================================================
REM  face-pixelate-cam : one-time setup
REM  Creates a local virtual environment and installs everything.
REM  Run this ONCE on your personal PC. Then use run.bat.
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo === face-pixelate-cam setup ===
echo.

REM Find a Python launcher.
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo ERROR: Python was not found on this PC.
        echo Install Python 3.10-3.12 from https://www.python.org/downloads/
        echo Make sure to tick "Add Python to PATH" during install.
        pause
        exit /b 1
    )
)

echo Using: %PY%
echo Creating virtual environment in .venv ...
%PY% -m venv .venv
if %errorlevel% neq 0 (
    echo ERROR: failed to create the virtual environment.
    pause
    exit /b 1
)

echo Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo Installing dependencies (this can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: dependency install failed. Scroll up for details.
    pause
    exit /b 1
)

echo.
echo === Setup complete! ===
echo Now run:  run.bat
echo.
pause
