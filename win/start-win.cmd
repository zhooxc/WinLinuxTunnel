@echo off
rem wlx agent launcher with crash-restart loop (no admin, no service).
rem A native crash in a desktop op must not permanently kill the bridge:
rem this loop relaunches the agent after any exit. The agent's single-instance
rem lock makes a duplicate start a harmless no-op.
setlocal EnableExtensions
set "HERE=%~dp0"
set "PY="
if exist "%HERE%runtime\python.exe" set "PY=%HERE%runtime\python.exe"
if not defined PY (
    where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo wlx: no Python found ^(need runtime\python.exe or system python^) >&2
    exit /b 1
)
:loop
%PY% "%HERE%wlx_agent.py"
rem ~3s pause before restart (ping works without a console stdin, unlike timeout)
ping -n 4 127.0.0.1 >nul
goto loop