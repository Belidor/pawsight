"""dashboard.html has no inline styles or script: they live in /static.

Checks that the page links the files, the files are served with cache
revalidation, and the module order is unchanged: go2rtc player before the
dashboard logic.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pawsight.config import Config
from pawsight.web import Dashboard


class DashboardStatic(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.client = TestClient(TestServer(
            Dashboard(Config(db_path=d / "events.db", snap_dir=d / "s")).app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_page_links_files(self):
        html = await (await self.client.get("/?dashboard=1")).text()
        self.assertNotIn("<style>", html)
        self.assertIn('<link rel="stylesheet" href="/static/dashboard.css">', html)
        player = html.index('src="/player/video-stream.js"')
        logic = html.index('<script type="module" src="/static/dashboard.js">')
        self.assertLess(player, logic)
        self.assertEqual(html.count("<script"), 3, "no inline scripts left")

    async def test_every_page_has_the_nav_bar(self):
        # The desktop app window has no browser toolbar: every page needs the
        # shared Back button and tabs, or a page like /setup is a dead end.
        for path in ("/?dashboard=1", "/label", "/zones", "/setup"):
            with self.subTest(path=path):
                html = await (await self.client.get(path)).text()
                self.assertIn('<script src="/static/nav.js" defer></script>', html)
        js = await (await self.client.get("/static/nav.js")).text()
        for href in ('"/?dashboard=1"', '"/label"', '"/zones"', '"/setup"'):
            self.assertIn(href, js)
        self.assertIn("history.back()", js)

    async def test_files_served_without_stale_cache(self):
        for path, kind in (("/static/dashboard.js", "javascript"),
                           ("/static/dashboard.css", "css")):
            with self.subTest(path=path):
                r = await self.client.get(path)
                self.assertEqual(r.status, 200)
                self.assertIn(kind, r.headers["Content-Type"])
                self.assertEqual(r.headers.get("Cache-Control"), "no-cache")
                self.assertGreater(len(await r.text()), 1000)

    async def test_logic_present(self):
        js = await (await self.client.get("/static/dashboard.js")).text()
        for needle in ('"X-Pawsight": "1"', '"/api/stream?src=dog_cam"', "connectLive();"):
            self.assertIn(needle, js)


if __name__ == "__main__":
    unittest.main()
