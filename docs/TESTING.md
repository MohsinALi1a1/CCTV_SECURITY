# Testing Smart Tech AI CCTV

Keep the dashboard open (http://localhost:8080) with notifications allowed, and optionally
watch the logs in a second window: `docker logs -f smarttech-engine`.

Tip: the same alert is not repeated within `cooldown_seconds` (60 s), so wait a minute between tests.

## 1. Notifications work
Press **Test alert** on the dashboard.
**Expected:** red banner, beep, a Windows notification and a `TEST` row in the list.

## 2. Person enters the zone (real camera)
Stand in the **right half** of the webcam picture (the Server Room zone) for a few seconds.
**Expected:** within about 3 seconds, **HIGH** "Unknown person entered Server Room" if your face is
not in the library, or **LOW** "Known person (your name) in Server Room" if it is.

## 3. Known face
1. Frigate → Face Library → add your name with 5–10 photos.
2. Walk in front of the camera (not in the zone).

**Expected:** **INFO** "Known person (your name) at Camera 01".

## 4. Unknown face
Cover the face-library match (e.g. have someone not in the library walk in, or hold a photo of a
stranger up to the webcam) and enter the zone.
**Expected:** **HIGH** alert with a snapshot.

## 5. Tests without a camera (fake Frigate messages)
```powershell
.\scripts\test_events.ps1 -Case unknown      # HIGH after 3 s
.\scripts\test_events.ps1 -Case known        # LOW + INFO for "Ahmed"
.\scripts\test_events.ps1 -Case late-face    # HIGH, then updated to LOW when the face is matched
```
Delete fake events afterwards: `docker exec smarttech-engine python -m app.admin delete-all`

## 6. Backup mode (inside takes over)
With no outside camera (as now), the dashboard shows **⚠ Inside only**.
```powershell
.\scripts\test_events.ps1 -Case unknown -Zone ""     # unknown person inside, no zone
```
**Expected:** **HIGH** "Unknown person inside the house". Once a working outside camera is
added (`area: outside`), the same test gives **MEDIUM**, and unplugging that outside camera
brings back HIGH plus a "Backup mode ON" alert.

## 7. Camera unplugged
Close the `start_webcam.ps1` window (or unplug a real camera's network cable).
**Expected:** after 45 s, **HIGH** "Camera 01 is OFFLINE", and the camera card turns dark.
Start the stream again → **INFO** "Camera 01 is back online".

## Watching MQTT live
- **MQTT Explorer** (easiest): connect to `localhost:1883` with the user/password from `.env`.
- **Command line:**
  ```powershell
  docker exec mosquitto mosquitto_sub -h localhost -u smarttech -P <password> -t "smarttech/#" -v
  docker exec mosquitto mosquitto_sub -h localhost -u smarttech -P <password> -t "frigate/events" -v
  ```

## Common problems

| Problem | Fix |
|---|---|
| Dashboard says "Frigate not responding" | `docker logs frigate` – usually a mistake in `config.yml` |
| No live picture / camera offline | Is `start_webcam.ps1` running? Check `rtsp://localhost:8554/pc_cam` in VLC |
| ffmpeg "Could not set video options" | Run `start_webcam.ps1 -ListOptions -Camera "Integrated Webcam"` and pick a listed size |
| No pop-up notification | Click "Allow notifications"; check Windows Settings → Notifications → your browser is on, and Focus / Do not disturb is off |
| No sound | Click anywhere on the page once (browsers block sound until you interact) |
| Family member reported as Unknown | Add more face photos (different angles and light), or raise `face_wait_seconds` |
| Stranger given a family name | Raise `recognition_threshold` in `config.yml` (e.g. 0.92) and remove bad photos from the Face Library |
| Too many alerts | Raise `cooldown_seconds`, or make the zone smaller |
| Changed `rules.yml` but nothing changed | `docker logs smarttech-engine` – a typing mistake is reported and the old rules are kept |
