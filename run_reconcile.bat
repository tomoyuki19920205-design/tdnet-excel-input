@echo off
REM ============================================================
REM run_reconcile.bat -- 深夜整合性チェック
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\reconcile_%date:~0,4%%date:~5,2%%date:~8,2%.log
if not exist logs mkdir logs
echo [%date% %time%] Starting reconcile >> %LOGFILE%
.venv\Scripts\python.exe tools\pipeline_run.py reconcile --trigger scheduler >> %LOGFILE% 2>&1
echo [%date% %time%] Finished (exit=%ERRORLEVEL%) >> %LOGFILE%
