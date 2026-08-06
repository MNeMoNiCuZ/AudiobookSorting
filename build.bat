@echo off
REM ---------------------------------------------------------------------------
REM  Build AudiobookOrganizer.exe into the project root.
REM
REM  Everything intermediate goes into build\ and dist\, which .gitignore already
REM  excludes; the finished executable is copied up to the root next to main.py,
REM  because that is where you go looking for it.
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

echo(
echo === Checking the build tools ===
"%PY%" -m pip install --quiet --upgrade pyinstaller pillow
if errorlevel 1 (
    echo Could not install PyInstaller / Pillow. Build stopped.
    exit /b 1
)

echo(
echo === Drawing the application icon ===
if not exist "build" mkdir "build"
"%PY%" -m scripts.gui.app_icon "build\audiobook_organizer.ico"
if errorlevel 1 (
    echo Could not generate the icon. Build stopped.
    exit /b 1
)

echo(
echo === Packaging ===
REM  --windowed          no console window behind the GUI
REM  --onefile           one .exe, nothing to install
REM  --icon              the .ico just drawn, embedded in the executable
REM  --collect-submodules  mutagen is imported lazily in places, so PyInstaller's
REM                        static analysis does not find all of it
REM  --paths             note the trailing dot in "%~dp0." - %~dp0 ends in a
REM                      backslash, which would escape the closing quote and swallow
REM                      the next argument
"%PY%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --windowed ^
    --name "AudiobookOrganizer" ^
    --icon "%~dp0build\audiobook_organizer.ico" ^
    --paths "%~dp0." ^
    --collect-submodules mutagen ^
    --hidden-import "scripts.gui.app_icon" ^
    --distpath "dist" ^
    --workpath "build\pyinstaller" ^
    --specpath "build" ^
    main.py
if errorlevel 1 (
    echo Packaging failed.
    exit /b 1
)

echo(
echo === Copying the executable to the project root ===
copy /y "dist\AudiobookOrganizer.exe" "AudiobookOrganizer.exe" >nul
if errorlevel 1 (
    echo Could not copy the executable to the root.
    exit /b 1
)

echo(
echo Built AudiobookOrganizer.exe
echo   Intermediates are in build\ and dist\ - both are gitignored and can be deleted.
echo(
endlocal
