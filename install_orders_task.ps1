param(
    [string]$TaskName = 'WB Orders to Google Sheets - Daily 08 and 20 MSK'
)
$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\pythonw.exe'
$script = Join-Path $project 'export_orders.py'
$config = Join-Path $project 'orders.local.json'
foreach ($path in @($python,$script,$config)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Required local project file is missing' }
}
if ((Get-TimeZone).Id -ne 'Russian Standard Time') { throw 'This installer expects the Moscow Windows time zone' }
$arguments = '-B "{0}" --settings "{1}" --apply' -f $script,$config
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $python -or $existing.Actions[0].Arguments -ne $arguments) {
        throw 'A different task already uses this name; it will not be replaced'
    }
}
$legacyName = 'WB Orders to Google Sheets - Hourly'
$legacy = $null
if ($TaskName -ne $legacyName) {
    $legacy = Get-ScheduledTask -TaskName $legacyName -ErrorAction SilentlyContinue
    if ($legacy -and ($legacy.Actions.Count -ne 1 -or $legacy.Actions[0].Execute -ne $python -or $legacy.Actions[0].Arguments -ne $arguments)) {
        throw 'A different legacy hourly task exists; it will not be removed'
    }
    if ($legacy -and $legacy.State -eq 'Running') { throw 'Wait until the active hourly export completes before migration' }
}
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
$triggers = @((New-ScheduledTaskTrigger -Daily -At '08:00'), (New-ScheduledTaskTrigger -Daily -At '20:00'))
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal -Description 'WB orders export daily at 08:00 and 20:00 Moscow time. Requires an interactive Windows login.' -Force | Out-Null
if ($legacy) {
    $created = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($created.Triggers.Count -ne 2) { throw 'Daily task was not registered correctly; hourly task retained' }
    $runs = Join-Path $project 'runs'
    New-Item -ItemType Directory -Path $runs -Force | Out-Null
    Export-ScheduledTask -TaskName $legacyName | Set-Content -LiteralPath (Join-Path $runs 'orders-previous-task.xml') -Encoding UTF8
    Unregister-ScheduledTask -TaskName $legacyName -Confirm:$false
}
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{TaskName=$TaskName;NextRunTime=$info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss');UserMustBeLoggedIn=$true} | ConvertTo-Json -Compress
