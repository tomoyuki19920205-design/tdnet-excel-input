@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  run_update_all.bat - TDnet one-click update (8 steps)
REM  Architecture: TDNET -> SQLite -> Supabase -> data.xlsx -> viewer.xlsx
REM  viewer.xlsx references data.xlsx directly (external link)
REM ============================================================

cd /d "%~dp0"

REM --- Python check ---
if not exist ".venv\Scripts\python.exe" (
    echo [FATAL] .venv not found. Contact admin.
    pause
    exit /b 1
)

REM --- Header ---
echo.
echo ============================================================
echo   TDnet Update  %date% %time%
echo ============================================================
echo.
for /f "delims=" %%V in ('.\.venv\Scripts\python.exe --version 2^>^&1') do echo   %%V
echo.

REM --- Log file ---
if not exist "logs" mkdir logs
for /f "tokens=1-5 delims=/: " %%a in ("%date% %time%") do (
    set LOGTS=%%a%%b%%c_%%d%%e
)
set LOGFILE=logs\run_%LOGTS%.log
echo START %date% %time% > "%LOGFILE%"

set HAS_FATAL=0

REM ========================================
REM Step 1/8: TDnet ingest
REM ========================================
echo [Step 1/8] TDnet ingest...
echo [Step 1/8] TDnet ingest... >> "%LOGFILE%"
.\.venv\Scripts\python.exe tools\tdnet_ingest.py >> "%LOGFILE%" 2>&1
set STEP1_RC=!errorlevel!
if !STEP1_RC! neq 0 (
    echo   [WARN] code=!STEP1_RC!
) else (
    echo   [OK]
)
echo.

REM ========================================
REM Step 2/8: SQLite to Supabase push
REM ========================================
echo [Step 2/8] SQLite to Supabase push...
echo [Step 2/8] SQLite to Supabase push... >> "%LOGFILE%"
.\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --db decision_db.db >> "%LOGFILE%" 2>&1
set STEP2_RC=!errorlevel!
if !STEP2_RC! neq 0 (
    echo   [ERROR] code=!STEP2_RC!
    set HAS_FATAL=1
) else (
    echo   [OK]
)
echo.

REM ========================================
REM Step 3/8: jquants to Supabase financials
REM ========================================
echo [Step 3/8] jquants sync...
echo [Step 3/8] jquants sync... >> "%LOGFILE%"
.\.venv\Scripts\python.exe tools\sync_financials.py --apply >> "%LOGFILE%" 2>&1
set STEP3_RC=!errorlevel!
if !STEP3_RC! neq 0 (
    echo   [WARN] code=!STEP3_RC!
) else (
    echo   [OK]
)
echo.

REM ========================================
REM Step 4/8: KPI sync (SKIPPED - viewer now references data.xlsx directly)
REM ========================================
echo [Step 4/8] KPI sync... (SKIPPED)
echo [Step 4/8] KPI sync... (SKIPPED) >> "%LOGFILE%"
echo.

REM ========================================
REM Step 5/8: Generate data.xlsx
REM ========================================
echo [Step 5/8] Generating data.xlsx...
echo [Step 5/8] Generating data.xlsx... >> "%LOGFILE%"
.\.venv\Scripts\python.exe -m tools.generate_data_excel --output "C:\Users\takuy\OneDrive\data.xlsx" >> "%LOGFILE%" 2>&1
set STEP5_RC=!errorlevel!
if !STEP5_RC! neq 0 (
    if exist "C:\Users\takuy\OneDrive\data.xlsx" (
        echo   [WARN] code=!STEP5_RC! but data.xlsx exists
    ) else (
        echo   [ERROR] code=!STEP5_RC! data.xlsx NOT created
        set HAS_FATAL=1
    )
) else (
    echo   [OK]
)
echo.

REM ========================================
REM Step 6/8: data.xlsx stats
REM ========================================
echo [Step 6/8] data.xlsx stats...
echo [Step 6/8] data.xlsx stats... >> "%LOGFILE%"
if exist "C:\Users\takuy\OneDrive\data.xlsx" (
    for /f "delims=" %%L in ('.\.venv\Scripts\python.exe -m tools.xlsx_stats --file "C:\Users\takuy\OneDrive\data.xlsx" 2^>^&1') do (
        echo   %%L
        echo   %%L >> "%LOGFILE%"
    )
) else (
    echo   [SKIP] data.xlsx not found
)
echo.

REM ========================================
REM Step 7/8: Viewer refresh (SKIPPED - viewer now references data.xlsx directly)
REM ========================================
echo [Step 7/8] Viewer refresh... (SKIPPED)
echo [Step 7/8] Viewer refresh... (SKIPPED) >> "%LOGFILE%"
echo.

REM ========================================
REM Step 8/8: Discord alerts
REM ========================================
echo [Step 8/8] Discord alerts...
echo [Step 8/8] Discord alerts... >> "%LOGFILE%"
if exist "logs\last_ingested_tickers.json" (
    .\.venv\Scripts\python.exe tools\discord_alerts.py >> "%LOGFILE%" 2>&1
    set STEP8_RC=!errorlevel!
    if !STEP8_RC! neq 0 (
        echo   [WARN] discord_alerts code=!STEP8_RC!
    ) else (
        echo   [OK]
    )
) else (
    echo   [SKIP] no new tickers
)
echo.

REM --- Final ---
echo ============================================================ >> "%LOGFILE%"
echo  DONE %date% %time% >> "%LOGFILE%"
echo ============================================================ >> "%LOGFILE%"

echo.
echo ========================================
if !HAS_FATAL! equ 1 (
    echo   ERRORS FOUND - check log: %LOGFILE%
) else (
    echo   ALL STEPS DONE
)
echo   output: C:\Users\takuy\OneDrive\data.xlsx
echo   log:    %LOGFILE%
echo ========================================

REM --- Failure notification (Step4/Step7 excluded - no longer active) ---
powershell -ExecutionPolicy Bypass -NoProfile -File ".\tools\notify_failure.ps1" ^
    -LogFile "%LOGFILE%" ^
    -Step1 !STEP1_RC! -Step2 !STEP2_RC! -Step3 !STEP5_RC! ^
    -HasFatal !HAS_FATAL! -Step3Status "OK"

REM --- Pause control ---
set DO_PAUSE=1
if "%~1"=="--nopause" set DO_PAUSE=0
if "%NO_PAUSE%"=="1" set DO_PAUSE=0

if !DO_PAUSE! equ 1 (
    echo   Press any key to close...
    echo ========================================
    pause
) else (
    echo   [AUTO] nopause mode
    echo   [AUTO] nopause >> "%LOGFILE%"
    echo ========================================
)
