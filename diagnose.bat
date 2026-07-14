@echo off
REM ============================================================
REM  face-pixelate-cam : virtual-camera diagnostic
REM  Runs diagnose.py and saves the output to diagnose-log.txt.
REM  Use this only if you want the VIRTUAL CAMERA path to work.
REM  (For Window Capture you don't need the virtual camera at all
REM   -- just use run-clean.bat.)
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" diagnose.py > "%~dp0diagnose-log.txt" 2>&1
type "%~dp0diagnose-log.txt"
echo.
echo (This output was also saved to diagnose-log.txt)
pause
