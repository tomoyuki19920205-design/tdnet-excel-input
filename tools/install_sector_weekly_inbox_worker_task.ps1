[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "SectorWeeklyInboxWorker",
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 5,
    [string]$RepositoryRoot,
    [string]$PythonPath,
    [string]$WorkRoot,
    [string]$DatabasePath,
    [switch]$DryRunSync,
    [switch]$RunNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing -and $PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    return
}

$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$workerScript = Join-Path $RepositoryRoot "tools\sector_weekly_inbox_worker.py"
$hiddenLauncher = Join-Path $RepositoryRoot "tools\run_sector_weekly_inbox_worker_hidden.vbs"
if (-not (Test-Path -LiteralPath $workerScript -PathType Leaf)) { throw "Worker not found: $workerScript" }
if (-not (Test-Path -LiteralPath $hiddenLauncher -PathType Leaf)) { throw "Launcher not found: $hiddenLauncher" }

$wscriptPath = Join-Path ([Environment]::GetFolderPath("System")) "wscript.exe"
$wscriptPath = (Resolve-Path -LiteralPath $wscriptPath).Path
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $RepositoryRoot ".venv\Scripts\pythonw.exe"
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if ([System.IO.Path]::GetFileName($PythonPath) -ine "pythonw.exe") {
    throw "SectorWeeklyInboxWorker requires pythonw.exe: $PythonPath"
}

function ConvertTo-QuotedTaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

$workerArguments = @($workerScript, "--once", "--root", $RepositoryRoot, "--trigger", "task_scheduler")
if (-not [string]::IsNullOrWhiteSpace($WorkRoot)) {
    $workerArguments += @("--work-root", (Resolve-Path -LiteralPath $WorkRoot).Path)
}
if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
    $databaseParent = (Resolve-Path -LiteralPath (Split-Path -Parent $DatabasePath)).Path
    $workerArguments += @("--db", (Join-Path $databaseParent (Split-Path -Leaf $DatabasePath)))
}
if ($DryRunSync) { $workerArguments += "--dry-run-sync" }

$launcherArguments = @("//B", "//NoLogo", $hiddenLauncher, $PythonPath, $RepositoryRoot) + $workerArguments
$argumentString = ($launcherArguments | ForEach-Object { ConvertTo-QuotedTaskArgument $_ }) -join " "
$action = New-ScheduledTaskAction -Execute $wscriptPath -Argument $argumentString -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Poll staged Sector Weekly payloads, upsert local canonical data, and sync Supabase."

if ($PSCmdlet.ShouldProcess($TaskName, "Register disabled scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    if ($RunNow) { throw "SectorWeeklyInboxWorker is installed disabled; -RunNow is not allowed" }
}

[pscustomobject]@{
    TaskName = $TaskName
    IntervalMinutes = $IntervalMinutes
    Execute = $wscriptPath
    Arguments = $argumentString
    WorkingDirectory = $RepositoryRoot
    MultipleInstances = "IgnoreNew"
    ExecutionTimeLimit = "PT5M"
    Enabled = $false
}
