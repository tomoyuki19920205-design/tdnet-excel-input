# ============================================================
# run_ingest.ps1 — pytest + tdnet_ingest ワンコマンド実行
# ============================================================
#
# 使い方:
#   .\run_ingest.ps1
#   .\run_ingest.ps1 --company-code 0812
#   .\run_ingest.ps1 --dry-run
#
# ============================================================

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

# プロジェクトルートに移動
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$Python = ".\.venv\Scripts\python.exe"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Step 1: pytest" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

& $Python -m pytest tests\ -q --tb=short
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[ERROR] pytest failed. Ingest skipped." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Step 2: tdnet_ingest" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

& $Python tools\tdnet_ingest.py @args
exit $LASTEXITCODE
