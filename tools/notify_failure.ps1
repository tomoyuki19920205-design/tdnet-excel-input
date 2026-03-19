# ============================================================
# notify_failure.ps1 - TDNET failure notification
# ============================================================
# Called by run_update_all.bat after completion.
# Only notifies on failure (non-zero steps or HAS_FATAL=1).
# Success = silent (no notification).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File notify_failure.ps1 ^
#     -LogFile "logs\run_20260301_153000.log" ^
#     -Step1 0 -Step2 0 -Step3 0 -HasFatal 0
# ============================================================

param(
    [string]$LogFile = "",
    [int]$Step1 = 0,
    [int]$Step2 = 0,
    [int]$Step3 = 0,
    [int]$HasFatal = 0,
    [string]$Step3Status = "OK"
)

# --- Determine if this is a failure ---
$isFail = $false
$reasons = @()

if ($HasFatal -ne 0) {
    $isFail = $true
    $reasons += "HAS_FATAL=1"
}
if ($Step1 -ne 0) {
    $isFail = $true
    $reasons += "Step1=$Step1"
}
if ($Step2 -ne 0) {
    $isFail = $true
    $reasons += "Step2=$Step2"
}
if ($Step3 -ne 0 -and $Step3Status -eq "ERROR") {
    $isFail = $true
    $reasons += "Step3=$Step3 (Status=$Step3Status)"
}

# --- Success: exit silently ---
if (-not $isFail) {
    exit 0
}

# --- Build notification message ---
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$summary = "TDNET Update FAILED at $timestamp"
$detail = "Reasons: " + ($reasons -join ", ")

$logPath = ""
if ($LogFile -and (Test-Path $LogFile)) {
    $logPath = (Resolve-Path $LogFile).Path
}

$tail = ""
if ($logPath) {
    $tail = (Get-Content $logPath -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
}

$fullMsg = @"
$summary
$detail
Step1=$Step1 Step2=$Step2 Step3=$Step3 Step3Status=$Step3Status HasFatal=$HasFatal
Log: $logPath

--- Last 20 lines ---
$tail
"@

# --- 1) Write to Windows Event Log ---
$source = "TDNET_Update_All"
$logName = "Application"

try {
    # Register source if not exists (may need admin once)
    if (-not [System.Diagnostics.EventLog]::SourceExists($source)) {
        try {
            [System.Diagnostics.EventLog]::CreateEventSource($source, $logName)
        } catch {
            # Non-admin: fall through, try writing anyway
        }
    }
    Write-EventLog -LogName $logName -Source $source -EventId 1001 `
        -EntryType Error -Message $fullMsg -ErrorAction Stop
    Write-Host "[NOTIFY] Event Log written (Application / $source / Error)"
} catch {
    # Fallback: use generic source
    try {
        Write-EventLog -LogName $logName -Source "Application" -EventId 1001 `
            -EntryType Error -Message $fullMsg -ErrorAction Stop
        Write-Host "[NOTIFY] Event Log written (Application / Application / Error)"
    } catch {
        Write-Host "[NOTIFY] Event Log write failed: $_"
    }
}

# --- 2) Windows Toast notification (best-effort) ---
try {
    $toastTitle = "TDNET Update FAILED"
    $toastBody = $detail
    if ($logPath) {
        $toastBody += "`nLog: $logPath"
    }

    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

    $template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>$toastTitle</text>
      <text>$toastBody</text>
    </binding>
  </visual>
</toast>
"@
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($template)
    $appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
    Write-Host "[NOTIFY] Toast notification shown"
} catch {
    Write-Host "[NOTIFY] Toast not available (non-interactive or unsupported): $_"
}

# --- 3) Console output (always) ---
Write-Host ""
Write-Host "========================================"  -ForegroundColor Red
Write-Host "  [FAILURE] $summary" -ForegroundColor Red
Write-Host "  $detail" -ForegroundColor Red
if ($logPath) {
    Write-Host "  Log: $logPath" -ForegroundColor Yellow
}
Write-Host "========================================"  -ForegroundColor Red
Write-Host ""