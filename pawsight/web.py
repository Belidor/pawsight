"""Web dashboard: annotated video, camera control, drinking counter.

Lives in the container together with the brain. Camera control works from
here because miIO is outbound UDP to the local network, which the container
can do (verified by a handshake and a property read from inside the container).

The go2rtc player and stream are proxied through this same server so the page
lives on one origin. The alternative is go2rtc's `api.origin: "*"`, but then
its API becomes reachable by ANY site open in the user's browser: 127.0.0.1
is reachable from any page. Proxying is cheap: it relays ready packets, no
transcoding.

The module is split by purpose (code review, 2026-11-21); URLs and behaviour are unchanged:
  webguard.py    — Host/Origin/X-Pawsight guards and input parsing;
  web_dataset.py — freeze frames, dataset, fine-tuning, cleanup;
  web_go2rtc.py  — player and stream proxy, go2rtc API calls;
  web_setup.py   — first-run setup;
  here           — state, broadcast, camera, reports, route assembly.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import setup
from .bus import KINDS, TOPIC_CMD, decode
from .follow import MANUAL_DIRS
from .config import Config
from .store import Store
from .web_dataset import DatasetRoutes
from .web_go2rtc import STREAMS, Go2rtcProxy  # noqa: F401 — the name's old import path
from .web_setup import SetupRoutes
from .webguard import (GUARD_HEADER, SAFE_HOSTS, SAFE_ORIGINS, _host_ok,  # noqa: F401
                       _int_arg, _same_origin, guard, read_body)

log = logging.getLogger("pawsight.web")
STATIC = Path(__file__).parent / "static"


def _day_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class Dashboard(DatasetRoutes, Go2rtcProxy, SetupRoutes):
    _body = staticmethod(read_body)

    def __init__(self, cfg: Config, bus=None, logs=None, store: Store | None = None) -> None:
        self.cfg = cfg
        self.bus = bus        # for detector commands (recapture the tilt reference)
        self.logs = logs      # LogBuffer: raw log stream for the tab
        # go2rtc lives on the host. The default address is the native one; in
        # the container it is overridden to host.docker.internal
        # (docker-compose.yml). The default used to be the container name, and
        # in the native stack (the .env.example default) it simply did not resolve.
        self.go2rtc = os.getenv("GO2RTC_URL", "http://127.0.0.1:1984")
        self._http: aiohttp.ClientSession | None = None
        self.latest: dict = {}
        self._clients: set[web.WebSocketResponse] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ptz = None
        # Camera state is cached and refreshed in the background: polling the
        # camera over miIO takes ~450 ms and must not happen in a request
        # handler: the server is single-threaded and everything stalls for that
        # time, including the video proxy.
        self._tracking: bool | None = None      # the camera's built-in tracking
        self.enforce_builtin_off = os.getenv("PAWSIGHT_ENFORCE_BUILTIN_OFF", "1") == "1"
        self._builtin_reenabled = 0
        self._follow: bool | None = None        # our own dog following
        self._camera_ok = False
        self._camera_checked = 0.0
        # One long-lived connection instead of a new one per call: the old
        # code did connect + executescript(SCHEMA) + commit for every bus
        # message (3.74 ms with fsync), and on the MQTT thread at that.
        #
        # In the brain process the Store is already open in Brain; we take that
        # one (store=...). Two connections to one database in one process are
        # two independent locks: brain writes and dashboard cleanup would
        # collide at the SQLite level ("database is locked") instead of on the
        # RLock inside Store. The dashboard opens its own connection only in
        # `setup` mode, where there is no brain.
        self._store: Store | None = store
        self._store_lock = threading.Lock()
        self._camlog_buf: list[tuple] = []
        self._camlog_lock = threading.Lock()
        # a queue per client: a slow browser must not hold up the others
        self._queues: dict[int, "asyncio.Queue[str]"] = {}
        self._log_pending: list[dict] = []
        self._log_lock = threading.Lock()
        self._pending: dict[str, "asyncio.Future"] = {}
        self._status: dict = {}
        self._status_at = 0.0
        self._snaps: dict[str, dict] = {}
        self._clean_tokens: dict[str, dict] = {}
        self._clean_days = int(os.getenv("PAWSIGHT_CLEAN_DAYS", "14"))
        self._clean_keep = int(os.getenv("PAWSIGHT_CLEAN_KEEP_RUNS", "1"))
        if logs is not None:
            logs.subscribe(self._on_log_record)

    def store(self) -> Store:
        with self._store_lock:
            if self._store is None:
                self._store = Store(self.cfg.db_path)
            return self._store

    # ---- camera activity feed ----

    def camlog(self, ts: float, kind: str, level: str, text: str) -> None:
        """From MQTT (detector) or from the dashboard itself (manual pans).

        Called from a FOREIGN thread. Writes go to the DB in batches: a commit
        per record cost 3.74 ms with fsync, and the expanded feed produces up
        to dozens of rows per minute.
        """
        if kind not in KINDS:
            kind = "manual"
        with self._camlog_lock:
            self._camlog_buf.append((ts, kind, level, text))
            due = len(self._camlog_buf) >= 64
        if due:
            self._camlog_flush()
        self.push({"type": "camera_log", "ts": ts, "kind": kind,
                   "level": level, "text": text})

    def _camlog_flush(self) -> None:
        with self._camlog_lock:
            rows, self._camlog_buf = self._camlog_buf, []
        if not rows:
            return
        try:
            self.store().add_camlog_many(rows)
        except Exception as e:
            log.warning("camera_log not written (%d rows): %s", len(rows), e)

    async def _camlog_writer(self, _app) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self._exec(self._camlog_flush)

    # ---- broadcast to clients ----

    def push(self, msg: dict) -> None:
        """A message to every open dashboard. Can be called from any thread."""
        loop = self._loop
        if not loop or not self._queues:
            return
        loop.call_soon_threadsafe(self._fanout, json.dumps(msg, ensure_ascii=False))

    def _fanout(self, payload: str) -> None:
        """Each client gets its own queue.

        The broadcast used to be a sequential loop with an await per client,
        all on the same event loop that proxies the video: one client
        throttled by its browser slowed the rest and made the picture stutter.
        Now a slow client loses its own rows, not other clients' time; it is
        told about the loss with a separate record.
        """
        for q in list(self._queues.values()):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()          # evict the oldest
                    q.put_nowait(payload)
                except Exception:
                    pass

    async def _ws_writer(self, ws, q: "asyncio.Queue[str]") -> None:
        while True:
            payload = await q.get()
            await ws.send_str(payload)

    async def api_camera_log(self, req):
        limit = max(1, min(_int_arg(req.query.get("limit"), 60, "limit"), 300))
        rows = await self._exec(lambda: self.store().camlog_recent(limit))
        return web.json_response({"items": rows})

    # ---- raw log stream ----

    def _on_log_record(self, rec: dict) -> None:
        """From the bus thread or the logging thread. Accumulate and send in
        batches: one WebSocket frame per line during an ffmpeg burst is wasteful."""
        with self._log_lock:
            self._log_pending.append(rec)
            if len(self._log_pending) > 500:      # the client won't read it anyway
                del self._log_pending[:-500]

    async def _log_pump(self, _app) -> None:
        while True:
            await asyncio.sleep(0.25)
            with self._log_lock:
                batch, self._log_pending = self._log_pending, []
            if batch and self._queues:
                self._fanout(json.dumps({"type": "logs", "items": batch},
                                        ensure_ascii=False))

    async def api_logs(self, req):
        if self.logs is None:
            return web.json_response({"items": [], "next": 0, "dropped": 0})
        try:
            limit = min(max(int(req.query.get("limit", "300")), 1), 1000)
            since = int(req.query.get("since", "0"))
        except ValueError:
            raise web.HTTPBadRequest(text="limit and since must be numbers")
        items = await self._exec(
            lambda: self.logs.query(since=since, limit=limit,
                                    level=req.query.get("level") or None,
                                    q=(req.query.get("q") or "")[:80] or None,
                                    src=req.query.get("src") or None))
        return web.json_response({"items": items, "next": self.logs.last_seq(),
                                  "dropped": self.logs.dropped})

    # ---- detector commands that wait for a reply ----

    async def ask(self, cmd: str, payload: dict | None = None,
                  timeout: float = 6.0) -> dict:
        """Request-reply on top of the publish-subscribe bus.

        Four features need it at once: snapshot, saving to the dataset,
        starting training, cleanup. So the id correlation is built once, here.
        """
        if not self.bus:
            return {"ok": False, "error": "bus unavailable"}
        rid = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[rid] = fut
        self.bus.publish(TOPIC_CMD, json.dumps(
            {"cmd": cmd, "id": rid, "ts": time.time(), **(payload or {})},
            ensure_ascii=False).encode())
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "timeout": True,
                    "error": "detector did not answer within %.0f s" % timeout}
        finally:
            self._pending.pop(rid, None)

    def on_reply(self, raw: bytes) -> None:
        """From the bus thread. The event loop must not be touched from there; only
        call_soon_threadsafe."""
        try:
            d, _ = decode(raw)
        except Exception:
            return
        fut = self._pending.get(d.get("id"))
        loop = self._loop
        if fut and loop and not fut.done():
            loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(d))

    # ---- model and dataset state ----

    def on_status(self, st: dict) -> None:
        self._status = st
        self._status_at = time.time()
        self.push({"type": "model", "status": st})

    async def api_model(self, _req):
        if not self._status:
            return web.json_response(
                {"ok": False, "error": "detector has not sent its state"}, status=503)
        return web.json_response({"ok": True, "status": self._status,
                                  "age_sec": round(time.time() - self._status_at, 1)})

    # ---- input from MQTT (called from another thread) ----

    def on_observation(self, obs: dict) -> None:
        self.latest = obs
        self.push(obs)

    # ---- camera ----

    def ptz(self):
        if self._ptz is None:
            from .ptz import CameraPTZ

            self._ptz = CameraPTZ()
        return self._ptz

    async def _exec(self, fn, *args):
        """Blocking call — goes to the thread pool; the event loop is not touched."""
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    def _poll_camera_sync(self) -> None:
        if not os.getenv("CAMERA_IP"):
            # A plain RTSP camera: no pan/tilt, nothing to ask. Otherwise the
            # log would get "camera unavailable" every 30 s on an install where
            # everything is fine.
            self._camera_ok = False
            self._camera_checked = time.time()
            return
        try:
            self._tracking = self.ptz().get_tracking()
            self._camera_ok = True
            # Owner's policy: the camera's built-in tracking stays off — it
            # tilts the camera and follows people. It has been seen turning
            # itself back on (or via the Mi Home app): we turn it off and count.
            if self._tracking and self.enforce_builtin_off:
                self.ptz().set_tracking(False, verify=False)
                self._builtin_reenabled += 1
                self._tracking = False
                log.warning("the camera's built-in tracking was on — turned it off "
                            "(%d times so far). If you are doing this in Mi Home, set "
                            "PAWSIGHT_ENFORCE_BUILTIN_OFF=0", self._builtin_reenabled)
        except Exception as e:
            log.warning("camera unavailable: %s", e)
            self._camera_ok = False
        self._camera_checked = time.time()

    async def _camera_poller(self, app) -> None:
        """Background camera state refresh every 30 s, off the event loop."""
        while True:
            await self._exec(self._poll_camera_sync)
            await asyncio.sleep(30)

    async def _start_bg(self, app) -> None:
        app["poller"] = asyncio.create_task(self._camera_poller(app))
        app["camlog_writer"] = asyncio.create_task(self._camlog_writer(app))
        app["log_pump"] = asyncio.create_task(self._log_pump(app))

    async def _stop_bg(self, app) -> None:
        app["poller"].cancel()
        app["camlog_writer"].cancel()
        app["log_pump"].cancel()
        await self._exec(self._camlog_flush)
        if self._http and not self._http.closed:
            await self._http.close()

    # ---- data ----

    def drinks_today(self, since: float | None = None) -> dict:
        """Daily summary — via the SHARED Store, not a connection of its own.

        There used to be a bare sqlite3.connect per /api/state request. It is
        also what failed: a second connection inside the brain process hit an
        orphaned WAL and returned "disk I/O error" — the dashboard returned 500
        over two thousand times in a row (docs/architecture.md, 2026-09-06). One
        database, one door.

        since is passed in when the day is computed by the host CLI: the image
        TZ and the Mac's TZ may differ, and "today" would then silently become
        a different day.
        """
        st = self.store()
        if since is None:
            since = _day_start()
        rows = st.bouts_by_kind(since, "drink")
        allb = st.bout_summary(since)
        last = st.events_recent(8)
        return {
            "drinks": [{"ts": r[0], "dur": r[1], "conflict": bool(r[2])} for r in rows],
            "total_sec": sum(r[1] for r in rows),
            "by_kind": {r[0]: {"count": r[1], "sec": r[2] or 0} for r in allb},
            "events": [{"ts": r[0], "zone": r[1], "state": r[2], "vlm": r[3]} for r in last],
            # Two stat(2) calls per request: the price of seeing SQLite's
            # service files being swapped immediately, not after three hours of lost writes.
            "db_orphaned": st.wal_orphaned(),
        }

    # ---- reports for the host CLI ----
    #
    # In docker mode the database belongs to the brain, and `pawsight today` on
    # the host cannot open it (pawsight/store.py, ForeignDatabase). To keep the
    # promise of AGENTS.md section 5, reports are served from here as RAW rows,
    # and the CLI still does the formatting. Then the output matches native
    # mode by construction, not by a lucky coincidence of two formatters.
    #
    # The day boundary (since) is computed by the caller: the image TZ is set
    # in docker-compose.yml and may differ from the Mac's, and "today" must
    # mean today for the user.

    def _since(self, req) -> float:
        raw = req.query.get("since")
        if raw is None:
            return _day_start()
        try:
            return float(raw)
        except ValueError:
            raise web.HTTPBadRequest(text="since: epoch time expected")

    def _report_today(self, since: float) -> dict:
        st = self.store()
        return {"summary": st.bout_summary(since), "events": st.today(since)}

    def _report_drinks(self, since: float) -> dict:
        return {"rows": self.store().bouts_by_kind(since, "drink")}

    async def api_report_today(self, req):
        # A read-only route, but the origin check is done explicitly: the guard
        # does not require it for GET, and this response is the whole day's
        # history. It does not get in the way of the host CLI (urllib sends
        # neither Origin nor Sec-Fetch-Site), but it does stop a foreign tab:
        # the browser will set Sec-Fetch-Site: cross-site.
        _same_origin(req)
        return web.json_response(await self._exec(self._report_today, self._since(req)))

    async def api_report_drinks(self, req):
        _same_origin(req)
        return web.json_response(await self._exec(self._report_drinks, self._since(req)))

    # ---- routes ----

    async def index(self, req):
        """Until first setup, "/" shows the wizard instead of an empty dashboard.

        The sign is an empty streams.dog_cam in go2rtc.yaml. The user has
        already opened 127.0.0.1:8099 as the installer told them, and there is
        no point making them guess a second address. The "open the dashboard
        anyway" link leads here with ?dashboard=1.
        """
        # writable is checked not for writing but for truth: in the container
        # go2rtc.yaml is not mounted, so "camera not configured" there would
        # only mean "file not visible", and the dashboard would vanish for good.
        if "dashboard" not in req.query and await self._exec(self._needs_setup):
            return web.FileResponse(STATIC / "setup.html")
        return web.FileResponse(STATIC / "dashboard.html")

    @staticmethod
    def _needs_setup() -> bool:
        return setup.writable()["go2rtc"] and not setup.camera_configured()

    async def api_state(self, _req):
        obs_age = time.time() - self.latest.get("ts", 0) if self.latest else None
        # A database failure must not take the whole dashboard down with a 500:
        # then neither the camera, nor the detector, nor the reason is visible —
        # exactly how the 2026-09-06 incident looked to the user. Return 200
        # and an honest db_error field.
        db_error = None
        try:
            stats = await self._exec(self.drinks_today)  # SQLite also off the loop
        except Exception as e:
            stats = {"drinks": [], "total_sec": 0, "by_kind": {}, "events": [],
                     "db_orphaned": False}
            db_error = str(e)
            if db_error != getattr(self, "_db_err_last", None):
                self._db_err_last = db_error      # log once per cause
                log.error("database unavailable: %s", e)
        return web.json_response({
            "db_error": db_error,
            "tracking": self._tracking,
            "follow": ((self.latest.get("follow") or {}).get("enabled")
                       if self.latest else self._follow),
            "camera_ok": self._camera_ok,
            "camera_checked_ago": round(time.time() - self._camera_checked, 1),
            "builtin_reenabled": self._builtin_reenabled,
            "detector_ok": obs_age is not None and obs_age < 15,
            # Paused during fine-tuning — from the detector status (retain).
            "detector_paused": bool((self._status.get("detector") or {}).get("paused")),
            "obs_age": round(obs_age, 1) if obs_age is not None else None,
            "latest": self.latest,
            **stats,
        })

    def _detector_alive(self) -> bool:
        """The detector is alive: fresh observations or, while recognition is
        paused, a fresh status (published every 30 s)."""
        now = time.time()
        if self.latest and now - float(self.latest.get("ts") or 0) < 15:
            return True
        paused = bool((self._status.get("detector") or {}).get("paused"))
        return paused and now - self._status_at < 90

    async def _rotate(self, d: str) -> dict:
        """Pan via the detector, or directly if there is none.

        Via the detector because it owns the motor: its patrol keeps odometry
        (patrol.pos), and a pan behind its back shifted "home". Directly only
        when there is no detector (or it has no PTZ): then there is no odometry
        either. If the detector is alive but did not answer in time, we do NOT
        pan directly: the command may already have reached the motor, and a
        retry would pan the camera twice.
        """
        if self.bus and self._detector_alive():
            r = await self.ask("ptz_manual", {"dir": d}, timeout=10.0)
            if not r.get("no_ptz"):
                if r.get("timeout"):
                    log.warning("manual pan %s: detector did not answer — result "
                                "unknown, not retrying directly", d)
                    r = {"ok": False, "status": 504, "error": (
                        "detector did not answer within 10 s — the pan may have happened. "
                        "Check the video before pressing again.")}
                return r
        log.warning("manual pan %s directly: detector %s — patrol odometry "
                    "will not account for it", d, "has no PTZ" if self.bus else "unavailable")
        try:
            await self._exec(self.ptz().rotate, d)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def api_ptz(self, req):
        d = str((await self._body(req)).get("dir") or "")
        if d not in MANUAL_DIRS:
            raise web.HTTPBadRequest(text="dir: left, right, up or down")
        log.info("manual pan from the dashboard: %s", d)
        self.camlog(time.time(), "manual", "info", f"manual pan from the dashboard: {d}")
        # A manual tilt is the new "as it is now": tilt hold must not
        # immediately undo the owner's action. First pause the loop, then
        # recapture the reference after the move.
        if d in ("up", "down") and self.bus:
            self.bus.publish("pawsight/commands", json.dumps({"cmd": "tilt_pause"}).encode())
        r = await self._rotate(d)
        if r.get("ok"):
            if d in ("up", "down") and self.bus:
                await asyncio.sleep(2.5)         # let the camera get there
                self.bus.publish("pawsight/commands", json.dumps({"cmd": "tilt_recapture"}).encode())
            return web.json_response({"ok": True, "dir": d})
        err = str(r.get("error") or "")
        # At the limit of travel the camera answers `{'code': -4000, 'message':
        # 'right overflow'}`, to a manual pan as well. Raw JSON in a toast read
        # as "it broke", although it is the firmware's honest answer: it will
        # not let anyone go further that way. The limit is a software counter
        # and drifts relative to the scene (docs/architecture.md, 2026-09-07).
        if "overflow" in err.lower() or r.get("edge"):
            side = {"left": "left", "right": "right", "up": "top", "down": "bottom"}.get(d, "")
            self.camlog(time.time(), "edge", "warning", f"manual pan {d}: camera at the edge of travel")
            return web.json_response({
                "ok": False, "edge": d,
                "error": (f"camera is at the {side} edge of travel — the firmware won't go further. "
                          "Pan the other way; if there is clearly room there, "
                          "rebooting the camera helps (the pan counter drifts)."),
            }, status=409)
        return web.json_response({"ok": False, "error": err}, status=r.get("status") or 400)

    async def api_home(self, _req):
        """Ask the native detector to return the camera to its home position."""
        r = await self.ask("camera_home", timeout=6.0)
        if r.get("ok"):
            self.camlog(time.time(), "manual", "info",
                        "camera return to the bowls requested from the dashboard")
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_tracking(self, req):
        """The "follow the dog" toggle.

        This is OUR following (pan only, dog only), not the camera's built-in
        one: that one tilts and follows people, so it is kept off.
        """
        on = bool((await self._body(req)).get("on"))
        if not self.bus:
            return web.json_response({"ok": False, "error": "bus unavailable"}, status=500)
        self.bus.publish("pawsight/commands",
                         json.dumps({"cmd": "follow_on" if on else "follow_off"}).encode())
        self._follow = on
        return web.json_response({"ok": True, "requested": on})

    async def api_builtin_tracking(self, req):
        """The camera's built-in tracking — only to turn it off / check it."""
        on = bool((await self._body(req)).get("on"))
        try:
            await self._exec(self.ptz().set_tracking, on, False)
            self._tracking = on
            asyncio.get_running_loop().call_later(
                4, lambda: asyncio.ensure_future(self._exec(self._poll_camera_sync)))
            return web.json_response({"ok": True, "requested": on})
        except Exception as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def api_live(self, req):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(req)
        self._clients.add(ws)
        q: "asyncio.Queue[str]" = asyncio.Queue(maxsize=200)
        self._queues[id(ws)] = q
        writer = asyncio.create_task(self._ws_writer(ws, q))
        if self.latest:
            await ws.send_str(json.dumps(self.latest))
        try:
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            writer.cancel()
            self._queues.pop(id(ws), None)
            self._clients.discard(ws)
        return ws

    # ---- zone editor ----

    async def zones_page(self, _req):
        return web.FileResponse(STATIC / "zones.html")

    async def api_zones_get(self, _req):
        """Zones, the latest detector frame and the bowls on it, from the detector:
        zones.yaml lives on the host next to it, in both stacks."""
        r = await self.ask("zones_get", timeout=8.0)
        return web.json_response(r, status=200 if r.get("ok") else 409)

    async def api_zones_save(self, req):
        body = await self._body(req)
        r = await self.ask("zones_save", {"edit": body}, timeout=8.0)
        if not r.get("ok"):
            return web.json_response(r, status=504 if r.get("timeout") else 400)
        # Same Config object as the brain: notifications follow at once.
        self.cfg.apply_zones(r["raw"])
        log.info("zones updated from the editor")
        return web.json_response({"ok": True})

    @staticmethod
    async def _static_no_cache(req, resp) -> None:
        """/static with revalidation: the dashboard script and styles are now
        separate files, and after an update the browser must not keep an old
        script under new markup. On loopback revalidation is a 304 in a millisecond.
        The pages themselves too: the desktop app's window (WebKit) has no
        reload button, so a cached old page would stay until the cache expired."""
        if req.path.startswith("/static/") or resp.content_type == "text/html":
            resp.headers["Cache-Control"] = "no-cache"

    def app(self) -> web.Application:
        app = web.Application(middlewares=[guard])
        app.on_response_prepare.append(self._static_no_cache)
        app.on_startup.append(self._start_bg)
        app.on_cleanup.append(self._stop_bg)
        app.add_routes([
            web.get("/", self.index),
            web.get("/api/state", self.api_state),
            web.get("/api/live", self.api_live),
            web.get("/api/camera_log", self.api_camera_log),
            web.get("/api/report/today", self.api_report_today),
            web.get("/api/report/drinks", self.api_report_drinks),
            web.get("/setup", self.setup_page),
            web.get("/zones", self.zones_page),
            web.get("/label", self.label_page),
            web.get("/api/label/classes", self.api_label_classes),
            web.get("/api/zones", self.api_zones_get),
            web.post("/api/zones", self.api_zones_save),
            web.get("/api/setup/state", self.api_setup_state),
            web.get("/api/setup/webcams", self.api_setup_webcams),
            web.get("/api/logs", self.api_logs),
            web.get("/api/model", self.api_model),
            web.post("/api/snapshot", self.api_snapshot),
            web.get("/api/snapshot/{sid}/{n}.jpg", self.api_snapshot_jpeg),
            web.get("/api/snapshot/{sid}", self.api_snapshot_meta),
            web.post("/api/snapshot/{sid}/save", self.api_snapshot_save),
            web.delete("/api/snapshot/{sid}", self.api_snapshot_drop),
            web.get("/api/dataset", self.api_dataset),
            web.get("/api/dataset/{name}.jpg", self.api_dataset_frame),
            web.get("/api/dataset/{name}/meta", self.api_dataset_meta),
            web.post("/api/dataset/{name}/hint", self.api_dataset_hint),
            web.post("/api/dataset/{name}/save", self.api_dataset_save),
            web.delete("/api/dataset/{name}", self.api_dataset_delete),
            web.post("/api/train", self.api_train),
            web.post("/api/train/stop", self.api_train_stop),
            web.post("/api/cleanup/preview", self.api_cleanup_preview),
            web.post("/api/cleanup/apply", self.api_cleanup_apply),
            web.get("/player/{name}", self.player_js),
            web.get("/api/stream", self.stream_ws),
            web.post("/api/ptz", self.api_ptz),
            web.post("/api/home", self.api_home),
            web.post("/api/tracking", self.api_tracking),
            web.post("/api/builtin_tracking", self.api_builtin_tracking),
            web.post("/api/setup/probe", self.api_setup_probe),
            web.post("/api/setup/camera", self.api_setup_camera),
            web.post("/api/setup/telegram", self.api_setup_telegram),
            web.post("/api/setup/bowls", self.api_setup_bowls),
            web.post("/api/setup/device_token", self.api_setup_device_token),
            web.static("/static", STATIC),
        ])
        return app

    def run(self, host: str = "127.0.0.1", port: int = 8099) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        log.info("dashboard listening on %s:%s", host, port)
        web.run_app(self.app(), host=host, port=port, loop=self._loop,
                    print=None, access_log=None)
