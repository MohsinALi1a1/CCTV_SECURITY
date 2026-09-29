# Deletes everything stored about one person:
#   - their face photos in Frigate's face library (frigate/storage/clips/faces/<Name>)
#   - their Smart Tech events and snapshots (data/events.db, data/snapshots)
# Usage:
#   .\scripts\delete_person.ps1 -List
#   .\scripts\delete_person.ps1 -Name "Ahmed"
param(
    [string]$Name = "",
    [switch]$List
)

$root = Split-Path -Parent $PSScriptRoot
$faces = Join-Path $root "frigate\storage\clips\faces"

if ($List -or $Name -eq "") {
    Write-Host "People in the face library:"
    if (Test-Path $faces) {
        Get-ChildItem $faces -Directory | Where-Object { $_.Name -ne "train" } |
            ForEach-Object { "  {0}  ({1} photos)" -f $_.Name, (Get-ChildItem $_.FullName -File).Count }
    } else { Write-Host "  (none yet)" }
    exit 0
}

$answer = Read-Host "Delete ALL face data and events for '$Name'? This cannot be undone. Type YES"
if ($answer -ne "YES") { Write-Host "Cancelled."; exit 1 }

$folder = Join-Path $faces $Name
if (Test-Path $folder) {
    Remove-Item -Recurse -Force $folder
    Write-Host "Deleted face photos: $folder"
} else {
    Write-Host "No face photos found for '$Name' (names are case-sensitive; use -List)."
}

docker exec smarttech-engine python -m app.admin delete-person "$Name"

Write-Host "Restarting Frigate so it forgets the face..."
docker restart frigate | Out-Null
Write-Host "Done."
