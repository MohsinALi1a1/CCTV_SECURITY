"""Firebase sync for the mobile app: Firestore (events, cameras, home status, pictures) + FCM push.

Firestore layout (see docs/MOBILE.md):
  homes/{home_id}                      home status + settings (settings.share_snapshots is set by the app)
  homes/{home_id}/cameras/{camera}     camera name, inside/outside, online, areas
  homes/{home_id}/events/{event_id}    one alert (same fields as the local SQLite event)
  homes/{home_id}/snapshots/{event_id} the alert picture (small JPEG) - ONLY if the owner allowed it

Pictures leave the AI box only when the owner switches on "Share pictures" in the app
(homes/{home_id}.settings.share_snapshots = true). Switching it off deletes every picture
already in Firebase. rules.yml (cloud.upload_snapshots) can also force it on or off.

Push: FCM topic "smarttech_{home_id}". If Firebase Storage is enabled, the picture is also
shown inside the notification.

Everything runs in background queues, so a slow or missing internet
connection never delays local alerts. Failed writes are retried.
"""
import asyncio
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("smarttech.cloud")

LEVEL_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}
LEVEL_ICON = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🔵", "INFO": "⚪"}
MAX_ATTEMPTS = 25          # ~1.5 hours of retries with back-off, then give up on that item
THUMB_WIDTH = 640          # picture size stored in Firestore
THUMB_MAX_BYTES = 700_000  # Firestore documents are limited to 1 MB
HEARTBEAT = 60             # seconds between homes/{id}.last_seen writes (app: offline after 3 min)


class CloudSync:
    def __init__(self, settings, rules, snapshot_dir: Path, fmt_time):
        self.rules = rules
        self.snapshot_dir = snapshot_dir
        self.fmt_time = fmt_time
        self.home_id = re.sub(r"[^A-Za-z0-9_-]", "_", settings.home_id) or "home"
        self.home_name = settings.home_name
        self.topic = f"smarttech_{self.home_id}"
        self.timezone = settings.timezone
        self.enabled = False
        self.connected = False
        self.last_error = ""
        self.app_allows_pictures = False       # homes/{id}.settings.share_snapshots (set by the app)
        self._settings_loaded = False
        self._switch_exists = True             # becomes False if the doc has no settings.share_snapshots
        self._latest_alert: dict | None = None # repeated in every heartbeat once known
        self._beat_event: asyncio.Event | None = None
        self.bucket_ok = False                 # Firebase Storage available -> picture inside the push
        # Two independent queues: a push must never wait for a slow/failing Firestore write.
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=2000)        # Firestore
        self.push_queue: asyncio.Queue = asyncio.Queue(maxsize=500)    # FCM (+ Storage upload)
        self._snapshot_urls: dict[str, str] = {}                       # event id -> Storage image URL

        cred_path = settings.firebase_credentials
        if not settings.firebase_enabled:
            log.info("Firebase sync is turned off (FIREBASE_ENABLED=false)")
            return
        if not cred_path.exists():
            log.info("Firebase sync is off: no credentials file at %s", cred_path)
            return
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore, messaging, storage
            cred = credentials.Certificate(str(cred_path))
            bucket = settings.firebase_bucket or f"{cred.project_id}.firebasestorage.app"
            self._app = firebase_admin.initialize_app(cred, {"storageBucket": bucket})
            self._firestore = firestore
            self._messaging = messaging
            self._storage = storage
            self.db = firestore.client()
            self.home = self.db.collection("homes").document(self.home_id)
            self.enabled = True
            log.info("Firebase sync on: project %s, home %s, push topic %s",
                     cred.project_id, self.home_id, self.topic)
        except Exception as exc:
            log.error("Firebase could not start (%s) - cloud sync is off", exc)

    # ---------- queues ----------

    def _submit(self, label: str, fn, queue: asyncio.Queue | None = None) -> None:
        if not self.enabled:
            return
        try:
            (queue or self.queue).put_nowait((label, fn))
        except asyncio.QueueFull:
            log.warning("Cloud queue full - dropping %s", label)

    def _submit_from_thread(self, label: str, fn) -> None:
        self._loop.call_soon_threadsafe(self._submit, label, fn)

    async def worker(self) -> None:
        if not self.enabled:
            return
        self._beat_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._start_background)
        await asyncio.gather(self._run(self.queue, track=True), self._run(self.push_queue, track=False))

    def _start_background(self) -> None:
        try:
            self.bucket_ok = self._storage.bucket().exists()
        except Exception:
            self.bucket_ok = False
        log.info("Firebase Storage %s", "available: pictures are shown inside push notifications"
                 if self.bucket_ok else "not enabled: pictures are kept in Firestore only")
        try:   # live updates when the owner flips "Share pictures" in the app
            self._watch = self.home.on_snapshot(self._on_home_changed)
        except Exception as exc:
            log.warning("Could not watch app settings (%s) - checking every minute instead", exc)

    async def _run(self, queue: asyncio.Queue, track: bool) -> None:
        while True:
            label, fn = await queue.get()
            delay = 5
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    await asyncio.to_thread(fn)
                    if track:
                        if not self.connected:
                            log.info("Connected to Firestore")
                        self.connected, self.last_error = True, ""
                    break
                except Exception as exc:
                    if track:
                        self.connected, self.last_error = False, str(exc)[:300]
                    if attempt == MAX_ATTEMPTS or _is_permanent(exc):
                        log.error("Cloud: giving up on %s: %s", label, exc)
                        break
                    log.warning("Cloud: %s failed (%s) - retry in %d s", label, str(exc)[:200], delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 300)

    # ---------- picture permission ----------

    def sharing_pictures(self) -> bool:
        """rules.yml cloud.upload_snapshots: app (default) = follow the app switch, true/false = force."""
        mode = (self.rules.data.get("cloud") or {}).get("upload_snapshots", "app")
        if mode is True or mode is False:
            return mode
        return self.app_allows_pictures

    def _on_home_changed(self, docs, _changes, _read_time) -> None:
        data = docs[0].to_dict() if docs and docs[0].exists else {}
        self._apply_settings(data or {})

    def _apply_settings(self, data: dict) -> None:
        settings = data.get("settings") or {}
        self._switch_exists = "share_snapshots" in settings
        allowed = bool(settings.get("share_snapshots", False))
        if allowed == self.app_allows_pictures and self._settings_loaded:
            return
        was = self.app_allows_pictures
        self.app_allows_pictures = allowed
        first = not self._settings_loaded
        self._settings_loaded = True
        if first:
            log.info("App setting: share pictures = %s", allowed)
        else:
            log.warning("Owner turned picture sharing %s in the app", "ON" if allowed else "OFF")
            if was and not allowed:
                self._submit_from_thread("remove shared pictures", self._purge_pictures)
        self.beat_now_from_thread()        # report the new pictures_shared right away

    # ---------- heartbeat (homes/{id}) ----------
    # Runs on its own timer, outside the queues, so alerts, retries or a backlog can never delay it.

    def beat_now(self) -> None:
        """Write the home document now (camera/mode/settings changed). Call from the event loop."""
        if self.enabled and self._beat_event is not None:
            self._beat_event.set()

    def beat_now_from_thread(self) -> None:
        if self.enabled and self._beat_event is not None:
            self._loop.call_soon_threadsafe(self._beat_event.set)

    async def heartbeat(self, get_status) -> None:
        """Writes homes/{id} immediately at start, then every HEARTBEAT seconds (and on changes).
        get_status() -> (status dict, camera list); called in the event loop."""
        if not self.enabled:
            return
        while not hasattr(self, "_loop"):
            await asyncio.sleep(0.1)
        next_due = time.monotonic()
        while True:
            wait = next_due - time.monotonic()
            if wait > 0:
                try:
                    await asyncio.wait_for(self._beat_event.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
            self._beat_event.clear()
            status, cameras = get_status()
            started = time.monotonic()
            try:
                await asyncio.wait_for(asyncio.to_thread(self._write_status, status, cameras), timeout=20)
                next_due = started + HEARTBEAT
            except Exception as exc:
                log.warning("Heartbeat write failed (%s) - retrying in 10 s", str(exc)[:200])
                next_due = time.monotonic() + 10

    # ---------- public API (called from the engine) ----------

    def event(self, event: dict) -> None:
        ev = dict(event)
        self._submit(f"push {ev['id']}", lambda: self._send_push(ev, new=True), self.push_queue)
        self._submit(f"event {ev['id']}", lambda: self._write_event(ev, new=True))

    def event_update(self, event: dict) -> None:
        ev = dict(event)
        self._submit(f"push update {ev['id']}", lambda: self._send_push(ev, new=False), self.push_queue)
        self._submit(f"event update {ev['id']}", lambda: self._write_event(ev, new=False))

    def picture_mode(self) -> str:
        """app | on | off - "on"/"off" mean rules.yml overrides the switch."""
        mode = (self.rules.data.get("cloud") or {}).get("upload_snapshots", "app")
        return "on" if mode is True else "off" if mode is False else "app"

    def set_share_pictures(self, allowed: bool) -> None:
        """Same switch as in the mobile app (homes/{id}.settings.share_snapshots)."""
        self._submit("picture switch", lambda: self.home.set(
            {"settings": {"share_snapshots": bool(allowed)}}, merge=True))

    def cleanup(self, days: int) -> None:
        self._submit("cleanup", lambda: self._cleanup(days))

    def delete_person(self, name: str) -> None:
        self._submit(f"delete person {name}", lambda: self._delete_where("person", name))

    def delete_all(self) -> None:
        self._submit("delete all", lambda: self._delete_where(None, None))

    # ---------- workers (run in a thread) ----------

    def _cloud_settings(self) -> dict:
        cloud = self.rules.data.get("cloud") or {}
        return {"push_levels": [str(x).upper() for x in cloud.get("push_levels", ["HIGH", "MEDIUM"])],
                "push_updates": bool(cloud.get("push_updates", True))}

    def _event_doc(self, ev: dict) -> dict:
        ts = float(ev["ts"])
        local = datetime.fromtimestamp(ts, self.timezone)
        known = ev.get("known")
        return {
            "id": ev["id"],
            "home_id": self.home_id,
            "ts": datetime.fromtimestamp(ts, timezone.utc),
            "ts_unix": ts,
            "date": local.strftime("%Y-%m-%d"),
            "time_text": ev.get("time") or self.fmt_time(ts),
            "level": ev.get("level"),
            "level_rank": LEVEL_RANK.get(ev.get("level"), 0),
            "kind": ev.get("kind"),                        # detection | camera | system | test
            "rule": ev.get("rule"),
            "title": _title(ev),
            "message": ev.get("message"),
            "camera": ev.get("camera"),
            "camera_name": ev.get("camera_name"),
            "area": ev.get("area"),                        # inside | outside
            "zone": ev.get("zone"),
            "zone_name": ev.get("zone_name"),
            "zone_kind": ev.get("zone_kind"),              # no_entry | restricted | None
            "label": ev.get("label"),
            "person": ev.get("person"),
            "known": None if known is None else bool(known),
            "score": ev.get("score"),
            "updated": bool(ev.get("updated", False)),
        }

    def _local_picture(self, ev: dict) -> Path | None:
        path = self.snapshot_dir / f"{ev['id']}.jpg"
        return path if ev.get("snapshot") and path.exists() else None

    def _send_push(self, ev: dict, new: bool) -> None:
        """Push queue: uploads the picture to Storage (only if allowed and available), then sends FCM."""
        image_url = self._snapshot_urls.get(ev["id"])
        picture = self._local_picture(ev)
        if new and image_url is None and picture and self.bucket_ok and self.sharing_pictures():
            image_url = self._upload_to_storage(ev["id"], picture)
            if image_url:
                self._snapshot_urls[ev["id"]] = image_url
                if len(self._snapshot_urls) > 500:
                    self._snapshot_urls.pop(next(iter(self._snapshot_urls)))
                ref = self.home.collection("events").document(ev["id"])
                self._submit_from_thread(f"picture link {ev['id']}", lambda: ref.set(
                    {"snapshot_url": image_url, "snapshot_path": self._storage_path(ev["id"])}, merge=True))
        settings = self._cloud_settings()
        if ev.get("level") in settings["push_levels"] and (new or settings["push_updates"]):
            self._push(self._event_doc(ev), image_url, update=not new)

    def _write_event(self, ev: dict, new: bool) -> None:
        doc = self._event_doc(ev)
        ref = self.home.collection("events").document(ev["id"])

        if new:
            picture = self._local_picture(ev)
            doc["has_snapshot"] = False
            if picture and self.sharing_pictures():
                data, width, height = _thumbnail(picture)
                self.home.collection("snapshots").document(ev["id"]).set({
                    "event_id": ev["id"], "ts": doc["ts"], "content_type": "image/jpeg",
                    "width": width, "height": height, "image": data})
                doc["has_snapshot"] = True
            image_url = self._snapshot_urls.get(ev["id"])
            if image_url:
                doc["snapshot_url"] = image_url
                doc["snapshot_path"] = self._storage_path(ev["id"])
            doc.update(acknowledged=False, acknowledged_by=None, acknowledged_at=None,
                       created_at=self._firestore.SERVER_TIMESTAMP)
            ref.set(doc)
        else:
            doc["updated_at"] = self._firestore.SERVER_TIMESTAMP
            ref.set(doc, merge=True)

        # home document: newest MEDIUM/HIGH alert for the app's home screen
        # (a new important alert, or an update of the alert that is currently shown there)
        current = self._latest_alert or {}
        is_new_important = new and doc["level_rank"] >= LEVEL_RANK["MEDIUM"] and doc["kind"] != "test"
        if is_new_important or (not new and current.get("id") == doc["id"]):
            latest = {k: doc.get(k) for k in ("id", "ts", "level", "title", "message", "camera_name", "person")}
            latest["has_snapshot"] = doc.get("has_snapshot", current.get("has_snapshot", False))
            self._latest_alert = latest
            self.home.set({"latest_alert": latest}, merge=True)

    def _storage_path(self, event_id: str) -> str:
        return f"homes/{self.home_id}/snapshots/{event_id}.jpg"

    def _upload_to_storage(self, event_id: str, picture: Path) -> str | None:
        blob = self._storage.bucket().blob(self._storage_path(event_id))
        blob.upload_from_filename(str(picture), content_type="image/jpeg")
        days = max(1, min(int(self.rules["retention_days"]), 7))   # signed URLs last at most 7 days
        return blob.generate_signed_url(expiration=timedelta(days=days), version="v4")

    def _push(self, doc: dict, image_url: str | None, update: bool) -> None:
        m = self._messaging
        level = doc["level"]
        urgent = level in ("HIGH", "MEDIUM")
        title = ("Update: " if update else "") + doc["title"]
        data = {k: str(v) for k, v in {
            "event_id": doc["id"], "home_id": self.home_id, "level": level, "kind": doc["kind"],
            "camera": doc["camera"] or "", "camera_name": doc["camera_name"] or "",
            "person": doc["person"] or "", "ts": int(doc["ts_unix"]), "updated": update,
            "has_snapshot": self.sharing_pictures() and bool(doc.get("id")),
            "click_action": "FLUTTER_NOTIFICATION_CLICK"}.items()}
        message = m.Message(
            topic=self.topic,
            notification=m.Notification(title=title, body=doc["message"], image=image_url),
            data=data,
            android=m.AndroidConfig(
                priority="high" if urgent else "normal",
                ttl=timedelta(hours=6),
                collapse_key=doc["id"],
                notification=m.AndroidNotification(
                    channel_id="smarttech_urgent" if urgent else "smarttech_info",
                    sound="default", tag=doc["id"],
                    default_vibrate_timings=True, visibility="public")),
            apns=m.APNSConfig(
                headers={"apns-priority": "10" if urgent else "5", "apns-collapse-id": doc["id"][:64]},
                payload=m.APNSPayload(aps=m.Aps(sound="default", thread_id=self.home_id,
                                                mutable_content=bool(image_url)))),
        )
        message_id = m.send(message)
        log.info("Push sent (%s): %s [%s]", level, doc["message"], message_id.split("/")[-1])

    def _write_status(self, status: dict, cameras: list) -> None:
        """One heartbeat: the full homes/{id} document (current schema), then the cameras.
        Not written by the box on purpose: `members` (managed by the owner) and
        `settings.share_snapshots` (the owner's switch; only created once, as false, if missing)."""
        home = {
            "home_id": self.home_id,
            "name": self.home_name,
            "timezone": str(self.timezone),
            "push_topic": self.topic,
            "frigate_online": bool(status.get("frigate")),
            "mode": status.get("mode"),                    # normal | backup
            "today": status.get("today") or {},
            "pictures_shared": self.sharing_pictures(),    # what the box is actually doing
            "storage_available": self.bucket_ok,
            "last_seen": self._firestore.SERVER_TIMESTAMP, # Firestore server clock, not the PC clock
            "heartbeat_seconds": HEARTBEAT,
            # fields from an earlier schema
            "engine_online": self._firestore.DELETE_FIELD,
            "retention_days": self._firestore.DELETE_FIELD,
        }
        if self._latest_alert:
            home["latest_alert"] = self._latest_alert
        if not self._switch_exists:
            home["settings"] = {"share_snapshots": False}  # create the switch for the app, default OFF
            self._switch_exists = True
        self.home.set(home, merge=True)                    # last_seen first - nothing before it can fail

        try:
            # fallback if the live settings watch is not running; also learns the latest alert after a restart
            current = self.home.get().to_dict() or {}
            if self._latest_alert is None and current.get("latest_alert"):
                self._latest_alert = current["latest_alert"]
            self._apply_settings(current)
        except Exception as exc:
            log.debug("Could not read home settings: %s", exc)

        batch = self.db.batch()
        for cam in cameras:
            batch.set(self.home.collection("cameras").document(cam["id"]), {
                "id": cam["id"], "name": cam["name"], "area": cam.get("area"),
                "online": cam.get("online"), "fps": cam.get("fps"),
                "enabled": cam.get("enabled", True), "set_up": not cam.get("missing", False),
                "restricted_areas": cam.get("restricted", []), "no_entry_areas": cam.get("no_entry", []),
                "updated_at": self._firestore.SERVER_TIMESTAMP}, merge=True)
        batch.commit()

    def _purge_pictures(self) -> None:
        """Owner switched sharing off: delete every picture in Firebase."""
        removed = self._delete_query(self.home.collection("snapshots"), with_pictures=False)
        events = self.home.collection("events").where("has_snapshot", "==", True)
        while True:
            docs = list(events.limit(400).stream())
            if not docs:
                break
            batch = self.db.batch()
            for d in docs:
                batch.update(d.reference, {"has_snapshot": False,
                                           "snapshot_url": self._firestore.DELETE_FIELD,
                                           "snapshot_path": self._firestore.DELETE_FIELD})
            batch.commit()
        if self.bucket_ok:
            for blob in self._storage.bucket().list_blobs(prefix=f"homes/{self.home_id}/snapshots/"):
                blob.delete()
        self._snapshot_urls.clear()
        log.warning("Deleted %d shared pictures from Firebase", removed)

    def _cleanup(self, days: int) -> None:
        cutoff = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc)
        removed = self._delete_query(self.home.collection("events").where("ts", "<", cutoff))
        self._delete_query(self.home.collection("snapshots").where("ts", "<", cutoff), with_pictures=False)
        if removed:
            log.info("Cloud retention: deleted %d events older than %d days", removed, days)
        if self.bucket_ok:
            for blob in self._storage.bucket().list_blobs(prefix=f"homes/{self.home_id}/snapshots/"):
                if blob.time_created and blob.time_created < cutoff:
                    blob.delete()

    def _delete_where(self, field: str | None, value) -> None:
        query = self.home.collection("events")
        if field:
            query = query.where(field, "==", value)
        removed = self._delete_query(query)
        if not field:
            self._delete_query(self.home.collection("snapshots"), with_pictures=False)
        log.info("Cloud: deleted %d events%s", removed, f" for {value}" if field else "")

    def _delete_query(self, query, with_pictures: bool = True) -> int:
        """Deletes all matching docs; for events also their picture doc and Storage file."""
        total = 0
        while True:
            docs = list(query.limit(200).stream())
            if not docs:
                return total
            batch = self.db.batch()
            for d in docs:
                batch.delete(d.reference)
                if with_pictures:
                    batch.delete(self.home.collection("snapshots").document(d.id))
                    storage_path = (d.to_dict() or {}).get("snapshot_path")
                    if storage_path and self.bucket_ok:
                        try:
                            self._storage.bucket().blob(storage_path).delete()
                        except Exception:
                            pass
            batch.commit()
            total += len(docs)


def _thumbnail(path: Path) -> tuple[bytes, int, int]:
    """Small JPEG for Firestore (stays well under the 1 MB document limit)."""
    from PIL import Image
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
        for quality in (75, 60, 45, 30):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= THUMB_MAX_BYTES:
                break
        return buf.getvalue(), img.width, img.height


def _title(ev: dict) -> str:
    level = ev.get("level") or "INFO"
    icon = LEVEL_ICON.get(level, "")
    kind = ev.get("kind")
    if kind == "test":
        return f"{icon} Test alert"
    if kind == "camera":
        return f"{icon} Camera {'offline' if level == 'HIGH' else 'online'} · {ev.get('camera_name')}"
    if kind == "system":
        return f"{icon} {ev.get('rule') or 'System'}"
    if ev.get("zone_kind") == "no_entry":
        return f"{icon} NO-ENTRY breach · {ev.get('zone_name')}"
    who = ev.get("person") or (ev.get("label") or "Object").capitalize()
    where = ev.get("zone_name") or ev.get("camera_name")
    return f"{icon} {level} · {who} · {where}"


def _is_permanent(exc: Exception) -> bool:
    name = type(exc).__name__
    return name in ("InvalidArgumentError", "ValueError", "TypeError", "PermissionDeniedError",
                    "UnregisteredError", "SenderIdMismatchError") and "Firestore API" not in str(exc)
