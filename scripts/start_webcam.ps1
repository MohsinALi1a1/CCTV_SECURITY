# Streams the PC webcam to the local RTSP server as rtsp://localhost:8554/pc_cam
# Usage:
#   .\scripts\start_webcam.ps1 -ListCameras
#   .\scripts\start_webcam.ps1 -Camera "Integrated Webcam" -ListOptions
#   .\scripts\start_webcam.ps1 -Camera "Integrated Webcam"
param(
    [string]$Camera = "",
    [switch]$ListCameras,
    [string]$Size = "1280x720",
    [int]$InputFps = 30,           # what the webcam delivers (check with -ListOptions)
    [int]$Fps = 15,                # what we send to the RTSP server
    [switch]$ListOptions
)

if ($ListOptions) {
    ffmpeg -hide_banner -f dshow -list_options true -i "video=$Camera" 2>&1 |
        Select-String 'vcodec=|pixel_format=' | ForEach-Object { $_.Line -replace '^\[.*?\]\s*', '' }
    exit 0
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg not found. Install it with:  winget install Gyan.FFmpeg  (then open a new terminal)"
    exit 1
}

if ($ListCameras -or $Camera -eq "") {
    Write-Host "Webcams found on this PC (use the name in quotes):"
    ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1 |
        Select-String '"(.+)" \(video\)' |
        ForEach-Object { "  " + $_.Matches[0].Groups[1].Value }
    exit 0
}

$url = "rtsp://localhost:8554/pc_cam"
$ffmpegArgs = @(
    "-hide_banner", "-loglevel", "error",
    "-f", "dshow", "-rtbufsize", "100M", "-vcodec", "mjpeg", "-video_size", $Size,
    "-framerate", "$InputFps", "-i", "video=`"$Camera`"",
    "-r", "$Fps", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
    "-pix_fmt", "yuv420p", "-g", "$Fps",
    "-f", "rtsp", "-rtsp_transport", "tcp", $url
)

function Test-Stream {
    # True if the RTSP server really has our stream (ffmpeg can hang after Docker restarts)
    & ffprobe -v quiet -rtsp_transport tcp -timeout 5000000 -show_entries stream=codec_name -of csv=p=0 $url 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

Write-Host "Streaming '$Camera' to $url  (Ctrl+C to stop)"
while ($true) {
    $proc = Start-Process ffmpeg -ArgumentList $ffmpegArgs -NoNewWindow -PassThru
    Start-Sleep -Seconds 10                      # give it time to connect
    $failures = 0
    while (-not $proc.HasExited) {
        if (Test-Stream) { $failures = 0 } else { $failures++ }
        if ($failures -ge 2) {
            Write-Host "$(Get-Date -Format T) Stream not reaching the RTSP server - restarting ffmpeg"
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            break
        }
        Start-Sleep -Seconds 15
    }
    Write-Host "$(Get-Date -Format T) ffmpeg stopped. Restarting in 3 seconds... (Ctrl+C to quit)"
    Start-Sleep -Seconds 3
}
