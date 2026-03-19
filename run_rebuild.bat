@echo off
REM ============================================================
REM run_rebuild.bat -- Serving view 再構築
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\rebuild_%date:~0,4%%date:~5,2%%date:~8,2%.log
if not exist logs mkdir logs
echo [%date% %time%] Starting rebuild >> %LOGFILE%
.venv\Scripts\python.exe tools\pipeline_run.py rebuild --trigger scheduler >> %LOGFILE% 2>&1
echo [%date% %time%] Finished (exit=%ERRORLEVEL%) >> %LOGFILE%
