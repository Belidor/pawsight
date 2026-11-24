"""Zone editor end to end without a camera: page -> brain web -> detector -> file.

The bus is replaced by a direct call into the detector's command handler, so
the test covers the real request and reply payloads in both directions.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from pawsight.config import Config
from pawsight.web import Dashboard

try:
    import numpy as np

    from pawsight.detector import Detector
except ImportError:                     # brain image: no OpenCV
    Detector = None

ROOT = Path(__file__).resolve().parents[1]
PAIR = [(447.0, 159.0, 471.0, 175.0), (469.0, 166.0, 497.0, 185.0)]
H = {"X-Pawsight": "1"}


@unittest.skipIf(Detector is None, "no OpenCV: this is the brain image")
class ZoneEditorRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copy(ROOT / "zones.example.yaml", d / "zones.yaml")
        p = mock.patch("pawsight.config.ROOT", d)
        p.start()
        self.addCleanup(p.stop)
        self.dir = d

        cfg = Config(db_path=d / "events.db", snap_dir=d / "s")
        cfg.apply_zones(__import__("yaml").safe_load((d / "zones.yaml").read_text()))
        self.cfg = cfg

        det = Detector.__new__(Detector)
        det.cfg = cfg
        det._names = {0: "bowl", 1: "dog"}
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        det._ring = deque([(1000.0, frame, [(0, 0.9, b) for b in PAIR])], maxlen=6)
        det.anchors, det._anchor_classes = {}, set()
        replies = []
        det.on_event = lambda kind, payload, jpeg: replies.append(payload)
        self.det = det

        dash = Dashboard(cfg)

        async def ask(cmd, payload=None, timeout=6.0):
            replies.clear()
            det._handle(cmd, {"id": "x", **(payload or {})})
            return replies[-1]
        dash.ask = ask
        self.client = TestClient(TestServer(dash.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_page_served(self):
        r = await self.client.get("/zones")
        self.assertEqual(r.status, 200)
        self.assertIn('src="/static/zones.js"', await r.text())
        js = await (await self.client.get("/static/zones.js")).text()
        self.assertIn('"X-Pawsight": "1"', js)

    async def test_get_returns_frame_zones_and_pair(self):
        d = await (await self.client.get("/api/zones")).json()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["size"], [640, 360])
        self.assertEqual(d["pair"], [472, 185])
        self.assertTrue(d["frame_b64"])
        self.assertEqual([z["name"] for z in d["zones"]], ["bowls", "water", "food"])
        self.assertEqual(len(d["drawn"]["bowls"]), 9, "anchored zones placed on this frame")

    async def test_save_reloads_detector_and_brain(self):
        d = await (await self.client.get("/api/zones")).json()
        zones = [{k: z[k] for k in ("name", "label", "notify", "kind")} for z in d["zones"]]
        zones[2]["notify"] = False
        zones.append({"label": "rug", "notify": True, "kind": "bowls",
                      "shape": [[-200, -20], [-150, -20], [-150, 20]]})
        r = await self.client.post("/api/zones", json={"zones": zones, "notify_outside": False},
                                   headers=H)
        self.assertEqual(r.status, 200, await r.text())
        self.assertEqual([z.name for z in self.cfg.zones], ["bowls", "water", "food", "rug"])
        self.assertFalse(next(z for z in self.cfg.zones if z.name == "food").notify)
        self.assertFalse(self.cfg.notify_outside)
        self.assertIn("rug", self.det.anchors)
        self.assertIn("rug:", (self.dir / "zones.yaml").read_text())

    async def test_save_error_is_400_and_file_untouched(self):
        before = (self.dir / "zones.yaml").read_text()
        r = await self.client.post("/api/zones", json={"zones": []}, headers=H)
        self.assertEqual(r.status, 400)
        self.assertIn("built-in", (await r.json())["error"])
        self.assertEqual((self.dir / "zones.yaml").read_text(), before)

    async def test_save_needs_guard_header(self):
        r = await self.client.post("/api/zones", json={"zones": []})
        self.assertEqual(r.status, 403)


if __name__ == "__main__":
    unittest.main()
