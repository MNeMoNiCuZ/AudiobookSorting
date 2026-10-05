@echo off
REM ---------------------------------------------------------------------------
REM  Build AudiobookOrganizer.exe into the project root.
REM
REM  Everything intermediate goes into src\junk\, which .gitignore excludes; the finished
REM  executable is copied up to the root, because that is where you go looking for it.
REM
REM  Run it from anywhere - it works out its own directory. If a venv\ exists it is
REM  used, otherwise whatever python is on PATH.
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    set "PY=python"
)
set "PYTHONPATH=%~dp0src"
set "PYTHONPYCACHEPREFIX=%~dp0src\junk\pycache"

REM  Windows will not overwrite an executable that is running, so a build made while
REM  the program is open lands in dist\ and never reaches the root copy you start.
tasklist /fi "imagename eq AudiobookOrganizer.exe" /nh 2>nul | find /i "AudiobookOrganizer.exe" >nul
if not errorlevel 1 (
    echo(
    echo *** STOPPED: AudiobookOrganizer.exe is running. ***
    echo Close it, then build again - a running exe cannot be replaced.
    pause
    exit /b 1
)

echo(
echo === Checking the build tools ===
"%PY%" -m pip install --quiet --upgrade pyinstaller pillow
if errorlevel 1 (
    echo Could not install PyInstaller / Pillow. Build stopped.
    exit /b 1
)

echo(
echo === Drawing the application icon ===
if not exist "src\junk\build" mkdir "src\junk\build"
"%PY%" -m scripts.gui.app_icon "src\junk\build\audiobook_organizer.ico"
if errorlevel 1 (
    echo Could not generate the icon. Build stopped.
    exit /b 1
)

echo(
echo === Packaging ===
REM  What goes into the executable, and what is kept out of it, is in
REM  src\AudiobookOrganizer.spec.
"%PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --distpath "src\junk\dist" ^
    --workpath "src\junk\build\pyinstaller" ^
    src\AudiobookOrganizer.spec
if errorlevel 1 (
    echo Packaging failed.
    exit /b 1
)

echo(
echo === Copying the executable to the project root ===
REM  A freshly written exe is often held for a moment by antivirus scanning it, so a
REM  failed copy is retried before it is reported.
set "TRIES=0"
:copy_again
copy /y "src\junk\dist\AudiobookOrganizer.exe" "AudiobookOrganizer.exe" >nul 2>&1
if not errorlevel 1 goto copied
set /a TRIES+=1
if %TRIES% lss 10 (
    ping -n 3 127.0.0.1 >nul
    goto copy_again
)
echo(
echo *** FAILED: AudiobookOrganizer.exe in this folder was NOT replaced. ***
echo Something still has it open. The new build is in src\junk\dist\AudiobookOrganizer.exe.
pause
exit /b 1
:copied

echo(
echo Built AudiobookOrganizer.exe
echo   Intermediates are in src\junk\ - gitignored and safe to delete.
echo(
endlocal
