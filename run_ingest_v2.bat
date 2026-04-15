@echo off
setlocal
REM ============================================================
REM run_ingest.bat -- TDnet ingest 実行 (phase marker 付き)
REM ============================================================
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
set LOGFILE=logs\ingest_%date:~0,4%%date:~5,2%%date:~8,2%.log
set PYTHON=.venv\Scripts\python.exe
if not exist logs mkdir logs
echo ===BAT_START=== [%date% %time%] host=%COMPUTERNAME% cwd=%CD% python=%PYTHON% >> %LOGFILE%
%PYTHON% tools\pipeline_run.py ingest --trigger scheduler >> %LOGFILE% 2>&1
set EXIT_CODE=%ERRORLEVEL%
echo ===BAT_END=== [%date% %time%] exit=%EXIT_CODE% >> %LOGFILE%
exit /b %EXIT_CODE%
