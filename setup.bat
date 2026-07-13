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

REM Remove any leftover/broken .venv from a previous failed run so we always
REM build a clean one (a half-created venv is a common cause of pip failures).
if exist ".venv" (
    echo Removing existing .venv for a clean install ...
    rmdir /s /q ".venv"
)

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
echo (full output is also saved to setup-log.txt)
".venv\Scripts\python.exe" -m pip install -r requirements.txt > "%~dp0setup-log.txt" 2>&1
set "PIPRESULT=%errorlevel%"
type "%~dp0setup-log.txt"
if %PIPRESULT% neq 0 (
    echo.
    echo ============================================================
    echo  ERROR: dependency install failed.
    echo  The FULL pip error is shown above and saved in:
    echo     %~dp0setup-log.txt
    echo  Share that file and the exact cause can be pinpointed.
    echo.
    echo  Common causes when requirements.txt is correct:
    echo   - Corporate/AV proxy or SSL inspection blocking PyPI
    echo   - No internet at install time
    echo   - 32-bit Python (this needs 64-bit)
    echo ============================================================
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
