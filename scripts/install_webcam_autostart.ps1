# Starts the PC-webcam stream automatically at Windows login (hidden, no window).
# Usage:
#   .\scripts\install_webcam_autostart.ps1              # install (or update)
#   .\scripts\install_webcam_autostart.ps1 -Remove      # uninstall
# Not needed once real IP cameras are used.
param(
    [string]$Camera = "Integrated Webcam",
    [switch]$Remove
)

$taskName = "SmartTech Webcam Stream"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed '$taskName'."
    exit 0
}

$script = Join-Path $PSScriptRoot "start_webcam.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Camera `"$Camera`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"      # give Docker Desktop a minute to start
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Smart Tech AI CCTV: streams the PC webcam to rtsp://localhost:8554/pc_cam" -Force | Out-Null
Write-Host "Installed '$taskName' - the webcam stream now starts 1 minute after you log in."
