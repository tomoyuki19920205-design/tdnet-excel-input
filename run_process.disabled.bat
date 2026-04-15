@echo off
setlocal
REM ============================================================
REM run_process.bat -- SQLite→Supabase push + J-Quants sync (phase marker 付き)
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\process_%date:~0,4%%date:~5,2%%date:~8,2%.log
set PYTHON=.venv\Scripts\python.exe
if not exist logs mkdir logs
echo ===BAT_START=== [%date% %time%] host=%COMPUTERNAME% cwd=%CD% python=%PYTHON% >> %LOGFILE%
%PYTHON% tools\pipeline_run.py process --trigger scheduler >> %LOGFILE% 2>&1
set EXIT_CODE=%ERRORLEVEL%
echo ===BAT_END=== [%date% %time%] exit=%EXIT_CODE% >> %LOGFILE%
exit /b %EXIT_CODE%
