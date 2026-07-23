@echo off
REM ============================================================
REM  face-pixelate-cam : launch (kept for back-compat)
REM  Same as run.bat -- opens the app window. The old "clean" mode
REM  is gone: the app shows the video full-window and the controls
REM  live behind the corner menu, which auto-hides so a Window
REM  Capture stays clean. Extra options still work, e.g.:
REM     run-clean.bat --camera 1 --mirror
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" pixelate_cam.py %*
exit /b 0
