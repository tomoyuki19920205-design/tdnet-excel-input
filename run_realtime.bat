@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
.venv\Scripts\python.exe tools\scheduler_realtime.py
exit /b %ERRORLEVEL%
