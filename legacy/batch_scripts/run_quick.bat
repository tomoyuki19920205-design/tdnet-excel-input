@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  run_quick.bat - TDNET realtime monitor (loop every 2 min)
REM ============================================================
REM  Earnings season mode: runs 5 steps in a loop.
REM  Double-click to start, close window to stop.
REM
REM  Step 1: TDnet ingest
REM  Step 2: Supabase push
REM  Step 3: data.xlsx generation
REM  Step 4: Viewer _DATA refresh
REM  Step 5: Discord alerts
REM ============================================================

cd /d "%~dp0"

if not exist ".\.venv\Scripts\python.exe" (
    echo [FATAL] .venv not found.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   TDNET Realtime Monitor
echo   Loop interval: 2 min / Close window to stop
echo ============================================================
echo.

set LOOP_COUNT=0

:loop_start
set /a LOOP_COUNT+=1

echo.
echo [%date% %time%] === Run #!LOOP_COUNT! ===

REM --- Step 1: TDnet ingest ---
echo   [1/5] TDnet ingest...
.\.venv\Scripts\python.exe tools\tdnet_ingest.py >nul 2>&1
if !errorlevel! neq 0 (
    echo         [WARN] code=!errorlevel!
) else (
    echo         [OK]
)

REM --- Step 2: Supabase push ---
echo   [2/5] Supabase push...
.\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --db decision_db.db >nul 2>&1
if !errorlevel! neq 0 (
    echo         [WARN] code=!errorlevel!
) else (
    echo         [OK]
)

REM --- Step 3: data.xlsx ---
echo   [3/5] data.xlsx...
.\.venv\Scripts\python.exe -m tools.generate_data_excel --output "C:\Users\takuy\OneDrive\data.xlsx" >nul 2>&1
if !errorlevel! neq 0 (
    echo         [WARN] code=!errorlevel!
) else (
    echo         [OK]
)

REM --- Step 4: Viewer refresh ---
echo   [4/5] Viewer...
if exist "C:\Users\takuy\OneDrive\data.xlsx" (
    if exist "C:\Users\takuy\OneDrive\20260303テスト用コピー.xlsx" (
        .\.venv\Scripts\python.exe tools\refresh_pl_view.py --data_xlsx "C:\Users\takuy\OneDrive\data.xlsx" --viewer_xlsx "C:\Users\takuy\OneDrive\20260303テスト用コピー.xlsx" --no-backup >nul 2>&1
        if !errorlevel! neq 0 (
            echo         [WARN] code=!errorlevel!
        ) else (
            echo         [OK]
        )
    ) else (
        echo         [SKIP] viewer not found
    )
) else (
    echo         [SKIP] data.xlsx not found
)

REM --- Step 5: Discord alerts ---
echo   [5/5] Discord...
if exist "logs\last_ingested_items.json" (
    .\.venv\Scripts\python.exe tools\discord_alerts.py >nul 2>&1
    if !errorlevel! neq 0 (
        echo         [WARN] code=!errorlevel!
    ) else (
        echo         [OK]
    )
) else if exist "logs\last_ingested_tickers.json" (
    .\.venv\Scripts\python.exe tools\discord_alerts.py >nul 2>&1
) else (
    echo         [SKIP] no new tickers
)

echo   --- Done. Next run in 120 sec (Ctrl+C or close to stop) ---

REM --- Wait 120 seconds ---
timeout /t 120 /nobreak >nul

goto loop_start
