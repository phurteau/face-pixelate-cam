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

REM Bundle the YuNet model next to the app inside the exe (--add-data), and
REM pull in OpenCV's data files. No MediaPipe anymore.
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onefile ^
  --name face-pixelate-cam ^
  --collect-data cv2 ^
  --hidden-import pyvirtualcam ^
  --add-data "face_detection_yunet_2023mar.onnx;." ^
  pixelate_cam.py

echo.
if exist "dist\face-pixelate-cam.exe" (
    echo === BUILD OK ===
    echo EXE is at:  dist\face-pixelate-cam.exe
    echo Copy that single file to any Windows PC that has the
    echo OBS/Streamlabs Virtual Camera driver installed.
    echo (The YuNet model is bundled inside the exe.)
) else (
    echo === BUILD FAILED === see messages above.
)
pause
