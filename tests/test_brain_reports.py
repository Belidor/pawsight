"""A slow VLM does not stall the brain.

Ollama takes up to 90 s to answer. While Brain called it straight from
observe(), the brain's state stood still and the observation queue (512
frames, ~100 s) approached its limit. Now the VLM, database and Telegram run
on the report thread, in event order.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from pawsight import brain as brain_mod


class SlowVLM:
    def __init__(self):
        self.gate = threading.Event()
        self.calls = []

    def look(self, jpegs):
        self.calls.append("look")
        self.gate.wait(5)
        return "dog", "lying"

    def classify_bout(self, jpegs):
        self.calls.append("bout")
        self.gate.wait(5)
        return "drink", "DRINKING"


class FakeStore:
    def __init__(self):
        self.order = []

    def add(self, now, tid, *a):
        self.order.append(("event", now, tid))

    def add_bout(self, zone, start, last, tid, **kw):
        self.order.append(("bout", start, kw["kind"]))


class FakeTg:
    def __init__(self):
        self.sent = []

    def send(self, text, jpeg=None, key=""):
        self.sent.append(text)


def _obs(ts, tid, zones):
    return {"ts": ts, "settled": True,
            "dogs": [{"track_id": tid, "box": [10 + ts % 7, 10, 50, 50], "conf": 0.9,
                      "zones": zones}]}


class ReportsOnSeparateThread(unittest.TestCase):
    def setUp(self):
        from pawsight.config import Config

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Config()
        cfg.db_path = Path(self.tmp.name) / "events.db"
        cfg.snap_dir = Path(self.tmp.name)
        self.b = brain_mod.Brain(cfg)
        self.addCleanup(lambda: (self.vlm.gate.set(), self.b.close(2)))
        self.vlm = SlowVLM()
        self.b.vlm, self.b.store, self.b.tg = self.vlm, FakeStore(), FakeTg()

    def _feed(self, t0, seconds, tid, zones):
        t = t0
        while t < t0 + seconds:
            self.b.observe(_obs(t, tid, zones), b"jpeg")
            t += 0.2
        return t

    def test_observe_does_not_wait_for_vlm(self):
        t0 = 1_000_000.0
        submitted = []
        real_report = self.b._report

        def spy(job):
            submitted.append(job)
            real_report(job)
        self.b._report = spy
        started = time.monotonic()
        # a visit to the bowls, then leaving: closing the visit calls the VLM
        t = self._feed(t0, 6, 1, ["bowls", "water"])
        t = self._feed(t + 0.2, brain_mod.BOUT_GAP_SEC + 2, 1, [])
        self.assertLess(time.monotonic() - started, 2.0,
                        "observe() waited for the VLM: the observation thread stalled")
        deadline = time.monotonic() + 2
        while not self.vlm.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.vlm.calls, "the VLM was never called")
        self.assertEqual(self.b.store.order, [], "the report went out before the VLM answered")
        # the brain's state kept updating while the VLM was thinking
        self.assertNotIn("bowls", self.b._open_bouts)

        self.vlm.gate.set()
        self.b.close(timeout=5)
        kinds = [x[0] for x in self.b.store.order]
        self.assertIn("bout", kinds)
        self.assertEqual(len(self.b.store.order), len(submitted),
                         "every submitted report is written")

    def test_reports_run_in_order(self):
        w = brain_mod.ReportWorker(max_pending=64)
        got = []
        for i in range(50):
            self.assertTrue(w.submit(lambda use_vlm, i=i: got.append(i)))
        w.close(timeout=5)
        self.assertEqual(got, list(range(50)))

    def test_full_queue_writes_event_without_vlm(self):
        self.b._reports.close(1)
        self.b._reports = brain_mod.ReportWorker(max_pending=1)
        busy = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        self.b._reports.submit(lambda use_vlm: (busy.set(), release.wait(5)))
        self.assertTrue(busy.wait(2))                  # the thread is busy with the first report
        self.b._reports.submit(lambda use_vlm: None)   # and the queue is full
        self.b._emit(5, None, "lying", 1_000_000.0)
        self.assertEqual(self.b.store.order, [("event", 1_000_000.0, 5)])
        self.assertEqual(self.b.tg.sent, [], "no VLM, no Telegram")

        self.assertEqual(self.vlm.calls, [])

if __name__ == "__main__":
    unittest.main()
