@echo off
chcp 65001 >nul 2>&1
REM ============================================================
REM EDINET shareholder watch (yuho/hanki major shareholders) - Task Scheduler
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"

REM Create logs directory
if not exist "logs" mkdir "logs"

REM Get date string via PowerShell for reliable YYYYMMDD
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set YYYYMMDD=%%d

set LOGFILE=logs\edinet_shareholder_watch_%YYYYMMDD%.log

echo [%DATE% %TIME%] ==== START edinet_shareholder_watch ==== >> "%LOGFILE%" 2>&1
.venv\Scripts\python.exe scripts\edinet_shareholder_watch.py --once >> "%LOGFILE%" 2>&1
set EXIT_CODE=%ERRORLEVEL%
echo [%DATE% %TIME%] ==== END exit_code=%EXIT_CODE% ==== >> "%LOGFILE%" 2>&1

exit /b %EXIT_CODE%
