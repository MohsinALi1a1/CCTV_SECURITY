"""Combines Frigate's zone + face results into clear Smart Tech events.

For every tracked object Frigate reports, we decide once per "location":
  - "*"        = the camera as a whole (rules with where: any)
  - zone name  = each watched zone it enters (rules with where: restricted / no_entry)

No-entry zones are decided immediately (nobody is allowed there, so the face doesn't matter).

A person is only called "Unknown" after waiting face_wait_seconds for face
recognition, so family members don't trigger HIGH alerts just because their
face wasn't matched in the first frame. If a face is recognised after we
already raised an "Unknown" alert, that alert is updated with the name.
"""
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import Rules, in_hours
from .frigate_api import FrigateAPI

log = logging.getLogger("smarttech.engine")

ANYWHERE = "*"
FORGET_ENDED_AFTER = 30      # seconds to keep an ended object (late face results)
FORGET_STALE_AFTER = 600     # seconds without any update


@dataclass
class Decision:
    event: dict | None       # the event we raised, or None (no rule matched / cooldown)
    known: bool              # identity at the time of the decision


@dataclass
class Tracked:
    id: str
    camera: str
    label: str
    first_seen: float
    last_update: float
    name: str | None = None
    score: float = 0.0
    zones: dict = field(default_factory=dict)       # restricted zone -> time first seen inside
    decided: dict = field(default_factory=dict)     # location -> Decision
    ended: bool = False


def parse_sub_label(sub_label) -> tuple[str | None, float]:
    """Frigate sends sub_label as ["Name", score], "Name" or null."""
    if isinstance(sub_label, (list, tuple)) and sub_label:
        return (str(sub_label[0]) if sub_label[0] else None,
                float(sub_label[1]) if len(sub_label) > 1 and sub_label[1] is not None else 1.0)
    if isinstance(sub_label, str) and sub_label:
        return sub_label, 1.0
    return None, 0.0


class AlertEngine:
    def __init__(self, rules: Rules, api: FrigateAPI, fmt_time: Callable[[float], str],
                 emit: Callable[[dict, tuple], Awaitable[None]],
                 emit_update: Callable[[dict], Awaitable[None]],
                 outside_down: Callable[[], bool], minute_of_day: Callable[[float], int]):
        self.rules = rules
        self.api = api
        self.fmt_time = fmt_time
        self.outside_down = outside_down        # True = backup mode (outside cameras not watching)
        self.minute_of_day = minute_of_day
        self.emit = emit
        self.emit_update = emit_update
        self.objects: dict[str, Tracked] = {}
        self.last_fired: dict[tuple, float] = {}

    # ---------- input from MQTT ----------

    def on_frigate_event(self, msg: dict) -> None:
        after = msg.get("after") or {}
        oid = after.get("id")
        if not oid or after.get("false_positive"):
            return
        now = time.time()
        obj = self.objects.get(oid)
        if obj is None:
            obj = Tracked(id=oid, camera=after.get("camera", "?"), label=after.get("label", "object"),
                          first_seen=now, last_update=now)
            self.objects[oid] = obj
            log.debug("New %s on %s (%s)", obj.label, obj.camera, oid)
        obj.last_update = now

        name, score = parse_sub_label(after.get("sub_label"))
        if name:
            self._set_name(obj, name, score)

        watched = self.rules.zone_kinds(obj.camera)
        seen = []
        for key in ("current_zones", "entered_zones"):
            value = after.get(key) or []
            seen += [value] if isinstance(value, str) else list(value)
        for zone in seen:
            if zone in watched and zone not in obj.zones:
                obj.zones[zone] = now
                log.info("%s entered %s zone %s on %s", obj.label, watched[zone], zone, obj.camera)

        if msg.get("type") == "end" or after.get("end_time"):
            obj.ended = True

    def on_face_update(self, msg: dict) -> None:
        obj = self.objects.get(msg.get("id"))
        name, score = msg.get("name"), float(msg.get("score") or 0)
        if obj and name and score >= self.rules["known_min_score"]:
            self._set_name(obj, name, score)

    def _set_name(self, obj: Tracked, name: str, score: float) -> None:
        if obj.name is None:
            log.info("Face recognised on %s: %s (%.2f)", obj.camera, name, score)
        obj.name = name
        obj.score = max(obj.score, score)

    # ---------- decisions (called every 0.5 s) ----------

    async def tick(self) -> None:
        now = time.time()
        wait = self.rules["face_wait_seconds"]
        for obj in list(self.objects.values()):
            locations = [(ANYWHERE, obj.first_seen)] + list(obj.zones.items())
            for loc, since in locations:
                decision = obj.decided.get(loc)
                if decision is None:
                    # No-entry areas apply to everyone, so there is no need to wait for the face.
                    ready = (obj.name is not None or obj.label != "person" or obj.ended
                             or now - since >= wait
                             or (loc != ANYWHERE and self.rules.zone_kind(obj.camera, loc) == "no_entry"))
                    if ready:
                        obj.decided[loc] = await self._decide(obj, loc, now)
                elif not decision.known and obj.name is not None:
                    obj.decided[loc] = await self._identified_later(obj, loc, decision, now)

            idle = now - obj.last_update
            if (obj.ended and idle > FORGET_ENDED_AFTER) or idle > FORGET_STALE_AFTER:
                del self.objects[obj.id]

        cooldown = self.rules["cooldown_seconds"]
        self.last_fired = {k: t for k, t in self.last_fired.items() if now - t < cooldown}

    def _is_known(self, obj: Tracked) -> bool:
        return obj.name is not None or obj.label != "person"

    def _match(self, obj: Tracked, loc: str, now: float) -> dict | None:
        person = ("known" if obj.name else "unknown") if obj.label == "person" else None
        area = self.rules.area(obj.camera)
        where = "any" if loc == ANYWHERE else self.rules.zone_kind(obj.camera, loc)
        backup = None     # only work it out if a rule needs it
        for rule in self.rules["rules"]:
            if rule["object"] != obj.label:
                continue
            if rule["where"] != where:
                continue
            if rule["person"] != "any" and rule["person"] != person:
                continue
            if rule["area"] != "any" and rule["area"] != area:
                continue
            if not in_hours(rule["hours"], self.minute_of_day(now)):
                continue
            if rule["when"] == "outside_down":
                if backup is None:
                    backup = self.outside_down()
                if not backup:
                    continue
            return rule
        return None

    async def _decide(self, obj: Tracked, loc: str, now: float) -> Decision:
        known = self._is_known(obj)
        rule = self._match(obj, loc, now)
        if rule is None:
            return Decision(None, known)
        key = (obj.camera, loc, obj.name or "unknown", rule["name"])
        if now - self.last_fired.get(key, 0) < self.rules["cooldown_seconds"]:
            log.info("Suppressed (cooldown): %s on %s", rule["name"], obj.camera)
            return Decision(None, known)
        self.last_fired[key] = now

        event = self._build(obj, loc, rule, now)
        await self.emit(event, (obj.id, obj.camera))
        return Decision(event, known)

    async def _identified_later(self, obj: Tracked, loc: str, decision: Decision, now: float) -> Decision:
        if decision.event is None:
            # e.g. "Known person seen" did not match while they were still unknown
            return await self._decide(obj, loc, now)
        rule = self._match(obj, loc, now)
        event = dict(decision.event)
        event.update(
            person=obj.name, known=1, score=round(obj.score, 2),
            level=rule["level"] if rule else "INFO",
            rule=rule["name"] if rule else "Identified",
            message=self._message(obj, loc, event["ts"]) + " (identified after alert)",
        )
        await self.emit_update(event)
        return Decision(event, True)

    # ---------- event text ----------

    def _build(self, obj: Tracked, loc: str, rule: dict, now: float) -> dict:
        zone = None if loc == ANYWHERE else loc
        return {
            "id": uuid.uuid4().hex,
            "ts": now,
            "kind": "detection",
            "level": rule["level"],
            "rule": rule["name"],
            "camera": obj.camera,
            "camera_name": self.api.camera_name(obj.camera),
            "zone": zone,
            "zone_name": self.api.zone_name(obj.camera, zone) if zone else None,
            "label": obj.label,
            "person": (obj.name or "Unknown") if obj.label == "person" else None,
            "known": int(obj.name is not None) if obj.label == "person" else None,
            "score": round(obj.score, 2) if obj.name else None,
            "message": self._message(obj, loc, now),
            "snapshot": None,
            "frigate_id": obj.id,
            "area": self.rules.area(obj.camera),
            "zone_kind": self.rules.zone_kind(obj.camera, zone) if zone else None,
        }

    def _message(self, obj: Tracked, loc: str, ts: float) -> str:
        camera = self.api.camera_name(obj.camera)
        if obj.label != "person":
            who = obj.label.capitalize()
        else:
            who = f"Known person ({obj.name})" if obj.name else "Unknown person"
        if loc == ANYWHERE:
            place = "inside the house" if self.rules.area(obj.camera) == "inside" else "outside"
            return f"{who} {place} - {camera} - {self.fmt_time(ts)}"
        zone = self.api.zone_name(obj.camera, loc)
        if self.rules.zone_kind(obj.camera, loc) == "no_entry":
            return f"{who} at NO-ENTRY area {zone} - {camera} - {self.fmt_time(ts)}"
        verb = "in" if obj.label == "person" and obj.name else "entered"
        return f"{who} {verb} {zone} - {camera} - {self.fmt_time(ts)}"
