@echo off
REM ============================================================
REM  face-pixelate-cam : CLEAN mode for OBS/Streamlabs Window Capture
REM  Opens a bare pixelated-video window (no button, no overlay) that
REM  you capture with a "Window Capture" source. No virtual camera needed.
REM  Extra options still work, e.g.:  run-clean.bat --camera 1 --mirror
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" pixelate_cam.py --clean --no-vcam %*

if %errorlevel% neq 0 (
    echo.
    echo The app exited with an error. See messages above.
    pause
)
