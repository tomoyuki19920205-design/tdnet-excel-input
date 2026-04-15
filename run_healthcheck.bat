@echo off
setlocal
REM ============================================================
REM run_healthcheck.bat -- パイプラインヘルスチェック
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set PYTHON=.venv\Scripts\python.exe
set LOGFILE=logs\healthcheck_%date:~0,4%%date:~5,2%%date:~8,2%.log
if not exist logs mkdir logs
echo [%date% %time%] healthcheck start >> %LOGFILE%
%PYTHON% tools\pipeline_healthcheck.py >> %LOGFILE% 2>&1
echo [%date% %time%] healthcheck end exit=%ERRORLEVEL% >> %LOGFILE%
