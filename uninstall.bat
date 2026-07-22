@echo off
REM ============================================================
REM  face-pixelate-cam : uninstaller
REM   1) Removes every generated file (.venv, settings.json, all
REM      logs, Python caches, PyInstaller build output) -- resets
REM      the folder to just the source you downloaded.
REM   2) Offers to remove update .zip(s) the auto-updater saved to
REM      your Downloads folder (the only thing this app ever writes
REM      OUTSIDE its own folder).
REM   3) Optionally deletes the ENTIRE app folder (self-delete).
REM
REM  This app is portable: it makes NO registry entries, NO Start-
REM  menu / Program Files entries, and installs NO driver. So the
REM  three steps below remove 100%% of its footprint.
REM
REM  It does NOT remove the OBS / Streamlabs Virtual Camera driver.
REM  That belongs to Streamlabs/OBS and other apps may rely on it --
REM  uninstall it from Streamlabs/OBS itself if you want it gone.
REM ============================================================
setlocal enableextensions enabledelayedexpansion
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
cd /d "%APPDIR%"

echo.
echo === face-pixelate-cam uninstaller ===
echo Folder: %APPDIR%
echo.
echo Step 1: removing generated files (source files are kept).
echo         The virtual camera driver is NOT touched.
echo.

REM --- generated folders -------------------------------------------------
call :rmdir_safe ".venv"
call :rmdir_safe "__pycache__"
call :rmdir_safe "build"
call :rmdir_safe "dist"

REM --- generated files ---------------------------------------------------
call :del_safe "settings.json"
call :del_safe "run-log.txt"
call :del_safe "setup-log.txt"
call :del_safe "diagnose-log.txt"
del /f /q *.spec >nul 2>nul
del /f /q *.pyc  >nul 2>nul

echo.
echo Cleanup done. Source files remain (run setup.bat to reinstall).
echo.

REM --- Step 2: update downloads in the Downloads folder ------------------
set "DL=%USERPROFILE%\Downloads"
if exist "%DL%\face-pixelate-cam-*.zip" (
    echo Step 2: the auto-updater left update file^(s^) in your Downloads:
    for %%F in ("%DL%\face-pixelate-cam-*.zip") do echo        %%~nxF
    set "DELZIP="
    set /p "DELZIP=Delete these update .zip file(s) from Downloads? (y/N): "
    if /i "!DELZIP!"=="y" (
        del /f /q "%DL%\face-pixelate-cam-*.zip" >nul 2>nul
        echo   removed update .zip^(s^) from Downloads
    ) else (
        echo   left them in Downloads.
    )
    echo.
)

REM --- Step 3: optional full removal -------------------------------------
set "CONFIRM="
set /p "CONFIRM=Also DELETE the entire folder and all source files? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo.
    echo Kept the folder. Uninstall complete.
    pause
    exit /b 0
)

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
    echo   removed %~1\
)
exit /b 0

:del_safe
if exist %1 (
    del /f /q %1
    echo   removed %~1
)
exit /b 0
