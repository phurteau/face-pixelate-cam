@echo off
REM ============================================================
REM  face-pixelate-cam : launch the app
REM  Opens the app window (video + a corner menu for all controls).
REM  Runs windowless (no black console) using the local .venv.
REM  Start the virtual camera from the panel when you want it.
REM  You can pass extra options, e.g.:  run.bat --camera 1 --mirror
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
REM black command window stays open while the app runs live.
start "" ".venv\Scripts\pythonw.exe" pixelate_cam.py %*
exit /b 0
