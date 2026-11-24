"""Home beyond the edge of travel: the bowls are visible, but the camera answers overflow.

A case from the live camera (2026-09-07, 19:18-20:12). The "right overflow" limit
is a firmware software counter, and it wanders relative to the scene: at the
same limit the bowls were at +0.30 and also at the frame centre (docs/architecture.md).
So home is reachable, but not every minute, and the patrol must TRY again:
only less often, honestly, and without declaring the bowl-measured odometry
lost. Before the fix it hit the limit every minute (88 overflows in two hours)
and logged "odometry lost" and "no bowls in frame" with the bowls in frame.
"""
from __future__ import annotations

import unittest

from pawsight.follow import DogFollow
from pawsight.patrol import GIVE_UP_RETRY_MAX, GIVE_UP_RETRY_SEC, LEFT_FRAC, RIGHT_FRAC, Patrol
from tests.test_follow import Worker
from tests.test_patrol import World, patrol, run

EDGE = -0.30          # camera at the right limit: bowls visible at +0.30


class EdgeWorld(World):
    """Like the real wiring: the camera's refusal (overflow) reaches the patrol
    via DogFollow._hit_edge -> Patrol.note_edge."""

    patrol = None

    def step(self, direction: str) -> bool:
        before = self.refused
        ok = super().step(direction)
        if self.refused > before and self.patrol is not None:
            self.patrol.note_edge(direction)
        return ok


def edge_world(**kw):
    w = EdgeWorld(pos=EDGE, limit=(-2.0, EDGE), fridge=False)
    p = patrol(w, **kw)
    w.patrol = p
    return w, p


class HomeBeyondLimit(unittest.TestCase):
    def test_bowl_odometry_not_declared_lost(self):
        w, p = edge_world()
        p.note_dog(0.0)                       # the dog has just left
        with self.assertLogs("pawsight.patrol", level="INFO") as cm:
            run(p, w, 1.0, 20.0)
        self.assertEqual(w.refused, 1)
        self.assertEqual(p.state, "home")
        self.assertAlmostEqual(p.pos, EDGE, places=2,
                               msg="the position is measured by the bowls; it must not be zeroed")
        self.assertNotIn("odometry lost", " ".join(p.notes))
        self.assertTrue(p.home_confirmed, "bowls in frame: home confirmed, even at the limit")
        self.assertIn("at the edge of travel", p.status)
        self.assertIn("retry in", p.status)
        text = "\n".join(cm.output)
        self.assertNotIn("no bowls in frame", text, "the log lied: the bowls were in frame")
        self.assertIn("edge of travel", text)

    def test_retries_get_sparser_not_every_30_seconds(self):
        w, p = edge_world(idle_sec=10_000)     # no patrol cycles: pure retries
        p.note_dog(0.0)
        run(p, w, 1.0, 900.0)
        # 30 -> 60 -> 120 -> 240 -> 300 -> 300: no more than six attempts in 15 minutes
        self.assertLessEqual(w.refused, 6, f"hitting the limit too often: {w.refused} times")
        self.assertGreaterEqual(w.refused, 3, "but we must keep trying: the limit drifts")
        self.assertEqual(p.state, "home")
        self.assertNotIn("odometry lost", " ".join(p.notes))

    def test_limit_moved_return_reaches_home(self):
        # A few minutes later the firmware "released" the limit: the next attempt
        # must bring the camera to the bowls, and the failure counter must reset.
        w, p = edge_world(idle_sec=10_000)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 100.0)
        self.assertGreaterEqual(w.refused, 2)
        w.limit = (-2.0, 0.5)                 # the limit moved
        t = run(p, w, t, 400.0)
        self.assertLessEqual(abs(p.pos), p.home_deadband, "home reached")
        self.assertIn("bowls in frame", p.status)
        self.assertNotIn("edge", p.status)

    def test_moving_away_from_limit_retries_immediately(self):
        w, p = edge_world(idle_sec=10_000)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 400.0)              # the retry pause has grown to hundreds of seconds
        refused = w.refused
        for _ in range(2):                     # following took the camera left after the dog
            w.step("left")
            p.note_pulse("left")
        p.note_dog(t)
        t = run(p, w, t, 30.0)
        self.assertGreaterEqual(w.rights, 2, "the camera moved: the return goes without waiting for the pause")
        self.assertEqual(w.refused, refused + 1, "reached the limit and stopped")
        self.assertEqual(p.state, "home")

    def test_to_bowls_button_always_makes_an_attempt(self):
        w, p = edge_world(idle_sec=10_000)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 400.0)
        refused = w.refused
        p.request_home(t)
        run(p, w, t, 15.0)                     # 10 s: the "dog nearby" pause after the command
        self.assertEqual(w.refused, refused + 1, "a manual return tries instead of citing the pause")
        self.assertFalse(p.return_requested)
        self.assertIn("at the edge of travel", p.status)

    def test_edge_without_bowls_in_frame_still_resets_odometry(self):
        # The counter lies and no bowls are visible: admit it out loud, as before.
        w = EdgeWorld(pos=-0.5, limit=(-10.0, -0.5), blind=True)
        p = patrol(w)
        w.patrol = p
        p.pos = -1.0                          # the counter thinks we are far to the left
        p.note_dog(0.0)
        run(p, w, 1.0, 60.0)
        self.assertIn("odometry lost", " ".join(p.notes))


class LaggyWorld(World):
    """Like the real camera: the command is accepted at once, but the shift is
    visible in the frame only after LAG seconds. While the frame is old, bowl
    odometry returns the previous position."""

    LAG = 1.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.now = 0.0
        self._queue: list[tuple[float, float]] = []

    def step(self, direction: str) -> bool:
        d = RIGHT_FRAC if direction == "right" else -LEFT_FRAC
        self.moves.append(direction)
        self._queue.append((self.now + self.LAG, d))
        return True

    def scene(self, settled: bool = True):
        due = [d for at, d in self._queue if at <= self.now]
        self._queue = [(at, d) for at, d in self._queue if at > self.now]
        self.pos += sum(due)
        return super().scene(settled=settled)


class ReturnOnStaleFrame(unittest.TestCase):
    def test_near_home_one_pulse_and_a_look_not_a_burst(self):
        # Case 20:24:36-37: bowls at -0.12, three lefts in 1.3 s from the same
        # frame, the camera went 0.3-0.48 to the left and swung back.
        w = LaggyWorld(pos=0.12, fridge=False)
        p = patrol(w)
        p.note_dog(0.0)
        t = 1.0
        while t < 40.0:
            w.now = t
            p.tick(t, w.scene())
            t += 0.2
        self.assertEqual(w.moves, ["left"], f"instead of one pulse: {w.moves}")
        self.assertLessEqual(abs(p.pos), p.home_deadband)
        self.assertEqual(p.state, "home")

    def test_blind_part_of_the_way_still_a_burst(self):
        w = LaggyWorld(pos=1.2, fridge=False)      # bowls far out of frame
        p = patrol(w)
        p.pos = 1.2                                 # what the odometry counted from following steps
        p.note_dog(0.0)
        t = 1.0
        first, last = None, None
        while t < 15.0:
            w.now = t
            p.tick(t, w.scene())
            if w.moves and first is None:
                first = t
            if len(w.moves) >= 4 and last is None:
                last = t
            t += 0.2
        self.assertIsNotNone(last, "fewer than four pulses in 14 s")
        self.assertLess(last - first, 2.0, "while the bowls are not visible, the return goes as a burst")


class RejectedPulse(unittest.TestCase):
    """The patrol moves its odometry when it sends a command; if the camera
    answered overflow, the camera did not move, and the shift must be rolled back."""

    def test_overflow_rolls_back_odometry_shift(self):
        worker = Worker()
        p = Patrol()
        follow = DogFollow(object(), 640, worker=worker, patrol=p)
        p.pos = -0.30
        self.assertTrue(p._pulse("right", 1.0))
        self.assertAlmostEqual(p.pos, -0.30 + RIGHT_FRAC, places=4, msg="shift counted when sent")
        worker.finish(False, "right overflow")
        follow.tick([], False, 1.0)
        self.assertAlmostEqual(p.pos, -0.30, places=4, msg="the camera did not move: no shift")
        self.assertEqual(p._edge, "right")

    def test_pause_constants_consistent(self):
        self.assertLess(GIVE_UP_RETRY_SEC, GIVE_UP_RETRY_MAX)


if __name__ == "__main__":
    unittest.main()
