"""Smart Tech event engine: Frigate MQTT -> rules -> clean events (MQTT + SQLite + dashboard)."""
import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import aiomqtt
from aiohttp import web

from .camera_setup import CameraSetup
from .config import ZONE_KINDS, AreaStore, CameraStore, Rules, Settings
from .cloud import CloudSync
from .database import Database
from .engine import AlertEngine
from .frigate_api import FrigateAPI
from .watchdog import CameraWatchdog
from .web import create_app

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("smarttech")

SUBSCRIBE = ["frigate/available", "frigate/events", "frigate/tracked_object_update", "frigate/stats"]

try:
    TZ = ZoneInfo(os.getenv("TZ") or "UTC")
except Exception:
    TZ = ZoneInfo("UTC")


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%I:%M %p").lstrip("0")


def minute_of_day(ts: float) -> int:
    local = datetime.fromtimestamp(ts, TZ)
    return local.hour * 60 + local.minute


class Hub:
    """Pushes live messages to every open dashboard (Server-Sent Events)."""

    def __init__(self):
        self.clients: set[asyncio.Queue] = set()

    def broadcast(self, message: dict) -> None:
        data = json.dumps(message)
        for queue in list(self.clients):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                pass


class App:
    def __init__(self):
        self.settings = Settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.areas = AreaStore(self.settings.data_dir / "areas.json")
        self.cam_store = CameraStore(self.settings.data_dir / "cameras.json")
        self.rules = Rules(self.settings.rules_file, self.areas, self.cam_store)
        self.db = Database(self.settings.data_dir / "events.db")
        self.snapshot_dir = self.settings.data_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.api = FrigateAPI(self.settings.frigate_url)
        self.hub = Hub()
        self.client: aiomqtt.Client | None = None
        self.watchdog = CameraWatchdog(self.rules, self.api, fmt_time, self.on_event,
                                       self.publish_status, self.publish_mode)
        self.engine = AlertEngine(self.rules, self.api, fmt_time, self.on_event, self.on_event_update,
                                  self.watchdog.outside_down, minute_of_day)
        self.cloud = CloudSync(self.settings, self.rules, self.snapshot_dir, fmt_time)
        self.setup = CameraSetup(self, self.settings.camera_secrets_dir, self.settings.lan_subnet)

    def sync_status(self) -> None:
        """Camera/mode changed: send the home + camera status to Firebase now (not only on the next beat)."""
        self.cloud.beat_now()

    # ---------- outputs ----------

    async def publish(self, topic: str, payload, retain: bool = False) -> None:
        if self.client is None:
            return
        body = payload if isinstance(payload, str) else json.dumps(payload)
        try:
            await self.client.publish(topic, body, qos=1, retain=retain)
        except aiomqtt.MqttError as exc:
            log.warning("MQTT publish to %s failed: %s", topic, exc)

    async def on_event(self, event: dict, snapshot_from: tuple | None) -> None:
        if snapshot_from:
            data = await self.api.snapshot(*snapshot_from)
            if data:
                (self.snapshot_dir / f"{event['id']}.jpg").write_bytes(data)
                event["snapshot"] = f"/snapshots/{event['id']}.jpg"
        event["time"] = fmt_time(event["ts"])
        self.db.save(event)
        await self.publish(f"smarttech/events/{event.get('camera') or 'system'}", event)
        self.hub.broadcast({"type": "event", "event": event})
        self.cloud.event(event)
        log.info("[%s] %s", event["level"], event["message"])

    async def on_event_update(self, event: dict) -> None:
        self.db.save(event)
        await self.publish(f"smarttech/events/{event['camera']}", {**event, "updated": True})
        self.hub.broadcast({"type": "update", "event": event})
        self.cloud.event_update({**event, "updated": True})
        log.info("[%s] %s", event["level"], event["message"])

    async def publish_status(self, camera: str, online: bool) -> None:
        await self.publish(f"smarttech/status/{camera}", "online" if online else "offline", retain=True)
        self.hub.broadcast({"type": "cameras"})
        self.sync_status()

    async def publish_mode(self, backup: bool) -> None:
        await self.publish("smarttech/mode", "backup" if backup else "normal", retain=True)
        self.hub.broadcast({"type": "status"})
        self.sync_status()

    async def test_alert(self) -> dict:
        cam = next(iter(self.api.cameras), "test")
        zones = self.rules.restricted(cam)
        zone = zones[0] if zones else None
        now = time.time()
        where = f"entered {self.api.zone_name(cam, zone)} - " if zone else "at "
        event = {"id": uuid.uuid4().hex, "ts": now, "kind": "test", "level": "HIGH", "rule": "Test alert",
                 "camera": cam, "camera_name": self.api.camera_name(cam), "zone": zone,
                 "zone_name": self.api.zone_name(cam, zone) if zone else None, "label": "person",
                 "person": "Unknown", "known": 0,
                 "message": f"TEST - Unknown person {where}{self.api.camera_name(cam)} - {fmt_time(now)}"}
        data = await self.api.latest(cam, 720)
        if data:
            (self.snapshot_dir / f"{event['id']}.jpg").write_bytes(data)
            event["snapshot"] = f"/snapshots/{event['id']}.jpg"
        await self.on_event(event, None)
        return event

    # ---------- areas drawn in the dashboard ----------

    def list_areas(self, camera: str) -> list[dict]:
        info = self.api.cameras.get(camera, {})
        kinds = self.rules.zone_kinds(camera)
        return [{"id": z, "name": name, "kind": kinds.get(z, "none"), "points": info["shapes"].get(z, [])}
                for z, name in info.get("zones", {}).items()]

    async def save_area(self, camera: str, name: str, kind: str, points: list, zone: str | None) -> dict:
        if camera not in self.api.cameras:
            raise ValueError("Unknown camera")
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("Please give the area a name")
        if kind not in ZONE_KINDS:
            raise ValueError("Area type must be no_entry or restricted")
        try:
            # Frigate rejects 1.0 ("must be relative"), so keep points just inside the picture
            pts = [[min(max(float(x), 0.0), 0.999), min(max(float(y), 0.0), 0.999)] for x, y in points]
        except (TypeError, ValueError):
            raise ValueError("Bad points") from None
        if not 3 <= len(pts) <= 40:
            raise ValueError("Click at least 3 points around the area")

        existing = self.api.cameras[camera]["zones"]
        if zone and zone in existing:
            zone_id = zone                                   # editing an existing area
        else:
            base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "area"
            if base[0].isdigit() or base == camera:
                base = f"area_{base}"
            zone_id, n = base, 2
            while zone_id in existing:
                zone_id, n = f"{base}_{n}", n + 1

        ok, msg = await self.api.save_zone(camera, zone_id, name, pts)
        if not ok:
            raise ValueError(f"Frigate refused the area: {msg}")
        self.areas.set(camera, zone_id, kind)
        log.info("Area saved: %s / %s (%s)", camera, zone_id, kind)
        self.hub.broadcast({"type": "cameras"})
        return {"id": zone_id, "name": name, "kind": kind, "points": pts}

    async def delete_area(self, camera: str, zone: str) -> None:
        if zone not in self.api.cameras.get(camera, {}).get("zones", {}):
            raise ValueError("Unknown area")
        ok, msg = await self.api.delete_zone(camera, zone)
        if not ok:
            raise ValueError(f"Frigate refused: {msg}")
        self.areas.remove(camera, zone)
        log.info("Area deleted: %s / %s", camera, zone)
        self.hub.broadcast({"type": "cameras"})

    # ---------- info for the dashboard ----------

    def camera_list(self) -> list[dict]:
        cams = []
        for cam, info in self.api.cameras.items():
            kinds = {z: k for z, k in self.rules.zone_kinds(cam).items() if z in info["zones"]}
            restricted = [self.api.zone_name(cam, z) for z, k in kinds.items() if k == "restricted"]
            no_entry = [self.api.zone_name(cam, z) for z, k in kinds.items() if k == "no_entry"]
            cams.append({"id": cam, "name": info["name"], "enabled": info.get("enabled", True),
                         "area": self.rules.area(cam), "missing": False,
                         "host": self.cam_store.data.get(cam, {}).get("host"),
                         "removable": cam in self.cam_store.data,
                         "restricted": restricted, "no_entry": no_entry, **self.watchdog.camera_state(cam)})
        # Cameras listed in rules.yml that Frigate doesn't have (not installed yet / typo).
        # Only once Frigate's camera list is known - right after a restart it is still empty.
        for cam in (self.rules["cameras"] if self.api.cameras else []):
            if cam not in self.api.cameras:
                cams.append({"id": cam, "name": cam, "enabled": True, "area": self.rules.area(cam),
                             "missing": True, "restricted": self.rules.restricted(cam), "no_entry": [],
                             "online": False, "fps": 0})
        return cams

    def status(self) -> dict:
        start_of_day = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {"mqtt": self.client is not None, "frigate": bool(self.watchdog.frigate_up),
                "mode": "backup" if self.watchdog.outside_down() else "normal",
                "outside_cameras": self.rules.outside_cameras(),
                "frigate_ui": self.settings.frigate_ui_url, "notify_levels": self.rules["notify_levels"],
                "face_wait_seconds": self.rules["face_wait_seconds"],
                "retention_days": self.rules["retention_days"],
                "cloud": {"enabled": self.cloud.enabled, "connected": self.cloud.connected,
                          "home_id": self.cloud.home_id, "push_topic": self.cloud.topic,
                          "pictures": self.cloud.enabled and self.cloud.sharing_pictures(),
                          "pictures_mode": self.cloud.picture_mode(),
                          "pictures_allowed": self.cloud.app_allows_pictures,
                          "error": self.cloud.last_error},
                "today": self.db.counts_since(start_of_day)}

    # ---------- loops ----------

    async def mqtt_loop(self) -> None:
        s = self.settings
        while True:
            try:
                async with aiomqtt.Client(
                        hostname=s.mqtt_host, port=s.mqtt_port, username=s.mqtt_user, password=s.mqtt_password,
                        identifier=f"smarttech-engine-{uuid.uuid4().hex[:6]}",
                        will=aiomqtt.Will("smarttech/engine/status", "offline", qos=1, retain=True)) as client:
                    self.client = client
                    for topic in SUBSCRIBE:
                        await client.subscribe(topic, qos=1)
                    await self.publish("smarttech/engine/status", "online", retain=True)
                    log.info("Connected to MQTT at %s:%s", s.mqtt_host, s.mqtt_port)
                    self.hub.broadcast({"type": "status"})
                    async for message in client.messages:
                        self.handle(str(message.topic), message.payload)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection lost (%s) - retrying in 5 s", exc)
            self.client = None
            self.hub.broadcast({"type": "status"})
            await asyncio.sleep(5)

    def handle(self, topic: str, payload) -> None:
        try:
            text = payload.decode("utf-8-sig") if isinstance(payload, (bytes, bytearray)) else str(payload)
            if topic == "frigate/available":
                self.watchdog.on_available(text)
                return
            data = json.loads(text)
            if topic == "frigate/events":
                self.engine.on_frigate_event(data)
            elif topic == "frigate/tracked_object_update" and data.get("type") == "face":
                self.engine.on_face_update(data)
            elif topic == "frigate/stats":
                self.watchdog.on_stats(data)
        except Exception:
            log.exception("Could not handle message on %s", topic)

    async def tick_loop(self) -> None:
        while True:
            try:
                if self.rules.reload_if_changed():
                    self.hub.broadcast({"type": "cameras"})
                await self.engine.tick()
                await self.watchdog.tick()
            except Exception:
                log.exception("Error in rule check")
            await asyncio.sleep(0.5)

    async def housekeeping_loop(self) -> None:
        last_cleanup = 0.0
        while True:
            try:
                if await self.api.refresh_config():
                    self.hub.broadcast({"type": "cameras"})
                if time.time() - last_cleanup > 3600:
                    self.cleanup()
                    last_cleanup = time.time()
            except Exception:
                log.exception("Housekeeping failed")
            await asyncio.sleep(5 if not self.api.cameras else 300)   # retry fast until Frigate answers

    def cleanup(self) -> None:
        """Deletes events and snapshots older than retention_days."""
        cutoff = time.time() - self.rules["retention_days"] * 86400
        removed = self.db.delete_older_than(cutoff)
        for path in removed:
            (self.snapshot_dir / os.path.basename(path)).unlink(missing_ok=True)
        for file in self.snapshot_dir.glob("*.jpg"):     # orphans
            if file.stat().st_mtime < cutoff:
                file.unlink(missing_ok=True)
        if removed:
            log.info("Retention: deleted %d events older than %d days", len(removed), self.rules["retention_days"])
        self.cloud.cleanup(self.rules["retention_days"])

    async def heartbeat_loop(self) -> None:
        """homes/{id}.last_seen for the mobile app: at start-up, then every 60 s and on every change."""
        await self.cloud.heartbeat(lambda: (self.status(), self.camera_list()))

    async def run(self) -> None:
        await self.api.start()
        await self.api.refresh_config()
        runner = web.AppRunner(create_app(self))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", self.settings.web_port).start()
        log.info("Dashboard running on port %d", self.settings.web_port)
        await asyncio.gather(self.mqtt_loop(), self.tick_loop(), self.housekeeping_loop(),
                             self.heartbeat_loop(), self.cloud.worker())


if __name__ == "__main__":
    asyncio.run(App().run())
