<#
.SYNOPSIS
  Register (or remove / inspect) the VISION autonomous jobs as Windows Scheduled Tasks.

.DESCRIPTION
  Wires the daily autonomous loop so it runs without babysitting:

    Vision-Council    daily  -> generate a deliberated draft + anime image, email owner
    Vision-Publisher  ~3 min -> publish every APPROVED & due draft to LinkedIn (with image)
    Vision-Expire     daily  -> expire un-actioned drafts at the cutoff
    Vision-Web        logon  -> the approval web server (serves the email Approve links)
    Vision-Retention  weekly -> archive >30d data -> Google Drive (rclone) -> prune + VACUUM

  Nothing publishes without the human: the poller only touches drafts the owner
  approved via the emailed link. Council just emails; expire just cleans up.

  Tasks run under the current user with "run only when logged on" (no stored
  password needed). For a personal always-on PC that is the simplest safe setup.

.PARAMETER Action
  Install (default) | Remove | Status

.PARAMETER CouncilAt   Daily time for the council email (default 08:00).
.PARAMETER ExpireAt    Daily time to expire un-actioned drafts (default 20:00).
.PARAMETER PollMinutes Publisher poll interval in minutes (default 3).
.PARAMETER WebPort     Port for the approval web server (default 8000).
#>
[CmdletBinding()]
param(
  [ValidateSet('Install', 'Remove', 'Status')]
  [string]$Action = 'Install',
  [string]$CouncilAt = '08:00',
  [string]$ExpireAt = '20:00',
  [int]$PollMinutes = 3,
  [int]$WebPort = 8000,
  [string]$RetentionAt = '03:30',
  [ValidateSet('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')]
  [string]$RetentionDay = 'Sunday'
)

$ErrorActionPreference = 'Stop'
$Prefix = 'Vision-'
# Project root = parent of this script's folder. Scripts venv lives at <root>\.venv.
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.venv\Scripts'
$User = "$env:USERDOMAIN\$env:USERNAME"

function Assert-Paths {
  foreach ($exe in 'vision-council.exe', 'vision-publisher.exe', 'vision-expire.exe', 'vision-retention.exe', 'uvicorn.exe') {
    $p = Join-Path $Venv $exe
    if (-not (Test-Path $p)) { throw "Missing venv executable: $p (is the venv built?)" }
  }
}

function New-VisionTask {
  param(
    [string]$Name,
    [string]$Exe,
    [string]$Arguments = '',
    [Microsoft.Management.Infrastructure.CimInstance]$Trigger,
    [switch]$LongRunning
  )
  $full = "$Prefix$Name"
  $exePath = Join-Path $Venv $Exe
  # New-ScheduledTaskAction rejects an empty -Argument, so only pass it when set.
  if ([string]::IsNullOrWhiteSpace($Arguments)) {
    $action = New-ScheduledTaskAction -Execute $exePath -WorkingDirectory $Root
  } else {
    $action = New-ScheduledTaskAction -Execute $exePath -Argument $Arguments -WorkingDirectory $Root
  }
  $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

  # A long-running server (web) must never be killed by an execution-time limit;
  # the short batch jobs get a 1h ceiling and auto-restart a few times on failure.
  if ($LongRunning) {
    $limit = New-TimeSpan -Seconds 0   # 0 = no execution time limit
  } else {
    $limit = New-TimeSpan -Hours 1
  }
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit $limit -MultipleInstances IgnoreNew

  # Idempotent: replace any existing task of the same name.
  Unregister-ScheduledTask -TaskName $full -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
  Register-ScheduledTask -TaskName $full -Action $action -Trigger $Trigger `
    -Principal $principal -Settings $settings -Description "VISION autonomous job: $Name" | Out-Null
  Write-Host "  registered  $full" -ForegroundColor Green
}

function Install-Tasks {
  Assert-Paths
  Write-Host "Installing VISION scheduled tasks (user: $User, root: $Root)" -ForegroundColor Cyan

  # Council: once daily at the chosen time.
  New-VisionTask -Name 'Council' -Exe 'vision-council.exe' `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $CouncilAt)

  # Publisher: repeat every N minutes, indefinitely (the .Repetition graft is the
  # documented way to get an unbounded repeating trigger in PowerShell).
  $pub = New-ScheduledTaskTrigger -Once -At (Get-Date)
  $pub.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
      -RepetitionInterval (New-TimeSpan -Minutes $PollMinutes) `
      -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition
  New-VisionTask -Name 'Publisher' -Exe 'vision-publisher.exe' -Trigger $pub

  # Expire: once daily at the cutoff.
  New-VisionTask -Name 'Expire' -Exe 'vision-expire.exe' `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $ExpireAt)

  # Retention: weekly archive -> Drive backup -> prune (off-hours, low traffic).
  New-VisionTask -Name 'Retention' -Exe 'vision-retention.exe' `
    -Trigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $RetentionDay -At $RetentionAt)

  # Web: the approval server, started at logon and kept alive.
  $webArgs = "vision.approval.web:create_app --factory --host 127.0.0.1 --port $WebPort"
  New-VisionTask -Name 'Web' -Exe 'uvicorn.exe' -Arguments $webArgs `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn) -LongRunning

  Write-Host "`nDone. Starting the web + one publisher pass now..." -ForegroundColor Cyan
  Start-ScheduledTask -TaskName "${Prefix}Web"       -ErrorAction SilentlyContinue
  Start-ScheduledTask -TaskName "${Prefix}Publisher" -ErrorAction SilentlyContinue
  Show-Status
}

function Remove-Tasks {
  Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue | ForEach-Object {
    Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
    Write-Host "  removed  $($_.TaskName)" -ForegroundColor Yellow
  }
}

function Show-Status {
  Write-Host "`nVISION scheduled tasks:" -ForegroundColor Cyan
  $tasks = Get-ScheduledTask -TaskName "$Prefix*" -ErrorAction SilentlyContinue
  if (-not $tasks) { Write-Host '  (none installed)' -ForegroundColor DarkYellow; return }
  $tasks | ForEach-Object {
    $info = $_ | Get-ScheduledTaskInfo
    "{0,-20} state={1,-8} lastRun={2} lastResult={3}" -f `
      $_.TaskName, $_.State, $info.LastRunTime, $info.LastTaskResult | Write-Host
  }
}

switch ($Action) {
  'Install' { Install-Tasks }
  'Remove' { Remove-Tasks }
  'Status' { Show-Status }
}
