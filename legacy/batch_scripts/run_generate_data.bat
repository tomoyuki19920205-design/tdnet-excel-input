@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo   data.xlsx 生成ツール
echo ========================================
echo.

:: logs フォルダ作成
if not exist "logs" mkdir "logs"

:: 日付付きログファイル名
for /f "tokens=1-6 delims=/:. " %%a in ("%date% %time%") do (
    set "LOGFILE=logs\generate_%%a%%b%%c_%%d%%e%%f.log"
)

:: Python環境チェック
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo ========================================
    echo   エラー: Python環境が見つかりません
    echo ========================================
    echo.
    echo   .venv フォルダが見つかりません。
    pause
    exit /b 1
)

:: .env チェック
if not exist ".env" (
    echo.
    echo ========================================
    echo   エラー: .env が見つかりません
    echo ========================================
    echo.
    pause
    exit /b 1
)

echo   実行中... しばらくお待ちください
echo.

:: 実行（★ここが変更済み：OneDrive直下に出力）
echo [%date% %time%] START > "%LOGFILE%"
.venv\Scripts\python.exe -m tools.generate_data_excel -o "C:\Users\takuy\OneDrive\data.xlsx" >> "%LOGFILE%" 2>&1

if errorlevel 1 (
    echo.
    echo ========================================
    echo   エラーが発生しました
    echo ========================================
    echo.
    echo   ログ: %LOGFILE%
    pause
    exit /b 1
)

echo.
echo   完了しました
echo   出力先:
echo   C:\Users\takuy\OneDrive\data.xlsx
echo   ログ:
echo   %LOGFILE%
echo.
echo ========================================
echo   何かキーを押すと閉じます
echo ========================================
pause >nul