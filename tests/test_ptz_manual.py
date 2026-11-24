"""A manual pan from the dashboard goes through the detector and is counted by the patrol.

/api/ptz used to turn the camera over its own miIO connection from the brain
process, while the patrol in the detector kept odometry (patrol.pos) and did not
know about the manual pan: "home" ended up in the wrong place afterwards. And
set_motor was retried after a timeout: a camera busy with the motor is silent on
UDP, and the retry turned it twice.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiohttp.test_utils import TestClient, TestServer

from pawsight.config import Config
from pawsight.detector import Detector
from pawsight.follow import EDGE_BLOCK_SEC, DogFollow
from pawsight.patrol import LEFT_FRAC, RIGHT_FRAC, Patrol
from pawsight.web import Dashboard
from tests.test_follow import Worker

GUARD = {"X-Pawsight": "1"}


class SetMotorWithoutRetries(unittest.TestCase):
    def test_rotate_retries_0_reads_keep_retries(self):
        from pawsight.ptz import CameraPTZ

        cam = CameraPTZ(ip="127.0.0.1", token="0" * 32)
        cam.dev = MagicMock()
        cam.rotate("right")
        args, kw = cam.dev.send.call_args
        self.assertEqual(args[0], "set_motor")
        self.assertEqual(kw.get("retries"), 0)
        cam.dev.send.reset_mock()
        cam.dev.send.return_value = ["on"] * 8
        cam.dev._did = 1
        with patch.object(CameraPTZ, "get_tracking", return_value=False):
            cam.status()
        self.assertNotIn("retries", cam.dev.send.call_args.kwargs, "reads may be retried")


class FollowAccountsForManualPan(unittest.TestCase):
    def setUp(self):
        self.worker = Worker()
        self.patrol = Patrol()
        self.f = DogFollow(object(), 640, worker=self.worker, patrol=self.patrol)
        self.replies = []
        self.f.on_manual = lambda *a: self.replies.append(a)
        self.steps = 0
        self.f.on_step = lambda: setattr(self, "steps", self.steps + 1)

    def test_pan_right_moves_odometry(self):
        self.assertTrue(self.f.manual("right", "r1", now=10.0))
        self.assertEqual(self.worker.commands, [("right", "manual")])
        self.assertEqual(self.patrol.pos, 0.0, "not counted before the camera answers")
        self.worker.finish(True)
        self.f._drain_results(11.0)
        self.assertAlmostEqual(self.patrol.pos, RIGHT_FRAC)
        self.assertEqual(self.replies, [("r1", "right", True, None)])
        self.assertEqual(self.steps, 1, "the height reference is recaptured after a pan")

    def test_left(self):
        self.f.manual("left", "r2", now=10.0)
        self.worker.finish(True)
        self.f._drain_results(11.0)
        self.assertAlmostEqual(self.patrol.pos, -LEFT_FRAC)

    def test_tilt_does_not_touch_odometry(self):
        self.f.manual("up", "r3", now=10.0)
        self.worker.finish(True)
        self.f._drain_results(11.0)
        self.assertEqual(self.patrol.pos, 0.0)
        self.assertEqual(self.replies[0][2], True)

    def test_edge_of_travel_closes_direction(self):
        self.f.manual("right", "r4", now=10.0)
        self.worker.finish(False, "{'code': -4000, 'message': 'right overflow'}")
        self.f._drain_results(11.0)
        self.assertEqual(self.patrol.pos, 0.0)
        self.assertGreaterEqual(self.f._blocked_until["right"], 11.0 + EDGE_BLOCK_SEC - 1)
        self.assertFalse(self.replies[0][2])
        self.assertIn("overflow", self.replies[0][3])

    def test_unknown_direction_rejected(self):
        self.assertFalse(self.f.manual("spin", "r5"))
        self.assertEqual(self.worker.commands, [])


class DetectorAcceptsCommand(unittest.TestCase):
    def _det(self, follow):
        det = Detector.__new__(Detector)
        det.follow = follow
        self.out = []
        det.on_event = lambda kind, msg, blob=None: self.out.append(msg)
        return det

    def test_without_ptz_says_so_honestly(self):
        det = self._det(None)
        det._handle("ptz_manual", {"id": "a", "dir": "left"})
        self.assertTrue(self.out[0]["no_ptz"])
        self.assertFalse(self.out[0]["ok"])

    def test_bad_direction(self):
        det = self._det(None)
        det._handle("ptz_manual", {"id": "a", "dir": "../etc"})
        self.assertFalse(self.out[0]["ok"])
        self.assertNotIn("no_ptz", self.out[0])

    def test_reply_comes_after_camera(self):
        worker = Worker()
        f = DogFollow(object(), 640, worker=worker, patrol=Patrol())
        det = self._det(f)
        f.on_manual = det._manual_reply
        det._handle("ptz_manual", {"id": "a", "dir": "right"})
        self.assertEqual(self.out, [], "the reply comes when the camera answers")
        worker.finish(True)
        det._poll_ptz()
        self.assertEqual(self.out, [{"cmd": "ptz_manual", "id": "a", "ok": True, "dir": "right"}])

    def test_overflow_marked_as_edge(self):
        worker = Worker()
        f = DogFollow(object(), 640, worker=worker, patrol=Patrol())
        det = self._det(f)
        f.on_manual = det._manual_reply
        det._handle("ptz_manual", {"id": "b", "dir": "left"})
        worker.finish(False, "left overflow")
        det._poll_ptz()
        self.assertEqual(self.out[0]["edge"], "left")


class DashboardRoute(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.dash = Dashboard(Config(db_path=self.dir / "events.db",
                                     snap_dir=self.dir / "snapshots"),
                              bus=MagicMock())
        self.asked = []
        self.reply = {"ok": True}

        async def ask(cmd, payload=None, timeout=6.0):
            self.asked.append((cmd, payload))
            return self.reply

        self.dash.ask = ask
        self.direct = MagicMock()
        self.dash._ptz = self.direct
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _alive(self):
        import time
        self.dash.latest = {"ts": time.time()}

    async def _post(self, d):
        r = await self.client.post("/api/ptz", json={"dir": d}, headers=GUARD)
        return r.status, await r.json() if r.content_type == "application/json" else None

    async def test_live_detector_turns_itself(self):
        self._alive()
        status, body = await self._post("right")
        self.assertEqual(status, 200)
        self.assertEqual(self.asked, [("ptz_manual", {"dir": "right"})])
        self.direct.rotate.assert_not_called()

    async def test_without_detector_directly(self):
        with self.assertLogs("pawsight.web", "WARNING") as logs:
            status, _ = await self._post("left")
        self.assertEqual(status, 200)
        self.assertEqual(self.asked, [])
        self.direct.rotate.assert_called_once_with("left")
        self.assertTrue(any("directly" in m for m in logs.output))

    async def test_detector_without_ptz_directly(self):
        self._alive()
        self.reply = {"ok": False, "no_ptz": True, "error": "detector has no PTZ"}
        status, _ = await self._post("left")
        self.assertEqual(status, 200)
        self.direct.rotate.assert_called_once_with("left")

    async def test_detector_silence_does_not_cause_double_pan(self):
        self._alive()
        self.reply = {"ok": False, "timeout": True, "error": "detector did not answer within 10 s"}
        status, body = await self._post("right")
        self.assertEqual(status, 504)
        self.direct.rotate.assert_not_called()

    async def test_edge_of_travel_in_plain_words(self):
        self._alive()
        self.reply = {"ok": False, "edge": "right", "error": "right overflow"}
        status, body = await self._post("right")
        self.assertEqual(status, 409)
        self.assertEqual(body["edge"], "right")
        self.assertIn("edge of travel", body["error"])

    async def test_bad_direction_400(self):
        status, _ = await self._post("spin")
        self.assertEqual(status, 400)
        self.assertEqual(self.asked, [])
        self.direct.rotate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
