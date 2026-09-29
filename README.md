# Smart Tech AI CCTV

Local AI for home cameras. Everything (video, faces, events) stays on this machine.

| Part | What it does | Where |
|---|---|---|
| **Smart Tech dashboard** | Live cameras, alerts, pop-up notifications, sound | http://localhost:8080 |
| Frigate (advanced) | Detection (YOLOv9 + OpenVINO), zones, face library, recordings | https://localhost:8971 |
| Mosquitto | MQTT messages between the parts | localhost:1883 |
| Event engine | Zone + face rules → HIGH / LOW / INFO alerts, SQLite history, camera watchdog | `event_engine/` |

**Installing on a new PC?** Follow [docs/SETUP_NEW_PC.md](docs/SETUP_NEW_PC.md) step by step.

## Start

```powershell
docker compose up -d                                        # start everything
.\scripts\start_webcam.ps1 -Camera "Integrated Webcam"      # PC webcam only - leave this window open
```

Open **http://localhost:8080** → click **Allow notifications** → press **Test alert**.
You should see a red banner, hear a beep and get a Windows notification.

First time only: copy `.env.example` to `.env` and set the passwords. The Frigate `admin`
password is printed once in its log: `docker logs frigate 2>&1 | Select-String "Password:"`.

## Everyday tasks

| I want to… | Do this |
|---|---|
| Add a family member | Frigate → **Face Library** → Add face → upload 5–10 clear, front-facing, colour photos |
| Mark a way in that nobody may use (wall top, window, gate) | Dashboard → camera → **✏ Draw areas** → click points → **🚫 No-entry** → Save |
| Mark an area only strangers may not enter | Same, but choose **⚠ Restricted** |
| Move or delete an area | Dashboard → **✏ Draw areas** → Edit / Delete in the list |
| Change alert rules | Edit `event_engine/config/rules.yml` and save (applies within a second) |
| Turn face recognition off | `frigate/config/config.yml` → `face_recognition: enabled: false` → `docker restart frigate` |
| Delete a person's data | `.\scripts\delete_person.ps1 -Name "Ahmed"` |
| Change how long data is kept | `retention_days` in `rules.yml` and `retain` days in `config.yml` |
| See engine logs | `docker logs -f smarttech-engine` |

## Inside + outside protection

Each camera is marked `area: outside` or `area: inside` in `event_engine/config/rules.yml`.

| Mode (shown on the dashboard) | When | What changes |
|---|---|---|
| 🛡 **Inside + Outside** | All outside cameras are sending video | Outside cameras catch people on the wall/lawn/gate (HIGH). An unknown person inside is MEDIUM. |
| ⚠ **Backup** | No outside camera, or any outside camera offline | Inside cameras take over: **any** unknown person inside is HIGH. |

Switching between modes is automatic and announced as an alert.

## Alert levels (default rules)

- **HIGH** – **Anyone** (family too) at a 🚫 No-entry area, instantly · Unknown person enters a restricted zone · unknown person inside in backup mode · camera offline · Frigate down
- **MEDIUM** – Unknown person inside while outside cameras are watching · backup mode switched on
- **LOW** – Known person enters a restricted zone
- **INFO** – Known person seen · camera back online · backup mode off

A person is only "Unknown" after face recognition has had `face_wait_seconds` (3 s) to find a
match. If they are recognised later, the alert is updated with their name.

## Mobile app (Firebase)

Every alert is also written to Firestore (`homes/<HOME_ID>/events`) and HIGH/MEDIUM alerts are
pushed with FCM to topic `smarttech_<HOME_ID>`, so the owner is notified anywhere.
The key lives in `secrets/firebase-service-account.json` (never share or commit it).
Alert pictures stay on this machine unless the owner turns on **Share pictures** in the app
(`homes/<HOME_ID>.settings.share_snapshots`); turning it off deletes them from Firebase.
`cloud: upload_snapshots: false` in `rules.yml` blocks pictures completely.
Structure, push format and console setup for the app developers: [docs/MOBILE.md](docs/MOBILE.md).

## MQTT topics (for other apps)

- `smarttech/events/<camera>` – every alert as JSON (time, camera, zone, person, level, message, snapshot)
- `smarttech/status/<camera>` – `online` / `offline` (retained)
- `smarttech/engine/status` – `online` / `offline` (retained)
- `smarttech/mode` – `normal` / `backup` (retained)

## Moving to the Intel N100 (Ubuntu)

1. In `docker-compose.yml`, uncomment the `devices: /dev/dri/renderD128` lines under `frigate`.
2. In `config.yml`, set `detectors: ov: device: GPU` and add `ffmpeg: hwaccel_args: preset-vaapi`.
3. Replace `cam01_pc` with your IP cameras (see the commented `cam02_back` example) and remove the `rtsp-server` service.

See [docs/TESTING.md](docs/TESTING.md) for step-by-step tests and common problems.
