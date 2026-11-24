"""The brain does not hoard state for tracks that are long gone.

BoT-SORT hands out new track_ids all the time, and each track in the brain
has a history, up to 12 JPEGs and debounce marks. Without cleanup that is
unbounded memory growth in a process that lives for weeks.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pawsight import brain as brain_mod


class FakeStore:
    def __init__(self):
        self.added, self.bouts = [], []

    def add(self, *a):
        self.added.append(a)

    def add_bout(self, *a, **kw):
        self.bouts.append((a, kw))


class FakeTg:
    def __init__(self):
        self.sent = []

    def send(self, text, jpeg=None, key=""):
        self.sent.append(text)


class FakeVLM:
    def look(self, jpegs):
        return "dog", "lying"

    def classify_bout(self, jpegs):
        return "drink", "DRINKING"


def _obs(ts, tids, zones=()):
    return {"ts": ts, "camera_moving": False, "settled": True,
            "dogs": [{"track_id": t, "box": [10, 10, 50, 50], "conf": 0.9,
                      "zones": list(zones)} for t in tids]}


class ForgetOldTracks(unittest.TestCase):
    def setUp(self):
        from pawsight.config import Config

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = Config()
        cfg.db_path = Path(self.tmp.name) / "events.db"
        cfg.snap_dir = Path(self.tmp.name)
        self.b = brain_mod.Brain(cfg, async_reports=False)
        self.b.store, self.b.tg, self.b.vlm = FakeStore(), FakeTg(), FakeVLM()

    def _dicts(self):
        b = self.b
        return (b.last_seen, b.hist, b.frames, b.committed, b.pending, b.zone_since)

    def test_hundred_flickering_tracks_do_not_stay_in_memory(self):
        t0 = 1_000_000.0
        for i in range(100):
            self.b.observe(_obs(t0 + i * 0.2, [i]), b"jpeg")
        self.assertEqual(len(self.b.frames), 100)
        # after the TTL only one new track is visible
        later = t0 + 20 + brain_mod.TRACK_TTL_SEC + brain_mod.PRUNE_EVERY_SEC + 1
        self.b.observe(_obs(later, [999]), b"jpeg")
        for d in self._dicts():
            self.assertLessEqual(set(d), {999}, d)

    def test_live_track_is_left_alone(self):
        t0 = 1_000_000.0
        for k in range(int(3 * brain_mod.TRACK_TTL_SEC)):
            self.b.observe(_obs(t0 + k, [5]), b"jpeg")
        self.assertIn(5, self.b.frames)
        self.assertIn(5, self.b.committed)

    def test_cleanup_does_not_cut_an_open_visit(self):
        """A visit is opened by track 1; track 1 goes stale and is forgotten,
        but track 2 continues the visit, and duration counts from the start."""
        t0 = 1_000_000.0
        self.b.observe(_obs(t0, [1], ["bowls", "water"]), b"a")
        # track 1 is gone, track 2 appears at the bowls right away and stays long
        t = t0
        while t < t0 + brain_mod.TRACK_TTL_SEC + 30:
            t += 1.0
            self.b.observe(_obs(t, [2], ["bowls", "water"]), b"b")
        self.assertNotIn(1, self.b.last_seen)
        bout = self.b._open_bouts["bowls"]
        self.assertEqual(bout["start"], t0)
        # the dog leaves: the visit closes as one record for the whole duration
        self.b.observe(_obs(t + brain_mod.BOUT_GAP_SEC + 1, []))
        self.assertEqual(len(self.b.store.bouts), 1)
        (zone, start, last, tid), kw = self.b.store.bouts[0]
        self.assertEqual(start, t0)
        self.assertEqual(kw["kind"], "drink")

    def test_cleanup_does_not_cut_debounce(self):
        """The debounce (pending) of a visible track survives cleanup."""
        t0 = 1_000_000.0
        self.b.observe(_obs(t0, [3]), b"x")
        self.b._next_prune = 0.0           # cleanup on every frame

        self.b.observe(_obs(t0 + 1, [3]), b"x")
        self.assertIn(3, self.b.pending)


if __name__ == "__main__":
    unittest.main()
