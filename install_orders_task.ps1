param(
    [string]$TaskName = 'WB Orders to Google Sheets - Hourly'
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
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $project
$nextHour = (Get-Date).Date.AddHours((Get-Date).Hour + 1)
$trigger = New-ScheduledTaskTrigger -Once -At $nextHour -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Hourly reconciled WB orders export to explicitly configured sheets. Requires an interactive Windows login.' -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{TaskName=$TaskName;NextRunTime=$info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss');UserMustBeLoggedIn=$true} | ConvertTo-Json -Compress
