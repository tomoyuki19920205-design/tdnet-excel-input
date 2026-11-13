param(
    [string]$Runtime = 'C:\Users\takuy\tdnet-ny-market-runtime',
    [string]$Python = 'C:\Users\takuy\tdnet-excel-input\.venv\Scripts\pythonw.exe',
    [string]$ProductionRoot = 'C:\Users\takuy\tdnet-excel-input',
    [string]$State = 'C:\Users\takuy\tdnet-production\state',
    [string]$AutomationConfig = 'C:\Users\takuy\.codex\automations\ny\automation.toml',
    [string]$OutputDir = 'C:\Users\takuy\tdnet-production\state\data\ny_market_health',
    [string]$TaskName = 'NYMarketDeliveryHealth',
    [string]$BackupPath,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$script = Join-Path $Runtime 'tools\check_ny_market_delivery.py'
foreach ($path in @($Python,$script,$AutomationConfig,(Join-Path $State 'decision_db.db'))) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Missing required path: $path" }
    if ($path.Contains('"')) { throw 'Quote in task path is unsupported' }
}
$arguments = '-B -X utf8 "{0}" --runtime "{1}" --production-root "{2}" --db "{3}" --automation-config "{4}" --output-dir "{5}" --deadline 07:45' -f $script,$Runtime,$ProductionRoot,(Join-Path $State 'decision_db.db'),$AutomationConfig,$OutputDir
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($old -and $old.Description -ne 'NY report read-only delivery watchdog; no AI generation') {
    throw 'Refusing to replace an unrelated task'
}
if (-not $Apply) {
    [pscustomobject]@{TaskName=$TaskName;User=$user;LogonType='S4U';RunLevel='Limited';TimesJST='07:45,12:45,17:45,22:45 + boot';Execute=$Python;Arguments=$arguments;PaidGenerationCalls=0} | ConvertTo-Json
    exit 0
}
if ($old) {
    if (-not $BackupPath) { throw 'BackupPath required for an existing task' }
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath $BackupPath -Encoding utf8
}
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Runtime
$triggers = @('07:45','12:45','17:45','22:45') | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$triggers += New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Description 'NY report read-only delivery watchdog; no AI generation' -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Force | Select-Object TaskName,State
