"""The camera return command from the dashboard down to the patrol loop."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pawsight.config import Config
from pawsight.web import Dashboard

try:
    from pawsight.detector import Detector
except ImportError:  # the brain container deliberately has no OpenCV/YOLO
    Detector = None

GUARD = {"X-Pawsight": "1"}


@unittest.skipIf(Detector is None, "detector not installed in this environment")
class DetectorCommand(unittest.TestCase):
    def test_return_passed_to_follow_loop(self):
        class Follow:
            def __init__(self):
                self.called = 0

            def request_home(self, now):
                self.called += 1
                return True

        det = Detector.__new__(Detector)
        det.follow = Follow()
        replies = []
        det.on_event = lambda kind, msg, blob=None: replies.append(msg)

        det._handle("camera_home", {"id": "home-1"})

        self.assertEqual(det.follow.called, 1)
        self.assertEqual(replies, [{"cmd": "camera_home", "id": "home-1", "ok": True}])

    def test_without_patrol_command_refuses_honestly(self):
        det = Detector.__new__(Detector)
        det.follow = None
        replies = []
        det.on_event = lambda kind, msg, blob=None: replies.append(msg)

        det._handle("camera_home", {"id": "home-2"})

        self.assertFalse(replies[0]["ok"])
        self.assertIn("patrol", replies[0]["error"])


class DashboardRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.dash = Dashboard(Config(db_path=self.dir / "events.db",
                                     snap_dir=self.dir / "snapshots"))
        self.asked = []

        async def ask(cmd, payload=None, timeout=6.0):
            self.asked.append((cmd, payload, timeout))
            return {"ok": True}

        self.dash.ask = ask
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_button_reaches_detector(self):
        r = await self.client.post("/api/home", headers=GUARD, json={})
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {"ok": True})
        self.assertEqual(self.asked, [("camera_home", None, 6.0)])

    async def test_button_on_dashboard_waits_for_detector(self):
        r = await self.client.get("/?dashboard=1")
        html = await r.text()
        self.assertIn('id="camera-home"', html)
        self.assertIn('id="camera-home" type="button" disabled', html)
        self.assertIn("Return camera to bowls", html)

    async def test_foreign_page_cannot_move_camera(self):
        r = await self.client.post("/api/home", json={})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.asked, [])

    async def test_detector_refusal_shown_as_refusal(self):
        async def ask(cmd, payload=None, timeout=6.0):
            return {"ok": False, "error": "patrol disabled"}

        self.dash.ask = ask
        r = await self.client.post("/api/home", headers=GUARD, json={})
        self.assertEqual(r.status, 409)
        self.assertEqual((await r.json())["error"], "patrol disabled")


if __name__ == "__main__":
    unittest.main()
