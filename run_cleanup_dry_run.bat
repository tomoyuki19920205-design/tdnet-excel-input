@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
echo ============================================
echo TDNET Cleanup Dry Run
echo ============================================
echo.
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --yes
echo.
pause
