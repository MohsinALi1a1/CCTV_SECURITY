"""Add IP cameras from the dashboard - no config files for the customer.

  1. scan()   find cameras on the local network (RTSP / ONVIF ports + ONVIF WS-Discovery)
  2. probe()  log in with the camera's user/password, find its main + sub stream
              (ONVIF, else common brand URL patterns; tested with ffprobe in this container)
  3. add()    save the login as Frigate secret files (never in config.yml), add the
              camera to Frigate, restart Frigate, wait until video arrives

Passwords stay on the server: the browser only gets a session id and URLs with
the password hidden.
"""
import asyncio
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .onvif import OnvifCamera

log = logging.getLogger("smarttech.setup")

RTSP_PORT = 554
ONVIF_PORTS = (80, 8000, 2020, 8080, 8899)
SESSION_TTL = 20 * 60

# (label, main path, sub path) - tried when ONVIF does not give us the streams
BRAND_PATTERNS = [
    ("Hikvision / Annke / Hiwatch", "/Streaming/Channels/101", "/Streaming/Channels/102"),
    ("Dahua / Imou / Amcrest", "/cam/realmonitor?channel=1&subtype=0", "/cam/realmonitor?channel=1&subtype=1"),
    ("Reolink", "/h264Preview_01_main", "/h264Preview_01_sub"),
    ("TP-Link Tapo / Vigi", "/stream1", "/stream2"),
    ("EZVIZ", "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream"),
    ("Uniview", "/unicast/c1/s0/live", "/unicast/c1/s1/live"),
    ("Generic ONVIF", "/live/ch00_0", "/live/ch00_1"),
    ("Generic", "/11", "/12"),
]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:24] or "camera"


class CameraSetup:
    def __init__(self, app, secrets_dir: Path, default_subnet: str | None):
        self.app = app                      # the engine App (api, rules, cam_store, hub)
        self.secrets_dir = secrets_dir      # mounted in Frigate as /run/secrets
        self.default_subnet = default_subnet
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}

    # ---------- 1. scan ----------

    def suggest_subnet(self) -> str:
        return self.default_subnet or "192.168.1.0/24"

    async def scan(self, subnet: str) -> list[dict]:
        net = ipaddress.ip_network(subnet.strip(), strict=False)
        if not net.is_private:
            raise ValueError("Only your own (private) network can be scanned, e.g. 192.168.1.0/24")
        if net.num_addresses > 1024:
            raise ValueError("Network too big - use a /24 like 192.168.1.0/24")
        hosts = [str(h) for h in net.hosts()]
        sem = asyncio.Semaphore(400)

        async def check(ip: str, port: int):
            async with sem:
                try:
                    _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.8)
                    w.close()
                    return ip, port
                except Exception:
                    return None

        results = await asyncio.gather(*(check(ip, p) for ip in hosts for p in (RTSP_PORT,) + ONVIF_PORTS))
        wsd = await asyncio.to_thread(_ws_discovery, 2.0)
        found: dict[str, dict] = {}
        for r in results:
            if r:
                found.setdefault(r[0], {"ip": r[0], "ports": []})["ports"].append(r[1])
        for ip, xaddr in wsd.items():
            if ip in found or ipaddress.ip_address(ip) in net:
                found.setdefault(ip, {"ip": ip, "ports": []})["onvif_discovered"] = True
        known = self._known_hosts()
        devices = []
        for d in found.values():
            d["ports"].sort()
            d["rtsp"] = RTSP_PORT in d["ports"]
            d["onvif_discovered"] = d.get("onvif_discovered", False)
            d["likely_camera"] = d["rtsp"] or d["onvif_discovered"]
            d["already_added"] = d["ip"] in known
            if d["likely_camera"] or d["already_added"]:
                devices.append(d)
        devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
        log.info("Scan %s: %d possible cameras", subnet, len(devices))
        return devices

    def _known_hosts(self) -> set:
        return {c.get("host") for c in self.app.cam_store.data.values() if c.get("host")}

    # ---------- 2. probe ----------

    async def probe(self, ip: str, username: str, password: str, rtsp_port: int = RTSP_PORT) -> dict:
        ipaddress.ip_address(ip)                                  # validates
        manufacturer = model = None
        candidates: list[tuple[str, str]] = []           # (url with login for testing, url without login)
        # ffmpeg decodes %-escapes in the login, so any character in the password is safe
        login = f"{quote(username, safe='')}:{quote(password, safe='')}@" if username else ""

        def pair(url_without_login: str) -> tuple[str, str]:
            return url_without_login.replace("rtsp://", f"rtsp://{login}", 1), url_without_login

        # a) ONVIF: ask the camera itself for its stream addresses (done here, so the
        #    password never appears in another service's request log)
        for port in ONVIF_PORTS:
            if not await _port_open(ip, port):
                continue
            cam = OnvifCamera(self.app.api.session, ip, port, username, password)
            try:
                await cam.sync_clock()
                try:
                    manufacturer, model = await cam.device_info()
                except Exception as exc:
                    log.debug("ONVIF device info on %s:%s failed: %s", ip, port, exc)
                for uri in await cam.stream_uris():
                    clean = re.sub(r"^rtsp://[^/@]*@", "rtsp://", uri)   # drop any login the camera added
                    candidates.append(pair(clean))
            except PermissionError:
                raise ValueError("The camera refused the username or password (ONVIF).") from None
            except Exception as exc:
                log.debug("ONVIF on %s:%s failed: %s", ip, port, exc)
            if candidates:
                break

        streams = await self._measure(candidates) if candidates else []

        # b) brand patterns
        brand = None
        if not streams:
            port = "" if rtsp_port == RTSP_PORT else f":{rtsp_port}"
            for label, main, sub in BRAND_PATTERNS:
                found = await self._measure([pair(f"rtsp://{ip}{port}{main}"), pair(f"rtsp://{ip}{port}{sub}")])
                if found:
                    streams, brand = found, label
                    break

        if not streams:
            reachable = await _port_open(ip, rtsp_port)
            raise ValueError("Could not open the camera video. " + (
                "Check the username and password, and that RTSP is switched on in the camera's app."
                if reachable else f"Nothing answers on {ip}:{rtsp_port} - check the IP address."))

        streams.sort(key=lambda s: s["width"] * s["height"], reverse=True)
        main = streams[0]
        sub = streams[-1] if len(streams) > 1 else streams[0]
        sid = secrets.token_urlsafe(16)
        self._gc_sessions()
        self.sessions[sid] = {"ip": ip, "username": username, "password": password, "main": main, "sub": sub,
                              "manufacturer": manufacturer, "model": model, "created": time.time()}
        return {"session": sid, "ip": ip, "manufacturer": manufacturer or brand or "Unknown", "model": model or "",
                "main": _public(main), "sub": _public(sub), "same_stream": main is sub}

    async def _measure(self, urls: list[tuple[str, str]]) -> list[dict]:
        """ffprobe each URL (locally); keep the ones that work, with their size."""
        out, seen = [], set()
        for url, clean in urls:
            if clean in seen:
                continue
            seen.add(clean)
            video = await _ffprobe(url)
            if video:
                out.append({"url": url, "clean": clean, "width": int(video["width"]),
                            "height": int(video["height"]), "codec": video.get("codec_name")})
        return out

    async def preview(self, sid: str) -> bytes | None:
        s = self._session(sid)
        return await _snapshot(s["sub"]["url"])

    def _session(self, sid: str) -> dict:
        s = self.sessions.get(sid)
        if not s or time.time() - s["created"] > SESSION_TTL:
            raise ValueError("This setup expired - please connect to the camera again")
        return s

    def _gc_sessions(self):
        now = time.time()
        for k in [k for k, v in self.sessions.items() if now - v["created"] > SESSION_TTL]:
            del self.sessions[k]

    # ---------- 3. add ----------

    def start_add(self, sid: str, name: str, area: str, face: bool) -> str:
        s = self._session(sid)
        name = (name or "").strip()[:40]
        if not name:
            raise ValueError("Please give the camera a name")
        if area not in ("inside", "outside"):
            raise ValueError("Choose inside or outside")
        existing = set(self.app.api.cameras) | set(self.app.cam_store.data)
        n = len(existing) + 1
        cam_id = f"cam{n:02d}_{_slug(name)}"
        while cam_id in existing:
            n += 1
            cam_id = f"cam{n:02d}_{_slug(name)}"
        job = {"id": uuid.uuid4().hex, "camera": cam_id, "name": name, "step": "Saving camera login",
               "done": False, "error": None, "started": time.time()}
        self.jobs[job["id"]] = job
        asyncio.create_task(self._add(job, s, cam_id, name, area, face))
        self.sessions.pop(sid, None)
        return job["id"]

    async def _add(self, job: dict, s: dict, cam_id: str, name: str, area: str, face: bool):
        api = self.app.api
        key = re.sub(r"[^A-Z0-9]", "_", cam_id.upper())
        user_var, pass_var = f"FRIGATE_CAM_{key}_USER", f"FRIGATE_CAM_{key}_PASS"
        try:
            # 1. login -> secret files (URL-encoded: go2rtc uses the value inside the URL as is)
            self.secrets_dir.mkdir(parents=True, exist_ok=True)
            (self.secrets_dir / user_var).write_text(quote(s["username"], safe=""), encoding="utf-8")
            (self.secrets_dir / pass_var).write_text(quote(s["password"], safe=""), encoding="utf-8")

            def stream_url(stream: dict) -> str:
                # braces in the camera URL itself would clash with {FRIGATE_*} substitution
                clean = stream["clean"].replace("{", "{{").replace("}", "}}")
                return clean.replace("rtsp://", f"rtsp://{{{user_var}}}:{{{pass_var}}}@", 1)

            main, sub = s["main"], s["sub"]
            streams = {cam_id: [stream_url(main)]}
            inputs = [{"path": f"rtsp://127.0.0.1:8554/{cam_id}", "input_args": "preset-rtsp-restream",
                       "roles": ["record"] if main is not sub else ["detect", "record"]}]
            if main is not sub:
                streams[f"{cam_id}_sub"] = [stream_url(sub)]
                inputs.insert(0, {"path": f"rtsp://127.0.0.1:8554/{cam_id}_sub",
                                  "input_args": "preset-rtsp-restream", "roles": ["detect"]})
            det_w, det_h = sub["width"], sub["height"]
            if det_w > 1280:          # keep detection light on big single-stream cameras
                det_h, det_w = round(det_h * 1280 / det_w / 2) * 2, 1280
            camera = {
                "friendly_name": name,
                "ffmpeg": {"inputs": inputs},
                "detect": {"enabled": True, "width": det_w, "height": det_h, "fps": 5},
                "objects": {"track": ["person", "car"]},
                "face_recognition": {"enabled": bool(face)},
                "snapshots": {"enabled": True},
                "record": {"enabled": True},
            }
            config = {"go2rtc": {"streams": streams}, "cameras": {cam_id: camera}}

            # 2. Frigate reads secret files only at start-up
            job["step"] = "Restarting the AI so it can use the camera login (about 1 minute)"
            await api.restart_and_wait()
            job["step"] = "Adding the camera"
            ok, msg = await api.config_set(config, restart=True)
            if not ok:
                raise ValueError(f"Frigate refused the camera: {msg}")
            self.app.cam_store.set(cam_id, {"area": area, "host": s["ip"], "name": name})

            job["step"] = "Restarting the AI with the new camera (about 1 minute)"
            await api.restart_and_wait()
            await api.refresh_config()
            job["step"] = "Waiting for the first video"
            for _ in range(45):
                if self.app.watchdog.camera_state(cam_id).get("online"):
                    break
                await asyncio.sleep(2)
            job["step"] = "Done"
            job["done"] = True
            log.info("Camera added: %s (%s, %s)", cam_id, s["ip"], area)
            self.app.hub.broadcast({"type": "cameras"})
        except Exception as exc:
            log.exception("Adding camera failed")
            job["error"] = str(exc)
            job["done"] = True

    # ---------- remove ----------

    def start_remove(self, cam_id: str) -> str:
        if cam_id not in self.app.api.cameras:
            raise ValueError("Unknown camera")
        job = {"id": uuid.uuid4().hex, "camera": cam_id, "step": "Removing camera", "done": False,
               "error": None, "started": time.time()}
        self.jobs[job["id"]] = job
        asyncio.create_task(self._remove(job, cam_id))
        return job["id"]

    async def _remove(self, job: dict, cam_id: str):
        api = self.app.api
        try:
            config = {"cameras": {cam_id: None}, "go2rtc": {"streams": {cam_id: None, f"{cam_id}_sub": None}}}
            ok, msg = await api.config_set(config, restart=True)
            if not ok:
                raise ValueError(f"Frigate refused: {msg}")
            key = re.sub(r"[^A-Z0-9]", "_", cam_id.upper())
            for var in (f"FRIGATE_CAM_{key}_USER", f"FRIGATE_CAM_{key}_PASS"):
                (self.secrets_dir / var).unlink(missing_ok=True)
            self.app.cam_store.remove(cam_id)
            for zone in list(self.app.areas.kinds(cam_id)):
                self.app.areas.remove(cam_id, zone)
            job["step"] = "Restarting the AI (about 1 minute)"
            await api.restart_and_wait()
            await api.refresh_config()
            job["step"] = "Done"
            job["done"] = True
            self.app.hub.broadcast({"type": "cameras"})
            log.info("Camera removed: %s", cam_id)
        except Exception as exc:
            log.exception("Removing camera failed")
            job["error"] = str(exc)
            job["done"] = True

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError("Unknown job")
        return {k: job[k] for k in ("camera", "step", "done", "error")}


def _public(stream: dict) -> dict:
    return {"url": stream["clean"], "width": stream["width"], "height": stream["height"],
            "codec": stream.get("codec")}


async def _run(args: list[str], timeout: float) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, out
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, b""


async def _ffprobe(url: str) -> dict | None:
    code, out = await _run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                            "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
                            "-of", "json", url], timeout=15)
    if code != 0:
        return None
    try:
        streams = json.loads(out).get("streams") or []
    except ValueError:
        return None
    return next((s for s in streams if s.get("width")), None)


async def _snapshot(url: str) -> bytes | None:
    code, out = await _run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000", "-i", url,
                            "-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                           timeout=20)
    return out if code == 0 and out else None


async def _port_open(ip: str, port: int) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 1.0)
        w.close()
        return True
    except Exception:
        return False


def _ws_discovery(timeout: float) -> dict[str, str]:
    """ONVIF WS-Discovery multicast. Works when the box is on the LAN directly
    (e.g. Linux with host networking); inside Docker Desktop it usually finds nothing."""
    msg = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
    found = {}
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        sock.sendto(msg.encode(), ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            m = re.search(rb"<[^>]*XAddrs>([^<]+)<", data)
            found[addr[0]] = m.group(1).decode(errors="ignore").split()[0] if m else ""
        sock.close()
    except OSError as exc:
        log.debug("WS-Discovery unavailable: %s", exc)
    return found
