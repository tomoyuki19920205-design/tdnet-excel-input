@echo off
cd /d "C:\Users\takuy\OneDrive\tdnet-excel-input"

:menu
cls
echo ============================================
echo TDNET SQLite Cleanup Menu
echo ============================================
echo.
echo [1] Dry Run
echo [2] Execute Cleanup
echo [3] Execute Cleanup + VACUUM
echo [4] Execute Cleanup + VACUUM + quarantine.db
echo [5] Execute Cleanup + VACUUM + audit_log
echo [6] Exit
echo.
set /p choice=Select mode (1/2/3/4/5/6): 

if "%choice%"=="1" goto dryrun
if "%choice%"=="2" goto execute
if "%choice%"=="3" goto vacuum
if "%choice%"=="4" goto vacuum_quarantine
if "%choice%"=="5" goto vacuum_audit
if "%choice%"=="6" goto end

echo.
echo Invalid selection.
pause
goto menu

:dryrun
echo.
echo [RUN] Dry Run
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --yes
echo.
pause
goto menu

:execute
echo.
echo [RUN] Execute Cleanup
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --execute --yes
echo.
pause
goto menu

:vacuum
echo.
echo [RUN] Execute Cleanup + VACUUM
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --execute --vacuum --yes
echo.
pause
goto menu

:vacuum_quarantine
echo.
echo [RUN] Execute Cleanup + VACUUM + quarantine.db
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --execute --vacuum --include-quarantine-db --yes
echo.
pause
goto menu

:vacuum_audit
echo.
echo [RUN] Execute Cleanup + VACUUM + audit_log
.\.venv\Scripts\python.exe tools\cleanup_intermediate_data.py --execute --vacuum --include-audit-log --yes
echo.
pause
goto menu

:end
exit /b