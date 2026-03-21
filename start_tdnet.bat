@echo off
chcp 65001 >nul
echo ========================================
echo   TDnet決算自動入力システム 起動中...
echo ========================================
echo.
cd /d C:\Users\takuy\OneDrive\tdnet-excel-input
.\.venv\Scripts\python.exe src\main.py
echo.
echo ========================================
echo   プログラムが終了しました
echo   何かキーを押すとウィンドウを閉じます
echo ========================================
pause
