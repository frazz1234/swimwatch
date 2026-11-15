# Creates the Windows task "SwimWatch": starts at boot, no login needed (S4U), no time limit.
# Also opens port 8765 on the home network for the web page.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$action = New-ScheduledTaskAction -Execute "$dir\run-swimwatch.cmd" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = "PT2M"   # after the HA VM (started 1 min after boot)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "SwimWatch" -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description "SwimWatch: is someone swimming in the pool" -Force | Out-Null
if (-not (Get-NetFirewallRule -DisplayName "SwimWatch 8765" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "SwimWatch 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 `
    -Action Allow -Profile Private,Domain -RemoteAddress LocalSubnet | Out-Null
}
Write-Output "task + firewall ok"
