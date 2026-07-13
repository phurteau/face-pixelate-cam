@echo off
REM ============================================================
REM  Build a standalone one-file EXE with PyInstaller.
REM  Run AFTER setup.bat. Output: dist\face-pixelate-cam.exe
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install pyinstaller -q

".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onefile ^
  --name face-pixelate-cam ^
  --collect-all mediapipe ^
  --collect-data cv2 ^
  --hidden-import pyvirtualcam ^
  pixelate_cam.py

echo.
if exist "dist\face-pixelate-cam.exe" (
    echo === BUILD OK ===
    echo EXE is at:  dist\face-pixelate-cam.exe
    echo Copy that single file to any Windows PC that has the
    echo OBS/Streamlabs Virtual Camera driver installed.
) else (
    echo === BUILD FAILED === see messages above.
)
pause
