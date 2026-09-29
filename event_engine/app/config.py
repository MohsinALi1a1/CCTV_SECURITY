"""Settings from environment variables, and alert rules from rules.yml (reloaded automatically)."""
import json
import logging
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

log = logging.getLogger("smarttech.config")

LEVELS = ("HIGH", "MEDIUM", "LOW", "INFO")
AREAS = ("inside", "outside")
ZONE_KINDS = ("no_entry", "restricted")

DEFAULTS = {
    "cameras": {},
    "restricted_zones": {},
    "face_wait_seconds": 3,
    "known_min_score": 0.9,
    "cooldown_seconds": 60,
    "retention_days": 7,
    "camera_offline_seconds": 45,
    "notify_levels": ["HIGH", "LOW"],
    "rules": [],
}


class Settings:
    def __init__(self):
        self.mqtt_host = os.getenv("MQTT_HOST", "mosquitto")
        self.mqtt_port = int(os.getenv("MQTT_PORT", "1883"))
        self.mqtt_user = os.getenv("MQTT_USER") or None
        self.mqtt_password = os.getenv("MQTT_PASSWORD") or None
        self.frigate_url = os.getenv("FRIGATE_URL", "http://frigate:5000").rstrip("/")
        self.frigate_ui_url = os.getenv("FRIGATE_UI_URL", "https://localhost:8971")
        self.rules_file = Path(os.getenv("RULES_FILE", "/config/rules.yml"))
        self.data_dir = Path(os.getenv("DATA_DIR", "/data"))
        self.web_port = int(os.getenv("WEB_PORT", "8080"))
        # Firebase (mobile app). Off automatically when the credentials file is missing.
        self.firebase_enabled = os.getenv("FIREBASE_ENABLED", "true").lower() in ("1", "true", "yes")
        self.firebase_credentials = Path(os.getenv("FIREBASE_CREDENTIALS", "/secrets/firebase-service-account.json"))
        self.firebase_bucket = os.getenv("FIREBASE_STORAGE_BUCKET") or None
        self.camera_secrets_dir = Path(os.getenv("CAMERA_SECRETS_DIR", "/camera-secrets"))
        self.lan_subnet = os.getenv("LAN_SUBNET") or None
        self.home_id = os.getenv("HOME_ID", "home")
        self.home_name = os.getenv("HOME_NAME", "My Home")
        try:
            self.timezone = ZoneInfo(os.getenv("TZ") or "UTC")
        except Exception:
            self.timezone = ZoneInfo("UTC")


class Rules:
    """Holds the current rules and reloads them whenever rules.yml is saved."""

    def __init__(self, path: Path, areas: "AreaStore | None" = None, cam_store: "CameraStore | None" = None):
        self.path = path
        self.areas = areas
        self.cam_store = cam_store
        self._mtime = None
        self.data = self._normalise({})
        self.reload_if_changed()

    def reload_if_changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime is not False:
                log.error("Rules file %s not found - using built-in defaults", self.path)
                self._mtime = False
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            self.data = self._normalise(raw)
        except Exception as exc:  # keep running with the last good rules
            log.error("rules.yml has a mistake, keeping the previous rules: %s", exc)
            return False
        log.info("Loaded %d alert rules from %s", len(self.data["rules"]), self.path)
        return True

    @staticmethod
    def _normalise(raw) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("rules.yml must contain 'key: value' settings")
        data = {**DEFAULTS, **raw}

        cameras = {}
        for cam, cfg in (data.get("cameras") or {}).items():
            cfg = cfg or {}
            area = str(cfg.get("area", "inside")).lower()
            if area not in AREAS:
                raise ValueError(f"camera {cam}: 'area' must be inside or outside")
            cameras[str(cam)] = {"area": area,
                                 "restricted_zones": [str(z) for z in (cfg.get("restricted_zones") or [])],
                                 "no_entry_zones": [str(z) for z in (cfg.get("no_entry_zones") or [])]}
        # older format: restricted_zones: {camera: [zones]}
        for cam, zl in (data.get("restricted_zones") or {}).items():
            entry = cameras.setdefault(str(cam), {"area": "inside", "restricted_zones": [], "no_entry_zones": []})
            entry["restricted_zones"] += [str(z) for z in (zl or []) if str(z) not in entry["restricted_zones"]]
        data["cameras"] = cameras

        rules = []
        for i, rule in enumerate(data.get("rules") or [], 1):
            obj = str(rule.get("object", "person")).lower()
            person = str(rule.get("person", "any")).lower()
            where = str(rule.get("where", "restricted")).lower()
            area = str(rule.get("area", "any")).lower()
            when = str(rule.get("when", "always")).lower()
            level = str(rule.get("level", "HIGH")).upper()
            if person not in ("known", "unknown", "any"):
                raise ValueError(f"rule {i}: 'person' must be known, unknown or any")
            if where not in ZONE_KINDS + ("any",):
                raise ValueError(f"rule {i}: 'where' must be no_entry, restricted or any")
            if area not in AREAS + ("any",):
                raise ValueError(f"rule {i}: 'area' must be inside, outside or any")
            if when not in ("always", "outside_down"):
                raise ValueError(f"rule {i}: 'when' must be always or outside_down")
            if level not in LEVELS:
                raise ValueError(f"rule {i}: 'level' must be one of {', '.join(LEVELS)}")
            rules.append({"name": rule.get("name") or f"Rule {i}", "object": obj, "person": person,
                          "where": where, "area": area, "when": when, "level": level,
                          "hours": _parse_hours(rule.get("hours"), i)})
        data["rules"] = rules

        data["notify_levels"] = [str(lv).upper() for lv in (data.get("notify_levels") or [])]
        for key in ("face_wait_seconds", "known_min_score", "cooldown_seconds", "camera_offline_seconds"):
            data[key] = float(data[key])
        data["retention_days"] = int(data["retention_days"])
        return data

    def __getitem__(self, key):
        return self.data[key]

    def zone_kinds(self, camera: str) -> dict:
        """zone -> "no_entry" | "restricted" for every watched zone of a camera.
        Areas drawn in the dashboard (areas.json) win over rules.yml."""
        cfg = self.data["cameras"].get(camera, {})
        kinds = {z: "restricted" for z in cfg.get("restricted_zones", [])}
        kinds.update({z: "no_entry" for z in cfg.get("no_entry_zones", [])})
        if self.areas:
            kinds.update(self.areas.kinds(camera))
        return kinds

    def zone_kind(self, camera: str, zone: str) -> str | None:
        return self.zone_kinds(camera).get(zone)

    def restricted(self, camera: str) -> list:
        """All watched zones (restricted + no-entry) of a camera."""
        return list(self.zone_kinds(camera))

    def area(self, camera: str) -> str:
        """inside or outside: rules.yml first, then cameras added in the dashboard, else inside."""
        if camera in self.data["cameras"]:
            return self.data["cameras"][camera]["area"]
        if self.cam_store and camera in self.cam_store.data:
            return self.cam_store.data[camera].get("area", "inside")
        return "inside"

    def outside_cameras(self) -> list:
        cams = set(self.data["cameras"]) | set(self.cam_store.data if self.cam_store else {})
        return sorted(cam for cam in cams if self.area(cam) == "outside")


class CameraStore:
    """Cameras added from the dashboard: inside/outside, IP, name (saved in /data/cameras.json)."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.error("Could not read %s: %s", path, exc)

    def set(self, camera: str, info: dict) -> None:
        self.data[camera] = {**self.data.get(camera, {}), **info}
        self._save()

    def remove(self, camera: str) -> None:
        self.data.pop(camera, None)
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class AreaStore:
    """Areas drawn in the dashboard: which kind each Frigate zone is (saved in /data/areas.json).
    The zone shapes themselves live in Frigate's config.yml."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, str]] = {}      # camera -> {zone: kind}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.data = {cam: {z: k for z, k in zones.items() if k in ZONE_KINDS} for cam, zones in raw.items()}
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.error("Could not read %s: %s", path, exc)

    def kinds(self, camera: str) -> dict:
        return dict(self.data.get(camera, {}))

    def set(self, camera: str, zone: str, kind: str) -> None:
        self.data.setdefault(camera, {})[zone] = kind
        self._save()

    def remove(self, camera: str, zone: str) -> None:
        self.data.get(camera, {}).pop(zone, None)
        self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _parse_hours(value, i: int):
    """'23:00-06:00' -> (1380, 360) minutes after midnight; None = all day."""
    if not value:
        return None
    try:
        start, end = (part.strip() for part in str(value).split("-"))
        to_min = lambda t: int(t.split(":")[0]) * 60 + int(t.split(":")[1])
        start_m, end_m = to_min(start), to_min(end)
        if not (0 <= start_m < 1440 and 0 <= end_m <= 1440):
            raise ValueError
        return start_m, end_m
    except ValueError:
        raise ValueError(f"rule {i}: 'hours' must look like \"23:00-06:00\"") from None


def in_hours(hours, minute_of_day: int) -> bool:
    if hours is None:
        return True
    start, end = hours
    if start <= end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end     # crosses midnight
