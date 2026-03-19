@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  run_update_all.bat - TDnet ワンクリック全更新
REM ============================================================
REM  Step 1: TDnet新規開示をワンショット取得 → SQLite
REM  Step 2: decision_db → Supabase push
REM  Step 3: jquants.db → Supabase financials 差分同期
REM  Step 4: KPI Excel → SQLite 吸い上げ
REM  Step 5: Supabase → data.xlsx 生成 (OneDrive直下)
REM  Step 6: data.xlsx 統計出力
REM ============================================================

cd /d "%~dp0"

set XLSX_PATH=C:\Users\takuy\OneDrive\data.xlsx

REM --- Python環境チェック ---
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ========================================
    echo   Python環境が見つかりません
    echo   .venv フォルダが必要です
    echo   管理者に連絡してください
    echo ========================================
    pause
    exit /b 1
)

REM --- .envチェック ---
if not exist ".env" (
    echo.
    echo ========================================
    echo   .env ファイルが見つかりません
    echo   SUPABASE_URL / SUPABASE_ANON_KEY を
    echo   .env に設定してください
    echo ========================================
    pause
    exit /b 1
)

REM --- ログ準備 (logs/ 自動作成) ---
if not exist "logs" mkdir "logs"

REM --- タイムスタンプ生成 ---
for /f "tokens=1-3 delims=/" %%a in ("%date%") do (
    set DT_Y=%%a
    set DT_M=%%b
    set DT_D=%%c
)
for /f "tokens=1-3 delims=:." %%a in ("%time: =0%") do (
    set TM_H=%%a
    set TM_M=%%b
    set TM_S=%%c
)
set TIMESTAMP=%DT_Y%%DT_M%%DT_D%_%TM_H%%TM_M%%TM_S%
set LOGFILE=logs\run_%TIMESTAMP%.log

echo.
echo ========================================
echo   TDnet ワンクリック全更新
echo   %date% %time%
echo ========================================
echo   ログ: %LOGFILE%
echo ========================================
echo.

echo ============================================================ > "%LOGFILE%"
echo  TDnet ワンクリック全更新  %date% %time% >> "%LOGFILE%"
echo ============================================================ >> "%LOGFILE%"

REM --- 全体判定用フラグ ---
set HAS_FATAL=0

REM ========================================
REM Step 1/5: TDnet新規開示ワンショット取得
REM ========================================
echo [Step 1/6] TDnet新規開示を取得中...
echo. >> "%LOGFILE%"
echo [Step 1/6] TDnet新規開示を取得中... >> "%LOGFILE%"
.\.venv\Scripts\python.exe tools\tdnet_ingest.py >> "%LOGFILE%" 2>&1
set STEP1_RC=!errorlevel!

if !STEP1_RC! neq 0 (
    echo   [WARN] エラーがありましたが続行します [code=!STEP1_RC!]
    echo   [WARN] Step1 code=!STEP1_RC! >> "%LOGFILE%"
) else (
    echo   [OK] 完了
)
echo.

REM ========================================
REM Step 2/6: SQLite → Supabase push (decision_db)
REM ========================================
echo [Step 2/6] SQLite → Supabase push中...
echo. >> "%LOGFILE%"
echo [Step 2/6] SQLite → Supabase push中... >> "%LOGFILE%"
.\.venv\Scripts\python.exe -m tools.sqlite_to_supabase --db decision_db.db >> "%LOGFILE%" 2>&1
set STEP2_RC=!errorlevel!

if !STEP2_RC! neq 0 (
    echo   [ERROR] 失敗しました [code=!STEP2_RC!]
    echo   [ERROR] Step2 code=!STEP2_RC! >> "%LOGFILE%"
    set HAS_FATAL=1
) else (
    echo   [OK] 完了
)
echo.

REM ========================================
REM Step 3/6: jquants.db → Supabase financials (差分30日)
REM ========================================
echo [Step 3/6] jquants → Supabase financials 差分同期中...
echo. >> "%LOGFILE%"
echo [Step 3/6] jquants → Supabase financials 差分同期中... >> "%LOGFILE%"
.\.venv\Scripts\python.exe tools\sync_financials.py --apply >> "%LOGFILE%" 2>&1
set STEP2B_RC=!errorlevel!

if !STEP2B_RC! neq 0 (
    echo   [WARN] sync_financials code=!STEP2B_RC! (non-fatal)
    echo   [WARN] sync_financials code=!STEP2B_RC! >> "%LOGFILE%"
) else (
    echo   [OK] 完了
)
echo.

REM ========================================
REM Step 4/6: KPI Excel → SQLite 吸い上げ
REM ========================================
set SHARED_EXCEL=C:\Users\takuy\OneDrive\20260228テスト用A.xlsx
echo [Step 4/6] KPI吸い上げ中...
echo. >> "%LOGFILE%"
echo [Step 4/6] KPI sync... >> "%LOGFILE%"

if exist "%SHARED_EXCEL%" (
    .\.venv\Scripts\python.exe -m tools.kpi_sync --excel "%SHARED_EXCEL%" --db decision_db.db >> "%LOGFILE%" 2>&1
    set KPI_RC=!errorlevel!
    if !KPI_RC! neq 0 (
        echo   [WARN] KPI sync code=!KPI_RC! (non-fatal)
        echo   [WARN] KPI sync code=!KPI_RC! >> "%LOGFILE%"
    ) else (
        echo   [OK] 完了
    )
) else (
    echo   [SKIP] shared Excel not found
    echo   [SKIP] shared Excel not found >> "%LOGFILE%"
)
echo.

REM ========================================
REM Step 5/6: Supabase → data.xlsx 生成
REM ========================================
echo [Step 5/6] data.xlsx を生成中...
echo. >> "%LOGFILE%"
echo [Step 5/6] data.xlsx を生成中... >> "%LOGFILE%"
.\.venv\Scripts\python.exe -m tools.generate_data_excel --output "%XLSX_PATH%" >> "%LOGFILE%" 2>&1
set STEP3_RC=!errorlevel!
set STEP3_STATUS=OK

if !STEP3_RC! neq 0 (
    REM --- Step3 非ゼロ: data.xlsx が存在するか確認して判定 ---
    if exist "%XLSX_PATH%" (
        REM ファイルは存在する → WARNING に格下げ
        set STEP3_STATUS=WARN
        echo   [WARN] 戻り値が非ゼロですが data.xlsx は存在します [code=!STEP3_RC!]
        echo   [WARN] Step3 code=!STEP3_RC! - data.xlsx exists, downgraded to WARN >> "%LOGFILE%"
    ) else (
        REM ファイルなし → 本当のエラー
        set STEP3_STATUS=ERROR
        echo   [ERROR] 失敗しました [code=!STEP3_RC!]
        echo   [ERROR] Step3 code=!STEP3_RC! - data.xlsx NOT found >> "%LOGFILE%"
        set HAS_FATAL=1
    )
) else (
    echo   [OK] 完了
)

REM --- SIMULATE_FAIL: test mode (overrides everything above) ---
if "%SIMULATE_FAIL%"=="1" (
    set STEP3_RC=1
    set STEP3_STATUS=ERROR
    set HAS_FATAL=1
    echo   [TEST] SIMULATE_FAIL=1 forced failure
    echo   [TEST] SIMULATE_FAIL=1 forced failure >> "%LOGFILE%"
)
echo.

REM ========================================
REM Step 6/6: data.xlsx 統計出力
REM ========================================
echo [Step 6/6] data.xlsx 統計チェック...
echo. >> "%LOGFILE%"
echo [Step 6/6] data.xlsx 統計チェック... >> "%LOGFILE%"
set STEP4_OK=0

if exist "%XLSX_PATH%" (
    for /f "delims=" %%L in ('.\.venv\Scripts\python.exe -m tools.xlsx_stats --file "%XLSX_PATH%" 2^>^&1') do (
        echo   %%L
        echo   %%L >> "%LOGFILE%"
        REM [XLSX] rows が取れたら成功とみなす
        echo %%L | findstr /C:"[XLSX] rows" >nul 2>&1
        if !errorlevel! equ 0 set STEP4_OK=1
    )
) else (
    echo   [SKIP] data.xlsx が存在しません
    echo   [SKIP] data.xlsx が存在しません >> "%LOGFILE%"
)

REM --- Step3がWARNだった場合、Step4で統計取得できたか最終判定 ---
if "!STEP3_STATUS!"=="WARN" (
    if !STEP4_OK! equ 1 (
        echo   [INFO] Step3 WARNING格下げ確定: data.xlsx の読み取り成功
        echo   [INFO] Step3 WARN confirmed: xlsx_stats succeeded >> "%LOGFILE%"
    ) else (
        REM Step4で読めなかった → やはりエラー
        set STEP3_STATUS=ERROR
        set HAS_FATAL=1
        echo   [ERROR] Step3 格下げ撤回: data.xlsx を読み取れません
        echo   [ERROR] Step3 WARN revoked: xlsx_stats failed >> "%LOGFILE%"
    )
)
echo.

REM --- 最終結果 ---
echo. >> "%LOGFILE%"
echo ============================================================ >> "%LOGFILE%"
echo  完了  %date% %time% >> "%LOGFILE%"
echo  Step1=%STEP1_RC% Step2=%STEP2_RC% Step3=%STEP3_RC% Step3Status=!STEP3_STATUS! >> "%LOGFILE%"
echo ============================================================ >> "%LOGFILE%"

echo.
echo ========================================
if !HAS_FATAL! equ 1 (
    echo   エラーが発生しました
    echo   ログを確認してください: %LOGFILE%
) else (
    echo   全ステップ完了
)
echo ----------------------------------------
echo   出力: %XLSX_PATH%
echo   ログ: %LOGFILE%
echo   quarantine: data\quarantine.db
echo ========================================

REM --- 失敗時通知 (notify_failure.ps1) ---
powershell -ExecutionPolicy Bypass -NoProfile -File ".\tools\notify_failure.ps1" ^
    -LogFile "%LOGFILE%" ^
    -Step1 !STEP1_RC! -Step2 !STEP2_RC! -Step3 !STEP3_RC! ^
    -HasFatal !HAS_FATAL! -Step3Status "!STEP3_STATUS!"

REM --- pause 判定: --nopause 引数 or NO_PAUSE=1 ならスキップ ---
set DO_PAUSE=1
if "%~1"=="--nopause" set DO_PAUSE=0
if "%NO_PAUSE%"=="1" set DO_PAUSE=0

if !DO_PAUSE! equ 1 (
    echo   何かキーを押すとウィンドウを閉じます
    echo ========================================
    pause
) else (
    echo   [AUTO] ノンインタラクティブモードで終了
    echo   [AUTO] nopause mode >> "%LOGFILE%"
    echo ========================================
)
