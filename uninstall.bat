@echo off
REM ============================================================
REM  face-pixelate-cam : uninstaller
REM   1) Removes generated files (.venv, settings.json, caches,
REM      build output) -- resets the folder to just the source.
REM   2) Optionally deletes the ENTIRE app folder (self-delete).
REM
REM  This does NOT remove the OBS / Streamlabs Virtual Camera
REM  driver. That belongs to Streamlabs/OBS and other apps may
REM  rely on it -- uninstall it from Streamlabs/OBS if you want.
REM ============================================================
setlocal enableextensions
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
cd /d "%APPDIR%"

echo.
echo === face-pixelate-cam uninstaller ===
echo Folder: %APPDIR%
echo.
echo Removing generated files (.venv, settings.json, __pycache__,
echo build, dist, *.spec). The virtual camera driver is NOT touched.
echo.

call :rmdir_safe ".venv"
call :rmdir_safe "__pycache__"
call :rmdir_safe "build"
call :rmdir_safe "dist"
if exist "settings.json" (
    del /f /q "settings.json"
    echo   removed settings.json
)
del /f /q *.spec >nul 2>nul

echo.
echo Cleanup done. Source files remain (run setup.bat to reinstall).
echo.
set "CONFIRM="
set /p "CONFIRM=Also DELETE the entire folder and all source files? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo.
    echo Kept the folder. Uninstall complete.
    pause
    exit /b 0
)

REM --- Full removal -----------------------------------------------------
REM The running .bat lives inside the folder we want to delete, so hand off
REM to a tiny helper in %TEMP% that waits for this script to exit (releasing
REM the file lock), deletes the whole folder, then deletes itself.
set "DELSCRIPT=%TEMP%\fpc_uninstall_%RANDOM%.bat"
> "%DELSCRIPT%" echo @echo off
>>"%DELSCRIPT%" echo timeout /t 1 /nobreak ^>nul
>>"%DELSCRIPT%" echo rmdir /s /q "%APPDIR%"
>>"%DELSCRIPT%" echo del /f /q "%%~f0"
cd /d "%APPDIR%\.."
echo.
echo Deleting the entire folder now...
start "" /min "%DELSCRIPT%"
exit /b 0

:rmdir_safe
if exist %1 (
    rmdir /s /q %1
    echo   removed %~1
)
exit /b 0
