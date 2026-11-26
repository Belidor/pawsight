"""Labelling page: served, takes its classes from labels.py, and can read a
still-frame ring by id instead of listening to the live broadcast."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pawsight.bus import encode
from pawsight.config import Config
from pawsight.labels import DATASET_CLASSES
from pawsight.web import Dashboard


class LabelPage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.dash = Dashboard(Config(db_path=d / "events.db", snap_dir=d / "s"))
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_page_served_with_its_files(self):
        r = await self.client.get("/label")
        self.assertEqual(r.status, 200)
        html = await r.text()
        self.assertIn('<script type="module" src="/static/label.js">', html)
        self.assertIn('href="/static/label.css"', html)
        self.assertNotIn("<style>", html)
        for path in ("/static/label.js", "/static/label.css"):
            with self.subTest(path=path):
                f = await self.client.get(path)
                self.assertEqual(f.status, 200)
                self.assertEqual(f.headers.get("Cache-Control"), "no-cache")
        js = await (await self.client.get("/static/label.js")).text()
        # every state-changing call must carry the guard header
        self.assertIn('"X-Pawsight": "1"', js)
        self.assertIn('"/api/label/classes"', js)

    async def test_editors_take_classes_from_the_server(self):
        """Both editors build their class buttons from /api/label/classes: a
        hardcoded three would drop boxes of an added class on the next Save."""
        for path in ("/static/label.js", "/static/dashboard.js"):
            with self.subTest(path=path):
                js = await (await self.client.get(path)).text()
                self.assertIn('"/api/label/classes"', js)
        js = await (await self.client.get("/static/label.js")).text()
        self.assertIn('"no_extra"', js)

    async def test_classes_endpoint_lists_extra(self):
        d = await (await self.client.get("/api/label/classes")).json()
        self.assertEqual(d["extra"], d["classes"][3:])

    async def test_dashboard_links_the_page(self):
        html = await (await self.client.get("/?dashboard=1")).text()
        self.assertIn('href="/label"', html)

    async def test_classes_come_from_labels_py(self):
        d = await (await self.client.get("/api/label/classes")).json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["classes"], [DATASET_CLASSES[i] for i in sorted(DATASET_CLASSES)])

    async def test_page_refuses_foreign_host(self):
        r = await self.client.get("/label", headers={"Host": "evil.example"})
        self.assertEqual(r.status, 403)

    async def test_snapshot_meta_unknown_is_404(self):
        r = await self.client.get("/api/snapshot/nope")
        self.assertEqual(r.status, 404)

    async def test_snapshot_meta_after_all_frames_arrived(self):
        sid = "s1"
        frames = [{"ts": 1.0, "size": [640, 360], "boxes": []},
                  {"ts": 2.0, "size": [640, 360],
                   "boxes": [{"cls": "dog", "cx": .5, "cy": .5, "w": .2, "h": .3, "conf": .9}]}]
        self.dash._on_snapshot(encode({"cmd": "snapshot", "id": sid, "ok": True,
                                       "frames": frames}, None))
        # description alone: the JPEGs have not arrived yet
        self.assertEqual((await self.client.get(f"/api/snapshot/{sid}")).status, 404)
        for n in range(2):
            self.dash._on_snapshot(encode({"cmd": "snapshot_frame", "id": sid, "ok": True,
                                           "n": n}, b"\xff\xd8jpeg"))
        d = await (await self.client.get(f"/api/snapshot/{sid}")).json()
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["frames"]), 2)
        self.assertEqual(d["frames"][1]["url"], f"/api/snapshot/{sid}/1.jpg")
        self.assertEqual(d["frames"][1]["boxes"][0]["cls"], "dog")


if __name__ == "__main__":
    unittest.main()
