@echo off
rem DEV HELPER only. End users run WinLinuxTunnel.exe (the GUI starts the agent).
rem Crash-restart loop for agent development. The single-instance lock makes
rem a duplicate start a no-op. Do not register this file for autostart.
setlocal EnableExtensions
set "HERE=%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
    echo wlt: Python 3.8+ not found. Install Python, or run WinLinuxTunnel.exe. >&2
    exit /b 1
)
:loop
%PY% "%HERE%wlt_main.py" --agent
ping -n 4 127.0.0.1 >nul
goto loop
