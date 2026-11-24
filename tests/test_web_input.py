"""Dashboard: request input, the frame cache across threads, and one database per process.

- a malformed number in a request is a 400 with a message, not a 500 with a traceback;
- the /api/stream source must be one of ours, and it goes into the go2rtc URL
  escaped;
- the freeze-frame cache is touched only by the event loop, not the MQTT thread;
- in the brain process the dashboard uses the brain's Store instead of opening a second one.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pawsight.bus import encode
from pawsight.config import Config
from pawsight.store import Store
from pawsight.web import Dashboard

GUARD = {"X-Pawsight": "1"}


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.dash = Dashboard(Config(db_path=self.dir / "events.db",
                                     snap_dir=self.dir / "snapshots"))
        self.asked = []

        async def ask(cmd, payload=None, timeout=6.0):
            self.asked.append((cmd, payload))
            return {"ok": True, "groups": []}

        self.dash.ask = ask
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class MalformedNumbers(_Base):
    async def test_camera_log_limit(self):
        r = await self.client.get("/api/camera_log?limit=abc")
        self.assertEqual(r.status, 400)
        self.assertIn("limit", await r.text())
        r = await self.client.get("/api/camera_log?limit=5")
        self.assertEqual(r.status, 200)

    async def test_dataset_limit(self):
        r = await self.client.get("/api/dataset?limit=many")
        self.assertEqual(r.status, 400)
        self.assertEqual(self.asked, [], "the detector is not asked with a malformed number")
        r = await self.client.get("/api/dataset?limit=10")
        self.assertEqual(r.status, 200)
        self.assertEqual(self.asked[-1][1]["limit"], 10, "the detector gets a number, not a string")

    async def test_cleanup_preview_days_keep(self):
        for body in ({"days": "x"}, {"keep_runs": [1]}, {"days": "1e3"}):
            with self.subTest(body=body):
                r = await self.client.post("/api/cleanup/preview", json=body, headers=GUARD)
                self.assertEqual(r.status, 400)
        r = await self.client.post("/api/cleanup/preview", json={"days": "7"}, headers=GUARD)
        self.assertEqual(r.status, 200)
        self.assertEqual((await r.json())["days"], 7)

    async def test_cleanup_preview_not_object(self):
        r = await self.client.post("/api/cleanup/preview", json=[1, 2], headers=GUARD)
        self.assertEqual(r.status, 400)

    async def test_snapshot_save_frame(self):
        self.dash._snaps["s1"] = {"at": 0, "jpegs": [b"x"]}
        r = await self.client.post("/api/snapshot/s1/save",
                                   json={"frame": "second", "boxes": []}, headers=GUARD)
        self.assertEqual(r.status, 400)

    async def test_bad_json_is_400(self):
        for path in ("/api/ptz", "/api/tracking", "/api/builtin_tracking",
                     "/api/cleanup/apply", "/api/dataset/x/save"):
            with self.subTest(path=path):
                r = await self.client.post(path, data=b"{not json",
                                           headers={**GUARD, "content-type": "application/json"})
                self.assertEqual(r.status, 400)


class OwnStreamsOnly(_Base):
    async def test_foreign_src_rejected_before_go2rtc(self):
        for src in ("evil", "dog_cam&x=1", "../api/config"):
            with self.subTest(src=src):
                r = await self.client.get("/api/stream", params={"src": src})
                self.assertEqual(r.status, 400)

    async def test_own_src_is_sent_escaped(self):
        seen = []

        async def go2rtc_ws(req):
            seen.append(req.query_string)
            ws = web.WebSocketResponse()
            await ws.prepare(req)
            await ws.close()
            return ws

        app = web.Application()
        app.add_routes([web.get("/api/ws", go2rtc_ws)])
        srv = TestServer(app)
        await srv.start_server()
        self.addAsyncCleanup(srv.close)
        self.dash.go2rtc = str(srv.make_url("")).rstrip("/")
        async with self.client.ws_connect("/api/stream?src=dog_cam_sd") as ws:
            await ws.receive(timeout=2)
        self.assertEqual(seen, ["src=dog_cam_sd"])


class FramesOnEventLoop(unittest.IsolatedAsyncioTestCase):
    async def test_bus_thread_does_not_touch_cache(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        dash = Dashboard(Config(db_path=d / "events.db", snap_dir=d / "s"))
        dash._loop = asyncio.get_running_loop()
        raw = encode({"id": "r1", "cmd": "dataset_frame", "name": "a"}, b"\xff\xd8")

        t = threading.Thread(target=dash.on_snapshot, args=(raw,))
        t.start()
        t.join()
        self.assertEqual(dash._snaps, {}, "the MQTT thread wrote the cache itself")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIn("ds:a", dash._snaps)

    def test_without_loop_parses_in_place(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        dash = Dashboard(Config(db_path=d / "events.db", snap_dir=d / "s"))
        dash.on_snapshot(encode({"id": "r1", "cmd": "dataset_frame", "name": "a"}, b"j"))
        self.assertIn("ds:a", dash._snaps)


class OneDatabase(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cfg = Config(db_path=self.d / "events.db", snap_dir=self.d / "s")

    def test_brain_dashboard_takes_its_store(self):
        st = Store(self.cfg.db_path)
        dash = Dashboard(self.cfg, store=st)
        self.assertIs(dash.store(), st)

    def test_setup_mode_opens_its_own(self):
        dash = Dashboard(self.cfg)
        self.assertIsInstance(dash.store(), Store)
        self.assertIs(dash.store(), dash.store())

    def test_brain_passes_store_to_dashboard(self):
        src = (Path(__file__).resolve().parent.parent / "pawsight" / "__main__.py").read_text()
        self.assertIn("store=brain.store", src)


if __name__ == "__main__":
    unittest.main()
