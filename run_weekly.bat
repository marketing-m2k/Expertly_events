@echo off
REM Weekly scrape entry point for Windows Task Scheduler.
REM Logs stdout/stderr to logs\ so a failed overnight run can be diagnosed
REM later instead of vanishing silently.

cd /d "%~dp0"
if not exist logs mkdir logs

set LOGFILE=logs\run_%date:~-4,4%-%date:~-10,2%-%date:~-7,2%.log

echo Run started: %date% %time% > "%LOGFILE%"
python scheduled_run.py >> "%LOGFILE%" 2>&1
echo Run finished: %date% %time% with exit code %errorlevel% >> "%LOGFILE%"
