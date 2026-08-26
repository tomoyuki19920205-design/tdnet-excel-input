@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
.venv\Scripts\python.exe tools\scheduler_realtime.py
if errorlevel 1 exit /b %ERRORLEVEL%
.venv\Scripts\python.exe tools\retry_material_urls.py --runner realtime
exit /b %ERRORLEVEL%
