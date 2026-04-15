@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
REM ============================================================
REM EDINET large holder watch (Task Scheduler)
REM   - mkdir lockdir for exclusive execution
REM   - daily log with START / END / exit_code
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"

REM --- Ensure directories ---
if not exist "logs" mkdir "logs"
if not exist "tmp" mkdir "tmp"

REM --- Date stamp ---
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "YYYYMMDD=%%d"
set "LOGFILE=logs\edinet_large_holder_watch_!YYYYMMDD!.log"

REM --- Lock (mkdir is atomic on Windows) ---
set "LOCKDIR=tmp\edinet_large_holder_watch.lockdir"
mkdir "!LOCKDIR!" 2>nul
if errorlevel 1 (
    echo [%DATE% %TIME%] LOCK_SKIP: another instance is running >> "!LOGFILE!"
    exit /b 0
)

REM --- Execute ---
echo [%DATE% %TIME%] ==== START edinet_large_holder_watch ==== >> "!LOGFILE!" 2>&1
.venv\Scripts\python.exe scripts\edinet_large_holder_watch.py --once >> "!LOGFILE!" 2>&1
set "EXIT_CODE=!ERRORLEVEL!"
echo [%DATE% %TIME%] ==== END exit_code=!EXIT_CODE! ==== >> "!LOGFILE!" 2>&1

REM --- Release lock ---
rmdir "!LOCKDIR!" 2>nul

exit /b !EXIT_CODE!
