@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo   Data Missed Log
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found
    pause
    exit /b 1
)

.\.venv\Scripts\python.exe tools\show_quarantine.py

echo.
echo ========================================
echo   Press any key to close
echo ========================================
pause
