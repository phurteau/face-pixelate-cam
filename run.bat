@echo off
REM ============================================================
REM  face-pixelate-cam : launch the filter + virtual camera
REM  Runs the app using the local .venv created by setup.bat.
REM  You can pass extra options, e.g.:  run.bat --camera 1 --mirror
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" pixelate_cam.py %*

if %errorlevel% neq 0 (
    echo.
    echo The app exited with an error. See messages above.
    pause
)
