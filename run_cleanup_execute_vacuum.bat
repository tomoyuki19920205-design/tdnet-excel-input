@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"
echo ============================================
echo TDNET Cleanup Execute + VACUUM
echo ============================================
echo.
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --execute --vacuum --yes
echo.
pause