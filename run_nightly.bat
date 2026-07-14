@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
.venv\Scripts\python.exe tools\scheduler_nightly.py
if errorlevel 1 exit /b %ERRORLEVEL%
.venv\Scripts\python.exe tools\backfill_earnings_tdnet_events.py --since 60
exit /b %ERRORLEVEL%
