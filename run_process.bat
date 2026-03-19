@echo off
REM ============================================================
REM run_process.bat -- SQLite→Supabase push + J-Quants sync
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\process_%date:~0,4%%date:~5,2%%date:~8,2%.log
if not exist logs mkdir logs
echo [%date% %time%] Starting process >> %LOGFILE%
.venv\Scripts\python.exe tools\pipeline_run.py process --trigger scheduler >> %LOGFILE% 2>&1
echo [%date% %time%] Finished (exit=%ERRORLEVEL%) >> %LOGFILE%
