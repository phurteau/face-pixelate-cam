@echo off
REM ============================================================
REM  face-pixelate-cam : CLEAN mode for OBS/Streamlabs Window Capture
REM  Opens a bare pixelated-video window (no button, no overlay) that
REM  you capture with a "Window Capture" source. No virtual camera needed.
REM  Runs windowless (no black console). Extra options still work,
REM  e.g.:  run-clean.bat --camera 1 --mirror
REM  Startup errors are shown in a popup and saved to run-log.txt.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

REM Launch with pythonw.exe (no console window) and exit this script so no
REM black command window stays open while you stream.
start "" ".venv\Scripts\pythonw.exe" pixelate_cam.py --clean --no-vcam %*
exit /b 0
