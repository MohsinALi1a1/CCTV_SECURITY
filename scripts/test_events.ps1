# Sends fake Frigate messages to MQTT to test the Smart Tech rules without walking in front of a camera.
# Watch the result on the dashboard (http://localhost:8080) or with:  docker logs -f smarttech-engine
# Usage:
#   .\scripts\test_events.ps1 -Case unknown     # Unknown person enters restricted zone -> HIGH (after face wait)
#   .\scripts\test_events.ps1 -Case known       # Known person (Ahmed) enters restricted zone -> LOW + INFO
#   .\scripts\test_events.ps1 -Case late-face   # Unknown -> HIGH, then face recognised -> alert updated
#   .\scripts\test_events.ps1 -Case unknown -Zone ""   # Unknown inside the house, not in a zone
#                                                      #   backup mode -> HIGH, normal mode -> MEDIUM
# Note: the same alert is not repeated within cooldown_seconds (rules.yml), so wait between runs.
param(
    [ValidateSet("unknown", "known", "late-face")][string]$Case = "unknown",
    [string]$Camera = "cam01_pc",
    [string]$Zone = "server_room",
    [string]$Person = "Ahmed"
)

$root = Split-Path -Parent $PSScriptRoot
$envFile = Get-Content (Join-Path $root ".env")
$user = ($envFile | Where-Object { $_ -like "MQTT_USER=*" }) -replace '^MQTT_USER=', ''
$pass = ($envFile | Where-Object { $_ -like "MQTT_PASSWORD=*" }) -replace '^MQTT_PASSWORD=', ''

function Publish($topic, $obj) {
    $json = $obj | ConvertTo-Json -Compress -Depth 5
    # Windows PowerShell strips plain quotes when calling programs, so escape them.
    docker exec mosquitto mosquitto_pub -h localhost -u $user -P $pass -t $topic -m ($json -replace '"', '\"')
}

$id = "test-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$zones = @()                                        # -Zone "" = seen on the camera, not in a zone
if ($Zone) { $zones = @($Zone) }
$sub = if ($Case -eq "known") { @($Person, 0.95) } else { $null }
Publish "frigate/events" @{
    type = "new"
    after = @{ id = $id; camera = $Camera; label = "person"; sub_label = $sub; false_positive = $false
               current_zones = $zones; entered_zones = $zones; end_time = $null }
}
$where = if ($Zone) { "in $Zone" } else { "(no zone)" }
Write-Host "Sent: person $id $where on $Camera ($Case)"

if ($Case -eq "late-face") {
    Start-Sleep -Seconds 5
    Publish "frigate/tracked_object_update" @{ type = "face"; id = $id; name = $Person; score = 0.93; camera = $Camera }
    Write-Host "Sent: face recognised as $Person"
}
