"""Dashboard web server: static page, JSON API, live snapshots and a live event stream."""
import asyncio
from pathlib import Path

from aiohttp import web

from .config import LEVELS

STATIC = Path(__file__).parent / "static"


def create_app(ctx) -> web.Application:
    app = web.Application()

    async def index(_):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def events(req):
        try:
            limit = max(1, min(int(req.query.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        level = req.query.get("level", "").upper()
        return web.json_response(ctx.db.recent(limit, level if level in LEVELS else None))

    async def cameras(_):
        return web.json_response(ctx.camera_list())

    async def status(_):
        return web.json_response(ctx.status())

    async def live(req):
        cam = req.match_info["camera"]
        if cam not in ctx.api.cameras:
            raise web.HTTPNotFound()
        try:
            height = max(120, min(int(req.query.get("h", 360)), 1080))
        except ValueError:
            height = 360
        data = await ctx.api.latest(cam, height)
        if not data:
            raise web.HTTPServiceUnavailable()
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def stream(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        ctx.hub.clients.add(queue)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=15)
                    await resp.write(f"data: {data}\n\n".encode())
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, ConnectionError):
            pass
        finally:
            ctx.hub.clients.discard(queue)
        return resp

    async def test_alert(_):
        return web.json_response(await ctx.test_alert())

    def require_json(req):
        # Only our own page sends JSON; this blocks simple cross-site form posts.
        if req.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON required")

    async def areas(req):
        return web.json_response(ctx.list_areas(req.match_info["camera"]))

    async def save_area(req):
        require_json(req)
        try:
            body = await req.json()
            area = await ctx.save_area(req.match_info["camera"], body.get("name"), body.get("kind"),
                                       body.get("points") or [], body.get("id"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(area)

    async def delete_area(req):
        require_json(req)
        try:
            await ctx.delete_area(req.match_info["camera"], req.match_info["zone"])
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/events", events)
    app.router.add_get("/api/cameras", cameras)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/live/{camera}.jpg", live)
    app.router.add_get("/api/stream", stream)
    app.router.add_post("/api/test-alert", test_alert)
    async def share_pictures(req):
        require_json(req)
        if not ctx.cloud.enabled:
            return web.json_response({"error": "Firebase is not set up"}, status=400)
        if ctx.cloud.picture_mode() != "app":
            return web.json_response({"error": "Locked in rules.yml (cloud.upload_snapshots)"}, status=400)
        body = await req.json()
        allowed = bool(body.get("share"))
        ctx.cloud.set_share_pictures(allowed)
        return web.json_response({"share": allowed})

    # ---------- add / remove cameras from the dashboard ----------
    async def body_of(req) -> dict:
        require_json(req)
        try:
            return await req.json()
        except Exception:
            raise web.HTTPBadRequest(text="Bad JSON")

    def fail(exc: Exception, status: int = 400):
        return web.json_response({"error": str(exc)}, status=status)

    async def setup_info(_):
        return web.json_response({"subnet": ctx.setup.suggest_subnet()})

    async def setup_scan(req):
        body = await body_of(req)
        try:
            return web.json_response(await ctx.setup.scan(str(body.get("subnet") or ctx.setup.suggest_subnet())))
        except ValueError as exc:
            return fail(exc)

    async def setup_probe(req):
        body = await body_of(req)
        try:
            port = int(body.get("rtsp_port") or 554)
            return web.json_response(await ctx.setup.probe(str(body.get("ip", "")).strip(),
                                                           str(body.get("username", "")), str(body.get("password", "")), port))
        except ValueError as exc:
            return fail(exc)

    async def setup_preview(req):
        try:
            data = await ctx.setup.preview(req.match_info["session"])
        except ValueError as exc:
            return fail(exc)
        if not data:
            raise web.HTTPServiceUnavailable(text="No picture from the camera")
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})

    async def setup_add(req):
        body = await body_of(req)
        try:
            job = ctx.setup.start_add(str(body.get("session", "")), str(body.get("name", "")),
                                      str(body.get("area", "")), bool(body.get("face", True)))
        except ValueError as exc:
            return fail(exc)
        return web.json_response({"job": job})

    async def camera_remove(req):
        require_json(req)
        try:
            job = ctx.setup.start_remove(req.match_info["camera"])
        except ValueError as exc:
            return fail(exc)
        return web.json_response({"job": job})

    async def setup_job(req):
        try:
            return web.json_response(ctx.setup.job(req.match_info["job"]))
        except ValueError as exc:
            return fail(exc, 404)

    app.router.add_get("/api/setup", setup_info)
    app.router.add_post("/api/setup/scan", setup_scan)
    app.router.add_post("/api/setup/probe", setup_probe)
    app.router.add_get("/api/setup/preview/{session}.jpg", setup_preview)
    app.router.add_post("/api/setup/add", setup_add)
    app.router.add_get("/api/setup/job/{job}", setup_job)
    app.router.add_delete("/api/cameras/{camera}", camera_remove)
    app.router.add_post("/api/cloud/pictures", share_pictures)
    app.router.add_get("/api/areas/{camera}", areas)
    app.router.add_post("/api/areas/{camera}", save_area)
    app.router.add_delete("/api/areas/{camera}/{zone}", delete_area)
    app.router.add_static("/snapshots", ctx.snapshot_dir)
    app.router.add_static("/static", STATIC)
    return app
