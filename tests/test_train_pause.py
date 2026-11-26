"""The "pause detection during training" checkbox works.

pause_detection used to be only written to train.lock and never read: the
person ticked the box, and the detector kept occupying Metal.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pawsight import hoststat, trainer
from pawsight.config import Config
from pawsight.detector import Detector


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.rc = None

    def poll(self):
        return self.rc


class _TmpLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = trainer.LOCK
        trainer.LOCK = Path(self.tmp.name) / "train.lock"
        self.addCleanup(lambda: setattr(trainer, "LOCK", old))
        cfg = Config(db_path=Path(self.tmp.name) / "events.db",
                     snap_dir=Path(self.tmp.name) / "s")
        cfg.dataset_dir = Path(self.tmp.name) / "dataset"
        self.cfg = cfg

    def _started(self, pause: bool) -> tuple[trainer.Trainer, FakeProc]:
        t = trainer.Trainer(self.cfg)
        proc = FakeProc()
        with patch.object(trainer.Trainer, "check", return_value=None), \
                patch.object(trainer.subprocess, "Popen", return_value=proc), \
                patch.object(trainer.Trainer, "_tail"):
            r = t.start(pause_detection=pause)
        self.assertTrue(r["ok"], r)
        return t, proc


class TrainerKnowsAboutPause(_TmpLock):
    def test_no_checkbox_no_pause(self):
        t, _ = self._started(pause=False)
        self.assertFalse(t.detection_paused())

    def test_checkbox_pauses_while_run_is_alive(self):
        t, proc = self._started(pause=True)
        self.assertTrue(t.detection_paused())
        self.assertTrue(json.loads(trainer.LOCK.read_text())["paused"])
        proc.rc = 0                         # the run finished on its own
        self.assertFalse(t.detection_paused())

    def test_stop_lifts_the_pause(self):
        t, _ = self._started(pause=True)
        with patch.object(trainer, "read_lock", return_value={"pid": 4242}), \
                patch.object(trainer.procutil, "terminate") as term:
            self.assertTrue(t.stop()["ok"])
        term.assert_called_once_with(4242)
        self.assertFalse(t.detection_paused())

    def test_detector_restart_remembers_the_pause(self):
        trainer.LOCK.write_text(json.dumps({"pid": 777, "paused": True, "started": 0}))
        with patch.object(trainer, "_alive", return_value=True), \
                patch.object(trainer.procutil, "pid_alive", return_value=True) as alive:
            t = trainer.Trainer(self.cfg)
            self.assertTrue(t.detection_paused())
            alive.return_value = False      # the run is gone
            self.assertFalse(t.detection_paused())


class DetectorListensToTrainer(unittest.TestCase):
    def _det(self, paused: bool):
        det = Detector.__new__(Detector)
        det.trainer = SimpleNamespace(detection_paused=lambda: self.paused)
        self.paused = paused
        det.paused_detection = False
        det._pause_checked = 0.0
        det._ring = __import__("collections").deque(maxlen=6)
        det._snap_req = None
        self.changed = 0
        det.on_changed = lambda: setattr(self, "changed", self.changed + 1)
        return det

    def test_pause_and_resume(self):
        det = self._det(True)
        self.assertTrue(det._detection_paused(100.0))
        self.assertTrue(det.paused_detection)
        self.assertEqual(self.changed, 1, "the status must learn about the pause right away")
        self.paused = False
        self.assertTrue(det._detection_paused(100.5), "the trainer is polled once per second")
        self.assertFalse(det._detection_paused(101.1))
        self.assertEqual(self.changed, 2)

    def test_no_trainer_no_pause(self):
        det = self._det(False)
        det.trainer = None
        self.assertFalse(det._detection_paused(5.0))

    def test_paused_frame_goes_into_the_ring(self):
        det = self._det(True)
        det._idle_frame("frame", 10.0)
        self.assertEqual(list(det._ring), [(10.0, "frame", [])])

    def test_loop_does_not_call_model_when_paused(self):
        src = (Path(__file__).resolve().parent.parent / "pawsight" / "detector.py").read_text()
        run = src[src.index("    def run(self) -> None:"):]
        self.assertLess(run.index("self._detection_paused(now)"), run.index("model.track("))


class StatusReportsPause(unittest.TestCase):
    def test_collect(self):
        det = SimpleNamespace(paused_detection=True, fps=0.0, _started=0.0,
                              _names={}, classes=[])
        cfg = Config()
        with patch.object(hoststat, "dataset_stats", return_value={}), \
                patch.object(hoststat, "_dir_bytes", return_value=0), \
                patch.object(hoststat, "_sha12", return_value=""):
            st = hoststat.collect(cfg, det)
        self.assertTrue(st["detector"]["paused"])


class DashboardSeesPause(unittest.IsolatedAsyncioTestCase):
    async def test_api_state(self):
        import shutil
        from aiohttp.test_utils import TestClient, TestServer
        from pawsight.web import Dashboard

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        dash = Dashboard(Config(db_path=d / "events.db", snap_dir=d / "s"))
        client = TestClient(TestServer(dash.app()))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.assertFalse((await (await client.get("/api/state")).json())["detector_paused"])
        dash.on_status({"detector": {"paused": True}})
        self.assertTrue((await (await client.get("/api/state")).json())["detector_paused"])
        js = await (await client.get("/static/dashboard.js")).text()
        self.assertIn("s.detector_paused", js)


if __name__ == "__main__":
    unittest.main()
