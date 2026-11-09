<#
.SYNOPSIS
    Registers local Windows Scheduled Tasks for Antigravity NSE daily market updates.

.DESCRIPTION
    Creates two zero-cloud-latency scheduled tasks in Windows Task Scheduler:
      1. 'Antigravity_NSE_Morning_0930' -> Runs at 09:30 AM IST (Mon-Fri)
      2. 'Antigravity_NSE_Evening_1748' -> Runs at 05:48 PM IST (Mon-Fri)

    These run locally using your Indian ISP with direct, low-latency access to NSE,
    dispatching Telegram & Email alerts immediately to your mobile device even if
    GitHub Actions experiences cloud queue delays.

.EXAMPLE
    .\pipeline\tools\register_windows_tasks.ps1 -Register
    .\pipeline\tools\register_windows_tasks.ps1 -Unregister
#>

param(
    [Parameter(Mandatory=$false)]
    [switch]$Register,

    [Parameter(Mandatory=$false)]
    [switch]$Unregister
)

$WorkspaceRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$PythonExe = (Get-Command python.exe).Source

if (-not $PythonExe) {
    Write-Error "Python executable not found in PATH."
    exit 1
}

$MorningTaskName = "Antigravity_NSE_Morning_0930"
$EveningTaskName = "Antigravity_NSE_Evening_1748"

if ($Unregister) {
    Write-Host "Unregistering Antigravity NSE Scheduled Tasks..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $MorningTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $EveningTaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "[OK] Tasks removed." -ForegroundColor Green
    exit 0
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  Registering Local Antigravity NSE Scheduled Tasks" -ForegroundColor Cyan
Write-Host "  Workspace: $WorkspaceRoot" -ForegroundColor Gray
Write-Host "  Python:    $PythonExe" -ForegroundColor Gray
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Morning 09:30 AM Task
$MorningAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "pipeline/tools/run_daily.py --morning --notify --top 5" -WorkingDirectory $WorkspaceRoot
$MorningTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "09:30AM"
$MorningSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $MorningTaskName -Action $MorningAction -Trigger $MorningTrigger -Settings $MorningSettings -Description "NSE 09:30 AM Morning Swing Entry Check & Mobile Alerts" -Force | Out-Null
Write-Host "[OK] Registered: $MorningTaskName (09:30 AM Mon-Fri)" -ForegroundColor Green

# 2. Evening 05:48 PM Task
$EveningAction = New-ScheduledTaskAction -Execute $PythonExe -Argument "pipeline/tools/run_daily.py --notify --top 5" -WorkingDirectory $WorkspaceRoot
$EveningTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "05:48PM"
$EveningSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $EveningTaskName -Action $EveningAction -Trigger $EveningTrigger -Settings $EveningSettings -Description "NSE Daily Evening Screening & Mobile Alerts" -Force | Out-Null
Write-Host "[OK] Registered: $EveningTaskName (05:48 PM Mon-Fri)" -ForegroundColor Green

Write-Host "`nBoth tasks are active in Windows Task Scheduler. Your alerts will now dispatch with zero delay!" -ForegroundColor Green
