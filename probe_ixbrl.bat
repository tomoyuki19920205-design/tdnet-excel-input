@echo off
chcp 65001 >nul
echo ========================================
echo   iXBRL Probe ツール
echo ========================================
echo.
cd /d C:\Users\takuy\OneDrive\tdnet-excel-input
.\.venv\Scripts\python.exe tools\ixbrl_probe.py %1
echo.
echo ========================================
echo   完了しました
echo   何かキーを押すとウィンドウを閉じます
echo ========================================
pause
