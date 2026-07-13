@echo off
REM ============================================================
REM  face-pixelate-cam : one-time setup
REM  Creates a local virtual environment and installs everything.
REM  Run this ONCE on your personal PC. Then use run.bat.
REM
REM  Works on Python 3.9 - 3.14 (face detection uses OpenCV YuNet,
REM  which has prebuilt wheels for all of these -- no compiler,
REM  no MediaPipe, no version juggling).
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo === face-pixelate-cam setup ===
echo.

REM Find a Python launcher (any modern Python 3 is fine).
where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PY=python"
    ) else (
        echo ERROR: Python was not found on this PC.
        echo Install Python 3.9 or newer from https://www.python.org/downloads/
        echo Tick "Add Python to PATH" during install, then re-run setup.bat.
        pause
        exit /b 1
    )
)

REM Sanity-check it is Python 3.9+.
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version_info.major)" 2^>nul') do set "PYMAJOR=%%v"
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version_info.minor)" 2^>nul') do set "PYMINOR=%%v"
if not "%PYMAJOR%"=="3" goto :badver
if "%PYMINOR%"=="" goto :badver
if %PYMINOR% LSS 9 goto :badver

echo Using: %PY% (Python %PYMAJOR%.%PYMINOR%)
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
    echo ERROR: dependency install failed. Scroll up for the pip error.
    echo If pip could not find a wheel, make sure you are on 64-bit Python.
    pause
    exit /b 1
)

echo.
echo === Setup complete! ===
echo Now run:  run.bat
echo.
pause
exit /b 0

:badver
echo.
echo ERROR: Python %PYMAJOR%.%PYMINOR% is too old. This app needs Python 3.9 or
echo newer (3.13 / 3.14 are fully supported). Install a newer Python from
echo https://www.python.org/downloads/ and re-run setup.bat.
pause
exit /b 1
