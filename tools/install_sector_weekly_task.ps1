[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$TaskName = "SectorWeeklyScheduler",
    [string]$RepositoryRoot,
    [string]$PythonPath,
    [string]$DatabasePath,
    [switch]$Enable,
    [switch]$RunNow,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) { $RepositoryRoot = Split-Path -Parent $PSScriptRoot }

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $existing -and $PSCmdlet.ShouldProcess($TaskName, "Unregister scheduled task")) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    return
}

$RepositoryRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$runner = Join-Path $RepositoryRoot "tools\sector_weekly_scheduler.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Runner not found: $runner" }
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$arguments = @($runner)
if (-not [string]::IsNullOrWhiteSpace($DatabasePath)) {
    $arguments += @("--db", $DatabasePath)
}
$now = Get-Date
$daysUntilSaturday = (([int][DayOfWeek]::Saturday - [int]$now.DayOfWeek) + 7) % 7
$firstSaturday = $now.Date.AddDays($daysUntilSaturday).AddHours(6)
if ($firstSaturday -le $now) { $firstSaturday = $firstSaturday.AddDays(7) }
$notBefore = $firstSaturday.ToString("yyyy-MM-ddTHH:mm:sszzz")
$arguments += @("--not-before", $notBefore)
$argumentString = ($arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join " "
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $argumentString -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger -Once -At $firstSaturday -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 59)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Queue at most one Sector Weekly assignment per hourly JST slot until the fixed reporting period reaches 33/33; never runs LLM or Web Search."

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    if (-not $Enable) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
    if ($RunNow) {
        if (-not $Enable) { throw "-RunNow requires -Enable" }
        Start-ScheduledTask -TaskName $TaskName
    }
}

[pscustomobject]@{
    TaskName = $TaskName
    NextStart = $firstSaturday
    Repetition = "PT1H"
    RepetitionDuration = "Indefinite"
    CompletionTarget = $firstSaturday.AddDays(2).AddHours(2).AddMinutes(55)
    HardStop = $false
    StopCondition = "COMPLETE_33_OF_33"
    Execute = $PythonPath
    Arguments = $argumentString
    MultipleInstances = "IgnoreNew"
    ExistingCompanyNewsTasksChanged = $false
    Enabled = [bool]$Enable
    NotBefore = $notBefore
}
