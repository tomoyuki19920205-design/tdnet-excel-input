[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "CompanyNewsInboxWorker",
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 1,
    [string]$RepositoryRoot,
    [string]$WorkerRoot,
    [string]$PythonPath,
    [string]$WorkDirectory,
    [string]$InboxDirectory,
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
$WorkerRoot = if ([string]::IsNullOrWhiteSpace($WorkerRoot)) {
    $RepositoryRoot
}
else {
    (Resolve-Path -LiteralPath $WorkerRoot).Path
}
$workerScript = Join-Path $RepositoryRoot "tools\company_news_inbox_worker.py"
if (-not (Test-Path -LiteralPath $workerScript -PathType Leaf)) {
    throw "Worker script not found: $workerScript"
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $venvPython = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $PythonPath = $venvPython
    }
    else {
        $PythonPath = (Get-Command python.exe -ErrorAction Stop).Source
    }
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

function ConvertTo-QuotedTaskArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

$workerArguments = @(
    $workerScript,
    "--once",
    "--root", $WorkerRoot,
    "--trigger", "task_scheduler"
)
if (-not [string]::IsNullOrWhiteSpace($WorkDirectory)) {
    $workerArguments += @("--work-dir", (Resolve-Path -LiteralPath $WorkDirectory).Path)
}
if (-not [string]::IsNullOrWhiteSpace($InboxDirectory)) {
    $workerArguments += @("--inbox", (Resolve-Path -LiteralPath $InboxDirectory).Path)
}
if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
    $databaseParent = Split-Path -Parent $DatabasePath
    if (-not [string]::IsNullOrWhiteSpace($databaseParent)) {
        $databaseParent = (Resolve-Path -LiteralPath $databaseParent).Path
        $DatabasePath = Join-Path $databaseParent (Split-Path -Leaf $DatabasePath)
    }
    $workerArguments += @("--db", $DatabasePath)
}
if ($DryRunSync) {
    $workerArguments += "--dry-run-sync"
}
$argumentString = ($workerArguments | ForEach-Object { ConvertTo-QuotedTaskArgument $_ }) -join " "

$action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument $argumentString `
    -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Poll company_news_v1 inbox payloads and run canonical ingest/sync."

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    if ($RunNow) {
        Start-ScheduledTask -TaskName $TaskName
    }
}

[pscustomobject]@{
    TaskName = $TaskName
    IntervalMinutes = $IntervalMinutes
    Execute = $PythonPath
    Arguments = $argumentString
    WorkingDirectory = $RepositoryRoot
    WorkerRoot = $WorkerRoot
    MultipleInstances = "IgnoreNew"
    User = $currentUser
    RunNow = [bool]$RunNow
}
