# Smart Tech AI CCTV – Mobile app integration

The AI box (Smart Tech event engine) writes every alert to **Cloud Firestore** and sends
**FCM push notifications**. The app only reads. All AI processing and video stay on the
AI box; only alert text (and, if the owner turns it on, a snapshot) goes to Firebase.

Firebase project: `ai-cctv-system-6f8c1` · Home id (default): `home_01`

## 1. Push notifications (FCM)

The box sends to the **topic** `smarttech_<home_id>`, e.g. `smarttech_home_01`.

```dart
await FirebaseMessaging.instance.requestPermission();
await FirebaseMessaging.instance.subscribeToTopic('smarttech_home_01');
```

Which levels are pushed is set on the box (`cloud.push_levels` in `rules.yml`, default HIGH and MEDIUM).

| Part | Content |
|---|---|
| `notification.title` | e.g. `🔴 NO-ENTRY breach · Back Wall Top`, `🔴 HIGH · Unknown · Server Room`, `🔴 Camera offline · Camera 02` |
| `notification.body` | full message, e.g. `Unknown person entered Server Room - Camera 01 - 11:42 PM` |
| `notification.image` | snapshot URL (only if snapshot upload is on) |
| `data.event_id` | Firestore doc id: open `homes/{home_id}/events/{event_id}` |
| `data.home_id`, `data.level`, `data.kind`, `data.camera`, `data.camera_name`, `data.person`, `data.ts` (unix s), `data.updated` ("True" when an earlier alert changed) | strings |

**Android channels** – create both at app start (Android 8+):

| channel_id | Use | Suggested importance |
|---|---|---|
| `smarttech_urgent` | HIGH and MEDIUM | `IMPORTANCE_HIGH` (heads-up, sound, bypass DND if the user allows) |
| `smarttech_info` | LOW and INFO | `IMPORTANCE_DEFAULT` |

Notifications for the same alert use the same `tag` / `collapse_key` / `apns-collapse-id`
(the event id), so an update ("Unknown" → recognised as "Sara") replaces the first notification.

## 2. Firestore structure

```
homes/{home_id}                        ← one document per house (+ settings the app can change)
homes/{home_id}/cameras/{camera_id}    ← one document per camera
homes/{home_id}/events/{event_id}      ← one document per alert
homes/{home_id}/snapshots/{event_id}   ← alert picture, ONLY while the owner allows it
```

### Alert pictures – owner's choice

Pictures are **off by default**. The app shows a switch **"Share pictures"** bound to
`homes/{home_id}.settings.share_snapshots` (bool, created as `false` by the box):

```dart
home.update({'settings.share_snapshots': value});   // allowed by firestore.rules
```

- **On:** every new alert gets a picture in `snapshots/{event_id}` and `events/{id}.has_snapshot = true`.
- **Off:** the box stops sending pictures **and deletes all pictures already in Firebase**
  (`has_snapshot` becomes `false`). The box reacts within a few seconds.
- `homes/{home_id}.pictures_shared` shows what the box is really doing (the box owner can also
  force it off in `rules.yml`, so show this value next to the switch).

`snapshots/{event_id}`: `image` (Blob, JPEG, max 640 px, usually 10–80 KB), `width`, `height`,
`content_type`, `event_id`, `ts`.

```dart
if (event['has_snapshot'] == true) {
  final snap = await home.collection('snapshots').doc(eventId).get();
  if (snap.exists) {
    final bytes = (snap['image'] as Blob).bytes;
    return Image.memory(bytes, fit: BoxFit.cover);
  }
}
```

Load pictures only on the detail screen (or lazily in the list) – don't stream the whole
`snapshots` collection. Pictures appear **inside the push notification** only if Firebase
Storage is enabled in the project (`homes/{home_id}.storage_available == true`); otherwise
the push has text only and the app loads the picture from Firestore when opened.

### `homes/{home_id}`

| Field | Type | Meaning |
|---|---|---|
| `name` | string | "My Home" |
| `engine_online` | bool | always true when written – use `last_seen` to detect offline |
| `last_seen` | timestamp | updated every **60 s** (`heartbeat_seconds`). **If older than ~3 min, show "AI box offline"** (power cut, internet down) |
| `frigate_online` | bool | AI video processing running |
| `mode` | string | `normal` (inside + outside cameras watching) or `backup` (outside camera missing/offline – inside cameras stricter) |
| `today` | map | counts for today, e.g. `{HIGH: 2, INFO: 5}` |
| `latest_alert` | map | newest MEDIUM/HIGH alert: `id, ts, level, title, message, camera_name, person` – for the home screen |
| `settings.share_snapshots` | bool | **the app's "Share pictures" switch** (owner's choice, default false) |
| `pictures_shared` | bool | what the box is actually doing |
| `storage_available` | bool | pictures can be shown inside push notifications |
| `timezone`, `push_topic`, `retention_days` | | info |
| `members` | array of uid | **you add this** (console or your own backend): Firebase Auth uids allowed to read this home |

### `homes/{home_id}/cameras/{camera_id}`

| Field | Type | Meaning |
|---|---|---|
| `name` | string | "Camera 01 (PC Webcam)" |
| `area` | string | `inside` / `outside` |
| `online` | bool/null | null = still checking |
| `fps` | number | frames per second being analysed |
| `enabled`, `set_up` | bool | `set_up=false`: listed in rules but not configured yet |
| `no_entry_areas`, `restricted_areas` | string[] | names of areas drawn on this camera |
| `updated_at` | timestamp | |

### `homes/{home_id}/events/{event_id}`

| Field | Type | Example / meaning |
|---|---|---|
| `id` | string | same as doc id |
| `ts` | timestamp | when it happened – **order by this** |
| `ts_unix`, `date`, `time_text` | number, "2026-09-28", "11:42 PM" | ready to display (box local time) |
| `level` | string | `HIGH` `MEDIUM` `LOW` `INFO` |
| `level_rank` | number | 3 / 2 / 1 / 0 – for filtering "MEDIUM and above" |
| `kind` | string | `detection` (person/car) · `camera` (offline/online) · `system` (Frigate down, backup mode) · `test` |
| `title` | string | short title (same as push) |
| `message` | string | full sentence |
| `rule` | string | which rule fired, e.g. "No-entry area crossed" |
| `camera`, `camera_name`, `area` | string | `area`: inside/outside |
| `zone`, `zone_name`, `zone_kind` | string/null | `zone_kind`: `no_entry` / `restricted` |
| `label` | string | `person`, `car` |
| `person` | string/null | name, or `"Unknown"` |
| `known` | bool/null | face recognised |
| `score` | number/null | face match confidence 0–1 |
| `has_snapshot` | bool | a picture exists in `snapshots/{id}` |
| `snapshot_url` | string/absent | signed image URL – only when Firebase Storage is enabled and pictures are shared (expires after up to 7 days) |
| `snapshot_path` | string/absent | Storage path of the same picture |
| `updated`, `updated_at` | bool, timestamp | alert changed after it was first sent (e.g. person identified later) |
| `acknowledged`, `acknowledged_by`, `acknowledged_at` | bool, uid, timestamp | the app sets these ("Seen it") |

Events are deleted automatically after `retention_days` (default 7), same as on the box.

## 3. Typical queries

```dart
final home = FirebaseFirestore.instance.collection('homes').doc('home_01');

// Live alert list, newest first
home.collection('events').orderBy('ts', descending: true).limit(50).snapshots();

// Only important alerts (needs the index in firebase/firestore.indexes.json)
home.collection('events').where('level_rank', isGreaterThanOrEqualTo: 2)
    .orderBy('level_rank').orderBy('ts', descending: true).limit(50).snapshots();

// Unread alerts badge
home.collection('events').where('acknowledged', isEqualTo: false)
    .orderBy('ts', descending: true).snapshots();

// Home status + cameras
home.snapshots();
home.collection('cameras').snapshots();

// "Seen it"
home.collection('events').doc(eventId).update({
  'acknowledged': true,
  'acknowledged_by': FirebaseAuth.instance.currentUser!.uid,
  'acknowledged_at': FieldValue.serverTimestamp(),
});

// Box offline?
final lastSeen = (homeDoc['last_seen'] as Timestamp).toDate();
final offline = DateTime.now().difference(lastSeen) > const Duration(minutes: 3);
```

## 4. One-time Firebase console setup

1. **Firestore Database → Create database** (production mode, a region near you).
2. **Rules** tab → paste `firebase/firestore.rules` → Publish.
3. **Indexes** → create those in `firebase/firestore.indexes.json` (or `firebase deploy --only firestore:indexes`).
   Firestore also shows a link to create a missing index the first time a query needs it.
4. **Authentication** → enable a sign-in method (e.g. Email/Password) for the app.
5. Add each user's uid to `homes/home_01` → field `members` (array).
6. Optional, for pictures **inside** the notification: **Storage → Get started** (needs the
   Blaze plan) and paste `firebase/storage.rules`. The box detects it after a restart.
   Without Storage, pictures still work in the app (from Firestore).

## 5. Suggested app screens

- **Home:** house name, 🟢/🔴 box online (`last_seen`), mode badge (🛡 normal / ⚠ backup), today's counts, latest alert card.
- **Alerts:** list coloured by `level`, filter chips, tap → detail with snapshot, camera, zone, person, "Seen it".
- **Cameras:** list with inside/outside, online state, areas.
