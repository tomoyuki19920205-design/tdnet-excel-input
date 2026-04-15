@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
.venv\Scripts\python.exe tools\scheduler_reconcile.py
exit /b %ERRORLEVEL%
