@echo off
setlocal
REM ============================================================
REM run_backfill_segments_v4_recent.bat
REM V4 segment backfill (last 14 days)
REM Schedule: daily 18:10 (before TDNET_Reconcile at 18:35)
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"

REM -- Date calculation via PowerShell --
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).AddDays(-14).ToString('yyyy-MM-dd')"') do set DATE_FROM=%%i
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd')"') do set LOG_DATE=%%i

if not defined DATE_FROM (
    echo [ERROR] DATE_FROM calculation failed >> logs\v4_backfill_recent_error.log
    exit /b 1
)

if not exist logs mkdir logs

set LOGFILE=logs\v4_backfill_recent_%LOG_DATE%.log
set PYTHON=.venv\Scripts\python.exe

echo ===BAT_START=== [%date% %time%] date_from=%DATE_FROM% >> %LOGFILE%

%PYTHON% tools\backfill_segments_tdnet.py ^
    --reset-target ^
    --date-from %DATE_FROM% ^
    --worker-version v4 ^
    --decision-db .\decision_db.db >> %LOGFILE% 2>&1

set EXIT_CODE=%ERRORLEVEL%
echo ===BAT_END=== [%date% %time%] exit=%EXIT_CODE% >> %LOGFILE%

exit /b %EXIT_CODE%
