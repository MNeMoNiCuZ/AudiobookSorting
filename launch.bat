@echo off
REM ---------------------------------------------------------------------------
REM  Launch AudiobookOrganizer from source.
REM
REM  Run it from anywhere - it works out its own directory. If a venv\ exists it is
REM  used, otherwise whatever python is on PATH, so a checkout without a venv still
REM  starts rather than failing with a path that means nothing to the reader.
REM
REM  Arguments are passed straight through, so this also drives the CLI:
REM      launch.bat --scan
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set "PY=venv\Scripts\python.exe"
) else (
    echo No venv found - falling back to the python on PATH.
    echo Run venv_create.bat first if you want the project's own environment.
    set "PY=python"
)

"%PY%" main.py %*
set "CODE=%ERRORLEVEL%"

REM  The window is usually started by double-clicking, where it would close before
REM  a traceback could be read. Only pause when something actually went wrong.
if not "%CODE%"=="0" (
    echo(
    echo Exited with code %CODE%.
    pause
)

exit /b %CODE%
