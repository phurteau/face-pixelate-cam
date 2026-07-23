@echo off
REM ============================================================
REM  Build a standalone Windows app with PyInstaller (one folder).
REM  Run AFTER setup.bat. Output: dist\face-pixelate-cam\
REM
REM  One-folder (--onedir), not one-file: on managed/corporate PCs
REM  a one-file exe is often blocked by Application Control because
REM  it unpacks unsigned DLLs to %TEMP%. The one-folder build keeps
REM  the DLLs next to the exe and runs. Zip the whole folder to share.
REM ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: .venv not found. Run setup.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install pyinstaller -q

REM --windowed: no console window (this is a GUI app).
REM --add-data bundles the YuNet model and the icon next to the app.
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onedir --windowed ^
  --name face-pixelate-cam ^
  --icon app.ico ^
  --collect-data cv2 ^
  --hidden-import pyvirtualcam ^
  --add-data "face_detection_yunet_2023mar.onnx;." ^
  --add-data "app.ico;." ^
  pixelate_cam.py

echo.
if exist "dist\face-pixelate-cam\face-pixelate-cam.exe" (
    echo === BUILD OK ===
    echo App folder: dist\face-pixelate-cam\
    echo Run it by double-clicking face-pixelate-cam.exe inside that folder.
    echo Zip the whole folder to share it. To use the virtual camera,
    echo the target PC needs OBS Studio installed. See the README.
) else (
    echo === BUILD FAILED === see messages above.
)
pause
