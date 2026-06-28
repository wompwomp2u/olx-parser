@echo off
REM Auto-restarting launcher for the OLX ThinkPad watcher.
REM If the script ever exits (crash, network outage), it waits 15s and restarts.
cd /d "%~dp0"

:loop
echo [%date% %time%] starting olx_parser...
python olx_parser.py
echo [%date% %time%] olx_parser exited, restarting in 15s...
timeout /t 15 /nobreak >nul
goto loop
