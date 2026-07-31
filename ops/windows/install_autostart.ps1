$ErrorActionPreference = "Stop"
$launcher = $env:NODE1_LAUNCHER
if ([string]::IsNullOrWhiteSpace($launcher) -or -not (Test-Path $launcher)) {
    throw "Set NODE1_LAUNCHER to ops\\windows\\start_node1.cmd"
}
$action = New-ScheduledTaskAction -Execute $launcher
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName "Hermes Node1" -Action $action -Trigger $trigger -Principal $principal -Force
