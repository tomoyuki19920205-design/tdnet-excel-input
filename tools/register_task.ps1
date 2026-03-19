# ============================================================
# register_task.ps1 - Register TDNET_Update_All scheduled task
# ============================================================
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\tools\register_task.ps1
#   powershell -ExecutionPolicy Bypass -File .\tools\register_task.ps1 -Unregister
#   powershell -ExecutionPolicy Bypass -File .\tools\register_task.ps1 -Time "18:00"
# ============================================================

param(
    [switch]$Unregister,
    [string]$Time = "15:30"
)

# --- Config (edit these if needed) ---
$TaskName = "TDNET_Update_All"
$BatPath  = "C:\Users\takuy\OneDrive\tdnet-excel-input\run_update_all.bat"
$WorkDir  = "C:\Users\takuy\OneDrive\tdnet-excel-input"
$Days     = "MON,TUE,WED,THU,FRI"

# ============================================================
# Unregister mode
# ============================================================
if ($Unregister) {
    Write-Host ""
    Write-Host "========================================"
    Write-Host "  Removing task: $TaskName"
    Write-Host "========================================"
    schtasks /Delete /TN $TaskName /F 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] Task removed." -ForegroundColor Green
    } else {
        Write-Host "  [WARN] Task not found or already removed." -ForegroundColor Yellow
    }
    exit 0
}

# ============================================================
# Pre-flight checks
# ============================================================
if (-not (Test-Path $BatPath)) {
    Write-Host ""
    Write-Host "========================================"  -ForegroundColor Red
    Write-Host "  [ERROR] bat file not found:" -ForegroundColor Red
    Write-Host "    $BatPath" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Copy files to OneDrive first." -ForegroundColor Red
    Write-Host "========================================"  -ForegroundColor Red
    exit 1
}

# ============================================================
# Register task via schtasks.exe
# ============================================================
Write-Host ""
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  TDNET Task Scheduler Registration"
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "  Task     : $TaskName"
Write-Host "  Command  : $BatPath --nopause"
Write-Host "  WorkDir  : $WorkDir"
Write-Host "  Schedule : Weekdays ($Days) at $Time"
Write-Host "  User     : $env:USERNAME"
Write-Host "========================================"  -ForegroundColor Cyan
Write-Host ""

# Remove existing task first (ignore errors)
schtasks /Delete /TN $TaskName /F 2>$null | Out-Null

# Build the command string for schtasks
# /TR must wrap the full command in quotes properly
$TaskCmd = "cmd.exe /c `"$BatPath`" --nopause"

schtasks /Create `
    /TN $TaskName `
    /TR $TaskCmd `
    /SC WEEKLY `
    /D $Days `
    /ST $Time `
    /RL LIMITED `
    /F

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "========================================"  -ForegroundColor Red
    Write-Host "  [ERROR] Task registration failed." -ForegroundColor Red
    Write-Host "  Try running PowerShell as Administrator." -ForegroundColor Red
    Write-Host "========================================"  -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "========================================"  -ForegroundColor Green
Write-Host "  [OK] Task registered successfully!" -ForegroundColor Green
Write-Host "========================================"  -ForegroundColor Green
Write-Host ""
Write-Host "  Verify:"
Write-Host "    schtasks /Query /TN TDNET_Update_All"
Write-Host ""
Write-Host "  Run now (manual test):"
Write-Host "    schtasks /Run /TN TDNET_Update_All"
Write-Host ""
Write-Host "  Remove:"
Write-Host "    .\tools\register_task.ps1 -Unregister"
Write-Host ""
Write-Host "  Change schedule via GUI:"
Write-Host "    taskschd.msc"
Write-Host ""