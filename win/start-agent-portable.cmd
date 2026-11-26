@echo off
rem Portable-mode agent launcher (embeddable runtime under win\runtime).
rem Normally not needed: the GUI starts the agent automatically.
rem Crash-restart loop; the single-instance lock makes duplicates a no-op.
setlocal
set "HERE=%~dp0"
:loop
"%HERE%runtime\python.exe" "%HERE%wlt_main.py" --agent
ping -n 4 127.0.0.1 >nul
goto loop