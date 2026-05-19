@echo off
REM ============================================================
REM  Local IPTV server launcher (Windows)
REM  Edit FOLDER and PORT below to match your setup, then either:
REM    1. Double-click this file to start the server in a window
REM    2. Put a shortcut to this file in the Startup folder to
REM       launch on login:  shell:startup
REM ============================================================

set "FOLDER=C:\iptv\movies"
set "PORT=8080"

REM Find python on PATH (try py launcher first, then python)
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

REM Run from the directory containing iptv_server.py
cd /d "%~dp0\.."

%PY% iptv_server.py --folder "%FOLDER%" --port %PORT%
pause
