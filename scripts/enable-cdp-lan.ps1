# Run once as administrator. All registered instances are synchronized every 5 seconds.
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv\Scripts\python.exe'
$worker = Join-Path $PSScriptRoot 'sync-cdp-lan.py'
Start-Service iphlpsvc
$action = New-ScheduledTaskAction -Execute $python -Argument "`"$worker`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'ChromeManager-CDP-LAN' -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Stop-ScheduledTask -TaskName 'ChromeManager-CDP-LAN'
Start-ScheduledTask -TaskName 'ChromeManager-CDP-LAN'
