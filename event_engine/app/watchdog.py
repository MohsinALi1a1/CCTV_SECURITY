"""Detects cameras that stop sending video, and Frigate itself going down.

Uses Frigate's stats (published on MQTT every stats_interval seconds):
a camera is offline when its camera_fps stays at 0 for camera_offline_seconds.
"""
import logging
import time
import uuid
from typing import Awaitable, Callable

from .config import Rules
from .frigate_api import FrigateAPI

log = logging.getLogger("smarttech.watchdog")

STATS_INTERVAL = 15   # must match mqtt.stats_interval in Frigate's config.yml
FRIGATE_GRACE = 30    # seconds Frigate may be away (e.g. a normal restart) before we alert


class CameraWatchdog:
    def __init__(self, rules: Rules, api: FrigateAPI, fmt_time: Callable[[float], str],
                 emit: Callable[[dict, tuple | None], Awaitable[None]],
                 publish_status: Callable[[str, bool], Awaitable[None]],
                 publish_mode: Callable[[bool], Awaitable[None]]):
        self.rules = rules
        self.api = api
        self.fmt_time = fmt_time
        self.emit = emit
        self.publish_status = publish_status
        self.publish_mode = publish_mode
        self.backup: bool | None = None       # True = outside cameras not watching
        self.started = time.time()
        self.cameras: dict[str, dict] = {}     # cam -> {"fps", "last_ok", "online"}
        self.last_stats: float | None = None
        self.frigate_available: bool | None = None
        self.frigate_up: bool | None = None
        self.frigate_down_since: float | None = None
        self.frigate_alerted = False

    def on_stats(self, stats: dict) -> None:
        now = time.time()
        self.last_stats = now
        for cam, s in (stats.get("cameras") or {}).items():
            state = self.cameras.setdefault(cam, {"fps": 0.0, "last_ok": now, "online": None})
            state["fps"] = float(s.get("camera_fps") or 0)
            if state["fps"] > 0.5:
                state["last_ok"] = now

    def on_available(self, payload: str) -> None:
        self.frigate_available = payload.strip() == "online"
        if self.frigate_available:
            self.last_stats = time.time()   # give Frigate time to publish fresh stats

    def outside_down(self) -> bool:
        """Backup mode: no outside camera set up, or any outside camera not delivering video."""
        outside = self.rules.outside_cameras()
        if not outside or not self.frigate_up:
            return True
        return any(self.cameras.get(cam, {}).get("online") is not True for cam in outside)

    def camera_state(self, cam: str) -> dict:
        state = self.cameras.get(cam, {})
        return {"online": state.get("online"), "fps": round(state.get("fps", 0.0), 1)}

    async def tick(self) -> None:
        now = time.time()
        limit = self.rules["camera_offline_seconds"]
        stale_after = max(limit, 3 * STATS_INTERVAL)
        if self.last_stats is None:
            stats_stale = now - self.started > stale_after
        else:
            stats_stale = now - self.last_stats > stale_after
        frigate_up = self.frigate_available is not False and not stats_stale

        # Frigate: alert only if it stays away longer than a normal restart.
        if frigate_up:
            self.frigate_down_since = None
            if self.frigate_alerted:
                self.frigate_alerted = False
                await self._system_event(True, now)
        else:
            self.frigate_down_since = self.frigate_down_since or now
            if not self.frigate_alerted and now - self.frigate_down_since >= FRIGATE_GRACE:
                self.frigate_alerted = True
                await self._system_event(False, now)
        self.frigate_up = frigate_up

        for cam, state in self.cameras.items():
            if not self.api.cameras.get(cam, {}).get("enabled", True):
                continue
            online = frigate_up and now - state["last_ok"] < limit
            if online == state["online"]:
                continue
            state["online"] = online
            await self.publish_status(cam, online)
            if not online and frigate_up:
                # Only the camera is down (while Frigate is down we already sent one alert for everything).
                state["alerted"] = True
                await self._camera_event(cam, False, limit, now)
            elif online and state.get("alerted"):
                state["alerted"] = False
                await self._camera_event(cam, True, limit, now)

        backup = self.outside_down()
        if backup != self.backup:
            # Stay quiet during the first minute after start-up while cameras report in.
            if self.backup is not None and now - self.started > 60:
                await self._mode_event(backup, now)
            self.backup = backup
            await self.publish_mode(backup)

    async def _mode_event(self, backup: bool, now: float) -> None:
        if backup:
            message = (f"Backup mode ON - outside camera not available, inside cameras now alert "
                       f"on any unknown person - {self.fmt_time(now)}")
        else:
            message = f"Backup mode OFF - outside cameras are watching again - {self.fmt_time(now)}"
        log.warning(message)
        await self.emit(self._event("system", "MEDIUM" if backup else "INFO", "Protection mode",
                                    "system", "Smart Tech", message, now), None)

    async def _camera_event(self, cam: str, online: bool, limit: float, now: float) -> None:
        name = self.api.camera_name(cam)
        if online:
            message = f"{name} is back online - {self.fmt_time(now)}"
        else:
            message = f"{name} is OFFLINE - no video for {int(limit)} seconds - {self.fmt_time(now)}"
        log.warning(message)
        await self.emit(self._event("camera", "HIGH" if not online else "INFO",
                                    "Camera offline" if not online else "Camera online",
                                    cam, name, message, now), None)

    async def _system_event(self, up: bool, now: float) -> None:
        if up:
            message = f"Frigate is running again - {self.fmt_time(now)}"
        else:
            message = f"Frigate is not responding - cameras are not being watched - {self.fmt_time(now)}"
        log.warning(message)
        await self.emit(self._event("system", "INFO" if up else "HIGH", "Frigate status",
                                    "system", "Smart Tech", message, now), None)

    @staticmethod
    def _event(kind, level, rule, cam, cam_name, message, now) -> dict:
        return {"id": uuid.uuid4().hex, "ts": now, "kind": kind, "level": level, "rule": rule,
                "camera": cam, "camera_name": cam_name, "message": message}
