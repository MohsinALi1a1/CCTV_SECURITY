"""Small client for Frigate's internal API (port 5000, only reachable inside the Docker network)."""
import asyncio
import logging
import time

import aiohttp

log = logging.getLogger("smarttech.frigate")


def pretty(key: str) -> str:
    return key.replace("_", " ").title()


def _points(coordinates, width: int, height: int) -> list:
    """Frigate zone coordinates ("x,y,x,y" or list) -> [[x, y], ...] relative 0..1."""
    if isinstance(coordinates, (list, tuple)):
        coordinates = ",".join(str(c) for c in coordinates)
    try:
        values = [float(v) for v in str(coordinates or "").split(",") if v.strip()]
    except ValueError:
        return []
    pts = [values[i:i + 2] for i in range(0, len(values) - 1, 2)]
    if any(x > 1 or y > 1 for x, y in pts):          # old configs use pixels
        pts = [[x / width, y / height] for x, y in pts]
    return pts


class FrigateAPI:
    def __init__(self, base_url: str):
        self.base = base_url
        self.session: aiohttp.ClientSession | None = None
        # camera id -> {"name": friendly name, "zones": {zone id: friendly name}, "enabled": bool}
        self.cameras: dict[str, dict] = {}

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def close(self):
        if self.session:
            await self.session.close()

    async def refresh_config(self) -> bool:
        """Reads camera and zone friendly names from Frigate's config."""
        try:
            async with self.session.get(f"{self.base}/api/config") as resp:
                resp.raise_for_status()
                config = await resp.json()
        except Exception as exc:
            log.warning("Could not read Frigate config yet (%s) - will retry", exc)
            return False
        cameras = {}
        for cam, cfg in (config.get("cameras") or {}).items():
            detect = cfg.get("detect") or {}
            width, height = detect.get("width") or 1, detect.get("height") or 1
            zones, shapes = {}, {}
            for z, zc in (cfg.get("zones") or {}).items():
                zc = zc or {}
                zones[z] = zc.get("friendly_name") or pretty(z)
                shapes[z] = _points(zc.get("coordinates"), width, height)
            review = cfg.get("review") or {}
            required = {kind: list((review.get(kind) or {}).get("required_zones") or [])
                        for kind in ("alerts", "detections")}
            cameras[cam] = {"name": cfg.get("friendly_name") or pretty(cam), "zones": zones,
                            "shapes": shapes, "enabled": cfg.get("enabled", True), "required": required}
        self.cameras = cameras
        return True

    async def get_json(self, path: str, timeout: float = 15, **params):
        try:
            async with self.session.get(f"{self.base}{path}", params=params,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return await resp.json(content_type=None)
        except Exception as exc:
            log.debug("GET %s failed: %s", path, exc)
            return None

    async def get_bytes_q(self, path: str, timeout: float = 15, **params) -> bytes | None:
        try:
            async with self.session.get(f"{self.base}{path}", params=params,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200 and resp.content_type.startswith("image/"):
                    return await resp.read()
        except Exception as exc:
            log.debug("GET %s failed: %s", path, exc)
        return None

    async def config_set(self, config_data: dict, restart: bool) -> tuple[bool, str]:
        """Changes Frigate's config.yml (Frigate validates it and rolls back on errors)."""
        body = {"requires_restart": 1 if restart else 0, "config_data": config_data}
        try:
            async with self.session.put(f"{self.base}/api/config/set", json=body,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:
                data = await resp.json(content_type=None)
                return resp.status == 200 and data.get("success", False), data.get("message", "")
        except Exception as exc:
            return False, str(exc)

    async def restart_and_wait(self, timeout: float = 180) -> None:
        """Restarts Frigate and waits until its API answers again."""
        try:
            async with self.session.post(f"{self.base}/api/restart", timeout=aiohttp.ClientTimeout(total=15)):
                pass
        except Exception:
            pass                               # the connection often drops while it restarts
        await asyncio.sleep(8)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            data = await self.get_json("/api/stats", timeout=5)
            if isinstance(data, dict) and "cameras" in data:
                return
            await asyncio.sleep(3)
        raise TimeoutError("Frigate did not come back after the restart")

    async def _config_set(self, camera: str, zones: dict, extra: dict | None = None) -> tuple[bool, str]:
        body = {"requires_restart": 0, "update_topic": f"config/cameras/{camera}/zones",
                "config_data": {"cameras": {camera: {"zones": zones, **(extra or {})}}}}
        try:
            async with self.session.put(f"{self.base}/api/config/set", json=body) as resp:
                data = await resp.json(content_type=None)
                ok = resp.status == 200 and data.get("success", False)
                return ok, data.get("message", "")
        except Exception as exc:
            return False, str(exc)

    async def save_zone(self, camera: str, zone: str, name: str, points: list) -> tuple[bool, str]:
        """Creates or replaces a zone live (no Frigate restart). points are 0..1 relative [x, y]."""
        coords = ",".join(f"{v:.3f}" for p in points for v in p)
        ok, msg = await self._config_set(camera, {zone: {
            "coordinates": coords, "friendly_name": name, "objects": ["person"],
            "inertia": 1,               # react on the first frame inside
            "loitering_time": 0}})
        if ok:   # Frigate's /api/config catches up a moment later, so update our copy now
            cam = self.cameras.get(camera)
            if cam:
                cam["zones"][zone] = name
                cam["shapes"][zone] = [list(p) for p in points]
        return ok, msg

    async def delete_zone(self, camera: str, zone: str) -> tuple[bool, str]:
        # A zone used by Frigate's review settings must be removed there too, or Frigate refuses.
        required = self.cameras.get(camera, {}).get("required", {})
        review = {kind: {"required_zones": [z for z in zl if z != zone] or None}
                  for kind, zl in required.items() if zone in zl}
        ok, msg = await self._config_set(camera, {zone: None}, {"review": review} if review else None)
        if ok:
            cam = self.cameras.get(camera)
            if cam:
                cam["zones"].pop(zone, None)
                cam["shapes"].pop(zone, None)
                for zl in cam.get("required", {}).values():
                    if zone in zl:
                        zl.remove(zone)
        return ok, msg

    def camera_name(self, cam: str) -> str:
        return self.cameras.get(cam, {}).get("name") or pretty(cam)

    def zone_name(self, cam: str, zone: str) -> str:
        return self.cameras.get(cam, {}).get("zones", {}).get(zone) or pretty(zone)

    async def get_bytes(self, path: str) -> bytes | None:
        try:
            async with self.session.get(f"{self.base}{path}") as resp:
                if resp.status == 200:
                    return await resp.read()
        except Exception as exc:
            log.debug("GET %s failed: %s", path, exc)
        return None

    async def snapshot(self, frigate_id: str, camera: str) -> bytes | None:
        """Best snapshot of a tracked object so far; falls back to the camera's latest frame."""
        data = await self.get_bytes(f"/api/events/{frigate_id}/snapshot.jpg?bbox=1&timestamp=1")
        return data or await self.get_bytes(f"/api/{camera}/latest.jpg?bbox=1&height=720")

    async def latest(self, camera: str, height: int = 360) -> bytes | None:
        return await self.get_bytes(f"/api/{camera}/latest.jpg?height={height}")
