param(
    [ValidateSet("Install", "DisableLegacy", "Uninstall")]
    [string]$Mode = "Install"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RealtimeBat = Join-Path $ProjectRoot "run_realtime.bat"
$RealtimeLauncher = Join-Path $ProjectRoot "tools\run_tdnet_realtime_background.py"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$NightlyBat  = Join-Path $ProjectRoot "run_nightly.bat"
$ReconcileBat = Join-Path $ProjectRoot "run_reconcile_scheduled.bat"

# ── ヘルパー関数 ──────────────────────────────────────

function Test-RequiredPaths {
    $required = @($RealtimeBat, $RealtimeLauncher, $Pythonw, $NightlyBat, $ReconcileBat)
    foreach ($path in $required) {
        if (-not (Test-Path $path)) {
            throw "Required file not found: $path"
        }
    }
}

function Task-Exists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TaskName
    )
    # cmd.exe 経由で schtasks /Query を実行し、未存在時の赤エラーを回避
    $null = & cmd.exe /c "schtasks /Query /TN `"$TaskName`" >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Invoke-Schtasks {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$SchtasksArgs
    )
    Write-Host ("schtasks " + ($SchtasksArgs -join " "))
    & schtasks.exe @SchtasksArgs
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks failed with exit code $LASTEXITCODE"
    }
}

function Disable-TaskIfExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TaskName
    )
    if (Task-Exists -TaskName $TaskName) {
        Invoke-Schtasks -SchtasksArgs @("/Change", "/TN", $TaskName, "/DISABLE")
        Write-Host "Disabled: $TaskName"
    } else {
        Write-Host "Not found (skip): $TaskName"
    }
}

function Delete-TaskIfExists {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TaskName
    )
    if (Task-Exists -TaskName $TaskName) {
        Invoke-Schtasks -SchtasksArgs @("/Delete", "/TN", $TaskName, "/F")
        Write-Host "Deleted: $TaskName"
    } else {
        Write-Host "Not found (skip): $TaskName"
    }
}

# ── Install ───────────────────────────────────────────

function Install-TDNETTasks {
    Test-RequiredPaths

    # 既存タスクを安全に削除
    Delete-TaskIfExists -TaskName "TDNET_Realtime"
    Delete-TaskIfExists -TaskName "TDNET_Nightly"
    Delete-TaskIfExists -TaskName "TDNET_Reconcile"

    # Realtime: 平日 08:32-18:02, 10分間隔
    # /SC WEEKLY + /RI + /DU で MINUTE+D 非互換を回避
    $realtimeCommand = '"' + $Pythonw + '" "' + $RealtimeLauncher + '"'
    Invoke-Schtasks -SchtasksArgs @(
        "/Create",
        "/TN", "TDNET_Realtime",
        "/TR", $realtimeCommand,
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/MO", "1",
        "/ST", "08:32",
        "/RI", "10",
        "/DU", "09:30",
        "/F"
    )
    $realtimeSettings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
    Set-ScheduledTask -TaskName "TDNET_Realtime" -Settings $realtimeSettings | Out-Null
    Write-Host "Created: TDNET_Realtime"

    # Nightly: 毎日 19:00
    Invoke-Schtasks -SchtasksArgs @(
        "/Create",
        "/TN", "TDNET_Nightly",
        "/TR", $NightlyBat,
        "/SC", "DAILY",
        "/ST", "19:00",
        "/F"
    )
    Write-Host "Created: TDNET_Nightly"

    # Reconcile: 毎日 18:35
    Invoke-Schtasks -SchtasksArgs @(
        "/Create",
        "/TN", "TDNET_Reconcile",
        "/TR", $ReconcileBat,
        "/SC", "DAILY",
        "/ST", "18:35",
        "/F"
    )
    Write-Host "Created: TDNET_Reconcile"

    Write-Host "`nInstall completed. All 3 tasks created."
}

# ── DisableLegacy ─────────────────────────────────────

function Disable-LegacyTasks {
    $legacyTasks = @(
        "TDNET_MainPipeline",
        "TDNET_Update_PM",
        "TDNET_Update_All",
        "TDNet Pipeline - Ingest",
        "TDNet Pipeline - Process",
        "TDNet Pipeline - Notify",
        "TDNet Pipeline - Rebuild",
        "TDNet Pipeline - Reconcile"
    )

    foreach ($taskName in $legacyTasks) {
        Disable-TaskIfExists -TaskName $taskName
    }

    Write-Host "`nDisableLegacy completed."
}

# ── Uninstall ─────────────────────────────────────────

function Uninstall-TDNETTasks {
    $newTasks = @(
        "TDNET_Realtime",
        "TDNET_Nightly",
        "TDNET_Reconcile"
    )

    foreach ($taskName in $newTasks) {
        Delete-TaskIfExists -TaskName $taskName
    }

    Write-Host "`nUninstall completed."
}

# ── エントリポイント ──────────────────────────────────

switch ($Mode) {
    "Install" {
        Install-TDNETTasks
    }
    "DisableLegacy" {
        Disable-LegacyTasks
    }
    "Uninstall" {
        Uninstall-TDNETTasks
    }
    default {
        throw "Unknown mode: $Mode"
    }
}
