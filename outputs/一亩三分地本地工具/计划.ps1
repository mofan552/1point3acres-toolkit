[CmdletBinding(SupportsShouldProcess = $true)]
param([ValidateSet('Install', 'Status', 'Remove')][string]$Action = 'Status')

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows Task Scheduler.' }

$taskName = '1point3acres-toolkit-daily'
$workspacePath = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$pythonPath = Join-Path $workspacePath 'work\cf-probe-venv\Scripts\python.exe'
$windowlessPython = Join-Path $workspacePath 'work\cf-probe-venv\Scripts\pythonw.exe'
$cliPath = Join-Path $PSScriptRoot 'cli.py'
$taskArguments = '-X utf8 "{0}" daily --resume' -f $cliPath
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
$owned = $null -eq $existing -or (@($existing.Actions).Count -eq 1 -and
    $existing.Actions[0].Execute -eq $windowlessPython -and $existing.Actions[0].Arguments -eq $taskArguments)

if ($Action -ne 'Status' -and -not $owned) {
    throw 'The task name belongs to another checkout. Inspect it before changing it.'
}
if ($Action -eq 'Remove') {
    if ($existing -and $PSCmdlet.ShouldProcess($taskName, 'Remove this checkout daily task')) {
        Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null
        $scheduler = New-Object -ComObject 'Schedule.Service'
        $scheduler.Connect()
        if ($scheduler.GetFolder('\').GetTask($taskName).GetInstances(0).Count -gt 0) {
            throw 'Future triggers are disabled, but a run is active. Wait for it to finish, then run Remove again before updating or moving files.'
        }
        Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false
    }
    return
}
if ($Action -eq 'Install') {
    if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $windowlessPython)) {
        throw 'Install the documented Python environment first.'
    }
    $rawInfo = & $pythonPath -X utf8 $cliPath info
    if ($LASTEXITCODE -ne 0) { throw 'Offline runtime inspection failed. Run the info command first.' }
    $info = $rawInfo | ConvertFrom-Json
    if (-not $info.account_configured) { throw 'Configure the local account before enabling the daily task.' }
    $interval = [TimeSpan]::FromSeconds($info.schedule.poll_seconds)
    $limit = [TimeSpan]::FromSeconds($info.daily_run_timeout * ($info.daily_retry_limit + 1) + 120)
    $taskAction = New-ScheduledTaskAction -Execute $windowlessPython -Argument $taskArguments -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval $interval
    $taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit $limit
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited
    if ($PSCmdlet.ShouldProcess($taskName, 'Install or update the windowless daily task')) {
        Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $taskAction -Trigger $trigger `
            -Settings $taskSettings -Principal $principal -Description 'Local daily resume; business times come from toolkit settings.' -Force | Out-Null
    }
    $existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
}

if ($null -eq $existing) {
    [ordered]@{ installed = $false; task_name = $taskName } | ConvertTo-Json
} else {
    $history = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath '\'
    [ordered]@{ installed = $true; task_name = $taskName; state = [string]$existing.State
        current_checkout = $owned; interval = if ($owned) { [string]$existing.Triggers[0].Repetition.Interval } else { $null }
        next_run_utc = $history.NextRunTime.ToUniversalTime().ToString('o')
        last_result = $history.LastTaskResult } | ConvertTo-Json
}
