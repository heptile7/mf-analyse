# Registers a monthly Windows Task Scheduler job for the MF Analyse check-in.
# Runs on the 1st of every month at 09:00 as the current user (needed for the
# notification) and catches up at the next chance if the PC was off.
#   Register:  powershell -ExecutionPolicy Bypass -File scripts\register_checkin_task.ps1
#   Remove:    schtasks /Delete /TN "MF Analyse check-in" /F
# Uses the Task Scheduler COM API: New-ScheduledTaskTrigger has no monthly trigger.
$ErrorActionPreference = "Stop"
$root   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$main   = Join-Path $root "MF_analyser.py"
if (-not (Test-Path $python)) { throw "Virtualenv python not found at $python" }

$svc = New-Object -ComObject Schedule.Service
$svc.Connect()
$task = $svc.NewTask(0)
$task.RegistrationInfo.Description = "Monthly MF Analyse holdings check-in (notifies only on change)"
$task.Settings.StartWhenAvailable = $true
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
$task.Settings.ExecutionTimeLimit = "PT1H"
$task.Principal.LogonType = 3                     # interactive token: run as me, only when logged on

$trigger = $task.Triggers.Create(4)              # 4 = TASK_TRIGGER_MONTHLY
$trigger.DaysOfMonth = 1                          # bitmask: day 1
$trigger.MonthsOfYear = 0xFFF                     # every month
$trigger.StartBoundary = (Get-Date -Hour 9 -Minute 0 -Second 0).ToString("yyyy-MM-dd'T'HH:mm:ss")

$exec = $task.Actions.Create(0)                  # 0 = TASK_ACTION_EXEC
$exec.Path = $python
$exec.Arguments = "`"$main`" --check-in"
$exec.WorkingDirectory = $root

$svc.GetFolder("\").RegisterTaskDefinition("MF Analyse check-in", $task, 6, $null, $null, 3) | Out-Null
Write-Host "Registered 'MF Analyse check-in' (1st of each month, 09:00)."
