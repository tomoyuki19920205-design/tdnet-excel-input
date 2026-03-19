@echo off
REM ============================================================
REM run_ingest.bat -- TDnet ingest 実行
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\ingest_%date:~0,4%%date:~5,2%%date:~8,2%.log
if not exist logs mkdir logs
echo [%date% %time%] Starting ingest >> %LOGFILE%
.venv\Scripts\python.exe tools\pipeline_run.py ingest --trigger scheduler >> %LOGFILE% 2>&1
echo [%date% %time%] Finished (exit=%ERRORLEVEL%) >> %LOGFILE%
