You are a senior Flutter developer. Update my existing Flutter app "Smart Tech CCTV" (it already
has Home / Alerts / Cameras tabs, Firebase login and reads Firestore). Give complete files, not
snippets. Use the latest stable Flutter, firebase_core, firebase_auth, cloud_firestore,
firebase_messaging and flutter_local_notifications.

## Backend (already built - do not change it)
A local AI box writes to Firebase project `ai-cctv-system-6f8c1`. The app only READS, except the
two writes listed below. Home id: `home_01` (keep it configurable).

Firestore:
- `homes/{homeId}`: name, mode ("normal" | "backup"), frigate_online (bool),
  last_seen (timestamp, updated every 60 s), heartbeat_seconds (60), today (map level -> count),
  latest_alert (map: id, ts, level, title, message, camera_name, person, has_snapshot),
  settings.share_snapshots (bool, owner's "Share pictures" switch), pictures_shared (bool, what
  the box actually does), storage_available (bool), push_topic, timezone, members (array of uids).
- `homes/{homeId}/cameras/{cameraId}`: name, area ("inside" | "outside"), online (bool|null),
  fps, enabled, set_up, no_entry_areas (string[]), restricted_areas (string[]), updated_at.
- `homes/{homeId}/events/{eventId}`: id, ts (timestamp), ts_unix, date, time_text, level
  ("HIGH" | "MEDIUM" | "LOW" | "INFO"), level_rank (3/2/1/0), kind ("detection" | "camera" |
  "system" | "test"), title, message, rule, camera, camera_name, area, zone, zone_name,
  zone_kind ("no_entry" | "restricted" | null), label, person (name or "Unknown"), known (bool|null),
  score (0-1|null), has_snapshot (bool), snapshot_url (optional), updated (bool),
  acknowledged (bool), acknowledged_by (uid|null), acknowledged_at (timestamp|null).
- `homes/{homeId}/snapshots/{eventId}`: image (Blob, JPEG <= 640 px), width, height,
  content_type, ts. Exists only while the owner allows picture sharing.

Allowed writes (Firestore rules enforce this):
1. Acknowledge: `events/{id}.update({acknowledged: true, acknowledged_by: uid, acknowledged_at: serverTimestamp})`
2. Picture switch: `homes/{homeId}.update({'settings.share_snapshots': bool})`

Push (FCM): the box sends to topic `smarttech_{homeId}` (e.g. `smarttech_home_01`).
notification.title / body / optional image; data: event_id, home_id, level, kind, camera,
camera_name, person, ts, updated ("True" when an earlier alert changed), has_snapshot.
Android channel ids used by the box: `smarttech_urgent` (HIGH, MEDIUM) and `smarttech_info`
(LOW, INFO). The same event id is used as the notification tag, so updates replace the old one.

## What to build / fix
1. Push notifications
   - Ask notification permission (Android 13+ and iOS). Subscribe to the topic after login,
     unsubscribe on logout.
   - Create both Android channels at startup: urgent = max importance, sound, vibration;
     info = default importance.
   - Foreground messages: show them with flutter_local_notifications (FCM does not show them
     in the foreground), using the right channel and the event id as tag.
   - Tapping a notification (app killed, background or foreground) opens that alert's detail
     screen using data.event_id.
   - Register the background handler correctly (top-level function, @pragma('vm:entry-point')).
2. Home screen
   - "AI box online" if last_seen is newer than 3 minutes, otherwise a red "AI box offline -
     last seen <time>" card. Re-evaluate every 30 s even if Firestore sends nothing.
   - Mode badge: normal = "Inside + Outside" (green), backup = "Backup mode - outside camera
     not available" (orange).
   - Today counts (HIGH, MEDIUM, LOW, INFO) and the latest alert card (tap -> detail).
3. Alerts tab
   - Live list ordered by ts desc, limit 50 with "load more". Filter chips: All, High, Medium,
     Low, Info, Unread.
   - Colour by level (HIGH red, MEDIUM orange, LOW blue, INFO grey), a special "NO-ENTRY" badge
     when zone_kind == "no_entry", and an "updated" hint when updated == true.
   - Small thumbnail only when has_snapshot == true, loaded lazily per row.
   - Unread = acknowledged == false (bold). App-icon badge / tab badge with unread HIGH count.
4. Alert detail
   - Full picture (Image.memory from the Blob in snapshots/{id}); tap to zoom. If there is no
     picture show "No picture (sharing is off)".
   - Title, message, time, camera, inside/outside, zone and type, person, face match % (score).
   - "Seen it" button (acknowledge). Hide it once acknowledged and show who/when.
5. Cameras tab
   - Name, inside/outside chip, online/offline/checking, fps, and its No-entry and Restricted
     areas. "Not set up" when set_up == false.
6. Settings screen
   - "Share pictures in alerts" switch bound to settings.share_snapshots. Before turning ON,
     show a dialog: "Pictures may show faces and will be stored in Firebase (Google cloud)."
     Before turning OFF: "Pictures already sent will be deleted." Show pictures_shared next to
     it; if it differs from the switch for more than 10 s show "The AI box has locked this
     setting".
   - Notification toggle (subscribe/unsubscribe the topic), home id, signed-in user, logout.
7. Quality
   - Handle permission-denied (user not in members): show "Your account has no access to this
     home. Ask the owner to add your user id: <uid>" with a copy button.
   - Loading, empty and error states on every screen; works offline with Firestore cache.
   - Times: use ts converted to the phone's local time (not time_text).
   - Keep business logic out of widgets (a small repository/service layer), null-safe parsing
     for every field (fields may be missing on older documents).

Finally list: every file you changed or added, the Android manifest / iOS changes needed for
notifications, and a short manual test plan (foreground push, background push, killed-app
push, tap opens detail, acknowledge, picture switch on/off, AI box offline).
