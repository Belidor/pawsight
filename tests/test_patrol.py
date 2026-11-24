"""Patrol: home position on the bowls and sector search.

There is no hardware here: the camera and the scene are a model. The model is
deliberately physical, otherwise the test would check a stub rather than the
logic. In it the camera moves in asymmetric pulses (221 px right, 372 px left in
a 2304 px frame, calibration from docs/architecture.md), and the bowls and the fridge
are only visible when they fall within the frame.

The clock is fake too: time is passed into tick() from outside, so a sector
with 5-second pauses is checked instantly.
"""
from __future__ import annotations

import unittest

from pawsight.patrol import LEFT_FRAC, RIGHT_FRAC, Patrol, Scene

FRIDGE_AT = 0.30          # the fridge is 0.3 frame widths right of the bowls
IN_FRAME = 0.45           # further than this from the centre an object is out of frame


class World:
    """Scene and camera. The bowls are at point 0, the camera moves along the axis."""

    def __init__(self, pos: float = 0.0, limit: tuple[float, float] | None = None,
                 blind: bool = False, fridge: bool = True) -> None:
        self.pos = pos                # camera position in fractions of frame width
        self.limit = limit            # travel limits, if any
        self.blind = blind            # nothing is detected (dark/covered)
        self.has_fridge = fridge
        self.busy = False
        self.phantom = False
        self.moves: list[str] = []
        self.refused = 0

    # --- camera interface for the patrol ---
    def step(self, direction: str) -> bool:
        d = RIGHT_FRAC if direction == "right" else -LEFT_FRAC
        new = self.pos + d
        if self.limit and not (self.limit[0] <= new <= self.limit[1]):
            self.refused += 1
            # phantom=True is how a real edge behaves: the command is accepted
            # ("OK") but the camera did not move. That is what built up false odometry.
            return bool(self.phantom)
        self.pos = new
        self.moves.append(direction)
        return True

    # --- what the detector sees ---
    def _visible(self, at: float) -> float | None:
        off = at - self.pos
        return off if abs(off) <= IN_FRAME else None

    def scene(self, settled: bool = True) -> Scene:
        if self.blind:
            return Scene(settled=settled)
        seen = self._visible(FRIDGE_AT) if self.has_fridge else None
        return Scene(bowls=self._visible(0.0),
                     fridges=() if seen is None else (seen,),
                     settled=settled)

    @property
    def rights(self) -> int:
        return self.moves.count("right")

    @property
    def lefts(self) -> int:
        return self.moves.count("left")


class FakePtz:
    """Camera for checking the wiring: records commands and when they arrived."""

    def __init__(self) -> None:
        self.moves: list[str] = []
        self.last_ts = 0.0

    def rotate(self, direction: str) -> str:
        self.moves.append(direction)
        self.last_ts = _clock[0]
        return "ok"


_clock = [0.0]          # follow.py calls rotate() without a time, but we need one


def patrol(world: World, **kw) -> Patrol:
    opts = dict(idle_sec=180.0, step_right=3, step_left=2, steps=4,
                dwell_sec=5.0, dwell_frames=1, max_pulses=60, home_deadband=0.10)
    opts.update(kw)
    return Patrol(world, **opts)


def run(p: Patrol, w: World, t: float, seconds: float,
        dogs: bool = False, dt: float = 0.2, settled: bool = True) -> float:
    """Advance time, feeding the patrol frames at the detector rate (5 fps)."""
    end = t + seconds
    while t < end:
        if dogs:
            p.note_dog(t)
        else:
            p.tick(t, w.scene(settled=settled))
        t += dt
    return t


class ReturnHome(unittest.TestCase):
    def test_after_losing_dog_goes_home_instead_of_searching(self):
        w = World()
        p = patrol(w)
        for _ in range(5):                       # following took the camera to the right
            w.step("right")
            p.note_pulse("right")
        w.moves.clear()
        run(p, w, 0.0, 30.0)
        self.assertEqual(p.state, "home")
        self.assertLessEqual(abs(w.pos), 0.10)
        self.assertTrue(p.home_confirmed)
        self.assertEqual(w.rights, 0, "home is to the left, no reason to go right")

    def test_home_confirmed_by_bowls_not_by_counter(self):
        # The camera thinks it is home but is actually offset: the bowls will correct it.
        w = World(pos=0.35)
        p = patrol(w)
        p.pos = 0.0                               # odometry known to be lying
        p.pos_known = True
        run(p, w, 0.0, 30.0)
        self.assertLessEqual(abs(w.pos), 0.10)
        self.assertTrue(p.home_confirmed)

    def test_no_bowls_visible_home_not_confirmed_and_we_dont_lie(self):
        w = World(pos=0.29, blind=True)
        p = patrol(w)
        run(p, w, 0.0, 60.0)
        self.assertEqual(p.state, "home")
        self.assertFalse(p.home_confirmed)
        self.assertIn("not confirmed", p.status)

    def test_odometry_accounts_for_asymmetric_step(self):
        # Blind: 6 pulses to the right are NOT compensated by six to the left.
        w = World(blind=True)
        p = patrol(w)
        for _ in range(6):
            w.step("right")
            p.note_pulse("right")
        run(p, w, 0.0, 30.0)
        self.assertEqual(w.rights, 6)
        self.assertNotEqual(w.lefts, 6, "a symmetric return would miss by 68%")
        self.assertEqual(w.lefts, 3)
        self.assertLessEqual(abs(w.pos), 0.10)


class Timer(unittest.TestCase):
    def test_no_search_before_timeout(self):
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        run(p, w, 1.0, 178.0)
        self.assertEqual(p.state, "home")
        self.assertEqual(w.moves, [], "three minutes have not passed yet")

    def test_after_timeout_right_sector_starts(self):
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        run(p, w, 1.0, 185.0)
        self.assertEqual(p.state, "scan")
        self.assertGreater(w.rights, 0)
        self.assertEqual(w.lefts, 0)

    def test_timer_counts_from_dog_not_from_arriving_home(self):
        w = World()
        p = patrol(w)
        for _ in range(5):
            w.step("right")
            p.note_pulse("right")
        w.moves.clear()
        p.note_dog(0.0)
        t = run(p, w, 1.0, 177.0)          # part of the time went into returning
        self.assertEqual(p.state, "home")
        self.assertEqual(w.rights, 0)
        run(p, w, t, 6.0)
        self.assertEqual(p.state, "scan", "the return must not eat the three minutes")


class Sector(unittest.TestCase):
    def _to_sector_end(self, p: Patrol, w: World, t: float) -> float:
        p.note_dog(0.0)
        return run(p, w, t, 185.0 + 4 * 6.0)

    def test_exactly_as_many_stops_as_configured(self):
        w = World()
        p = patrol(w)
        self._to_sector_end(p, w, 1.0)
        self.assertEqual(w.rights, 12, "4 stops of 3 pulses")

    def test_step_and_stop_count_are_configurable(self):
        w = World()
        p = patrol(w, step_right=2, steps=3)
        p.note_dog(0.0)
        run(p, w, 1.0, 185.0 + 3 * 6.0)
        self.assertEqual(w.rights, 6)

    def test_pause_waits_for_fresh_frames_not_only_clock(self):
        # The stream froze after a pan (6-19 s without frames): no stepping blind.
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        run(p, w, 1.0, 186.0, settled=True)
        done = w.rights
        run(p, w, 187.0, 90.0, settled=False)
        self.assertLessEqual(w.rights - done, 3,
                             "without frames the current step may finish, but no more")
        self.assertLess(w.rights, 12, "and certainly not the whole sector blind")

    def test_dog_aborts_sector_immediately(self):
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 186.0)
        done = len(w.moves)
        self.assertGreater(done, 0)
        run(p, w, t, 30.0, dogs=True)
        self.assertEqual(len(w.moves), done, "with the dog in frame the patrol stays quiet")
        self.assertNotEqual(p.state, "scan")

    def test_right_then_home_then_left(self):
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        # Exactly to the end of the right sector: after that the return from the
        # LEFT sector goes in right pulses, and they must not be counted with the sector.
        # Plus ~12 s: the last return pulses, once the bowls are in frame, go one
        # at a time with a pause for a frame (PULSE_SETTLE_SEC), not as a burst.
        run(p, w, 1.0, 185.0 + 4 * 6.0 + 12.0)
        self.assertEqual(w.rights, 12, "right sector: exactly 12 pulses")
        self.assertGreaterEqual(p.home_visits, 1, "we pass through home between sectors")
        self.assertEqual(p.direction, "left")
        self.assertEqual(p.state, "scan")

    def test_after_both_sectors_cycle_rearms(self):
        w = World()
        p = patrol(w)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 400.0)
        self.assertEqual(p.state, "home", "both sectors done")
        before = len(w.moves)
        self.assertGreater(p.next_scan_at, t, "the next cycle is postponed")
        t = run(p, w, t, p.next_scan_at - t - 5.0)
        self.assertEqual(len(w.moves), before, "the new cycle waits its three minutes")
        run(p, w, t, 20.0)
        self.assertGreater(len(w.moves), before, "and then repeats")


class SafetyCutoffs(unittest.TestCase):
    def test_after_cutoff_return_waits_full_pause(self):
        w = World()
        p = patrol(w, max_pulses=2, release_sec=0)
        p.note_dog(0)
        t = 180.0
        for _ in range(100):
            p.tick(t, w.scene())
            t += .2
            if any("safety cutoff" in note for note in p.notes):
                break
        else:
            self.fail("pulse limit did not trigger")
        stopped = len(w.moves)
        deadline = p.next_scan_at
        self.assertGreater(abs(w.pos), p.home_deadband)
        while t < deadline:
            p.tick(t, w.scene())
            self.assertEqual(len(w.moves), stopped, "the return bypassed the safety cutoff")
            t += 1
        p.tick(deadline, w.scene())
        p.tick(deadline + .2, w.scene())
        self.assertGreater(len(w.moves), stopped, "after the pause the return must continue")

    def test_dog_does_not_reset_cutoff_pause(self):
        w = World(pos=.5)
        p = patrol(w, max_pulses=1, release_sec=0)
        p._cycle_pulses = 1
        p.tick(100, w.scene())
        p.note_dog(101)
        for t in (102, 110, 279):
            p.tick(t, w.scene())
        self.assertEqual(w.moves, [], "note_dog must not lift the patrol block")

    def test_edge_hit_ends_sector_and_does_not_jam(self):
        w = World(limit=(-2.0, 0.25))         # no travel to the right beyond 0.25
        p = patrol(w)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 186.0)
        self.assertGreater(w.refused, 0, "the camera hit the edge")
        p.note_edge("right")                   # this is how DogFollow reports it
        t = run(p, w, t, 20.0)
        self.assertEqual(p.direction, "left", "after the edge we go the other way")
        # There will be more right pulses: the return from the LEFT sector uses them.
        # We check not their count but that the camera no longer hits the wall.
        refusals = w.refused
        run(p, w, t, 90.0)
        self.assertEqual(w.refused, refusals, "no more pushing into the edge")

    def test_per_cycle_pulse_cutoff(self):
        w = World()
        p = patrol(w, max_pulses=5, steps=99)
        p.note_dog(0.0)
        run(p, w, 1.0, 200.0)
        self.assertLessEqual(w.rights, 6, "the safety cutoff must stop the motor")
        self.assertIn("safety cutoff", p.status.lower() + " " + " ".join(p.notes))


class Fridge(unittest.TestCase):
    def test_offset_learned_at_home_and_helps_return(self):
        w = World()
        p = patrol(w)
        run(p, w, 0.0, 2.0)                     # stood at home, learned the anchor
        self.assertIsNotNone(p.fridge_abs)
        self.assertAlmostEqual(p.fridge_abs, FRIDGE_AT, places=2)

    def test_fridge_fixes_odometry_when_bowls_not_visible(self):
        w = World()
        p = patrol(w)
        run(p, w, 0.0, 2.0)                     # learned
        w.pos = 0.55                            # moved away: the bowls are no longer visible
        p.pos, p.pos_known = 0.30, True         # odometry is off by 0.25 width
        self.assertIsNone(w.scene().bowls, "the bowls really are not visible")
        p.tick(3.0, w.scene())
        self.assertAlmostEqual(p.pos, 0.55, places=2, msg="position taken from the fridge")

    def test_false_fridge_rejected(self):
        # Measured on the dataset: YOLO produced a second "fridge" 0.69 widths
        # from the real one, and it was more confident. Accepting it would steer the camera away silently.
        w = World()
        p = patrol(w)
        run(p, w, 0.0, 2.0)                     # learned the real one
        self.assertAlmostEqual(p.fridge_abs, FRIDGE_AT, places=2)
        p.pos, p.pos_known = 0.0, True
        p.tick(3.0, Scene(bowls=None, fridges=(0.99, FRIDGE_AT), settled=True))
        self.assertAlmostEqual(p.pos, 0.0, places=2, msg="the real one was taken, not the false one")
        p.tick(3.2, Scene(bowls=None, fridges=(0.99,), settled=True))
        self.assertAlmostEqual(p.pos, 0.0, places=2,
                               msg="a lone false candidate does not move the position")

    def test_nothing_learned_from_two_candidates(self):
        w = World()
        p = patrol(w)
        p.tick(1.0, Scene(bowls=0.0, fridges=(0.30, 0.90), settled=True))
        self.assertIsNone(p.fridge_abs,
                          "there is no way to pick the right one of two \"fridges\"")

    def test_everything_works_without_fridge(self):
        # The fine-tuned three-class model does not know about fridges at all.
        w = World(fridge=False)
        p = patrol(w)
        for _ in range(5):
            w.step("right")
            p.note_pulse("right")
        run(p, w, 0.0, 30.0)
        self.assertEqual(p.state, "home")
        self.assertIsNone(p.fridge_abs)
        self.assertLessEqual(abs(w.pos), 0.10)


class Disabled(unittest.TestCase):
    def test_disabled_patrol_does_not_touch_motor(self):
        w = World(pos=0.48)
        p = patrol(w, enabled=False)
        p.note_dog(0.0)
        run(p, w, 1.0, 400.0)
        self.assertEqual(w.moves, [])


if __name__ == "__main__":
    unittest.main()


class WiringWithFollow(unittest.TestCase):
    """Patrol and following are separate modules, but they share one motor.

    This checks exactly the seam: who hands control to whom, whether following
    steps reach the patrol odometry, and whether patrol commands reach the camera.
    """

    def setUp(self):
        from pawsight.follow import DogFollow

        self.ptz = FakePtz()
        self.p = Patrol(idle_sec=180.0, step_right=3, step_left=2, steps=4,
                        dwell_sec=5.0, dwell_frames=1, max_pulses=60)
        self.follow = DogFollow(self.ptz, frame_w=640, enabled=True,
                                worker=None, patrol=self.p)

    def _advance(self, seconds: float, t: float, dogs=None, dt: float = 0.2) -> float:
        end = t + seconds
        while t < end:
            # "Camera moving" for a couple of ticks after each command: otherwise
            # follow.py decides it did not move and declares a false edge hit.
            moving = bool(self.ptz.moves) and (t - self.ptz.last_ts) < 0.5
            self.follow.tick(dogs or [], moving, t, Scene(bowls=0.0, settled=True))
            t += dt
        return t

    def test_patrol_motor_reaches_camera(self):
        self.assertIsNotNone(self.p.cam, "DogFollow must supply the motor")
        self._advance(190.0, 1.0)
        self.assertGreater(self.ptz.moves.count("right"), 0, "the sector went right")
        self.assertEqual(self.p.state, "scan")

    def test_dog_in_frame_stops_patrol(self):
        dog = [{"box": [300, 100, 340, 200]}]      # exactly at the centre of a 640 frame
        self._advance(200.0, 1.0, dogs=dog)
        self.assertEqual(self.ptz.moves, [], "no reason to pan at the frame centre")
        self.assertNotEqual(self.p.state, "scan")

    def test_follow_steps_reach_patrol_odometry(self):
        dog = [{"box": [580, 100, 620, 200]}]      # far right -> step right
        self.follow.tick(dog, False, 1.0, Scene(bowls=0.0, settled=True))
        self.assertEqual(self.ptz.moves, ["right"], "exactly one step per tick")
        self.assertAlmostEqual(self.p.pos, RIGHT_FRAC, places=4,
                               msg="following takes the camera away from home; the patrol must know")

    def test_manual_return_gets_motor_immediately_even_with_dog(self):
        """The "to the bowls" button is a command, not a wait for the ten-second pause."""
        self.p.pos = 0.40
        self.follow.set_enabled(False)                 # the button does not depend on the toggle
        self.assertTrue(self.follow.request_home(10.0))
        dog_right = [{"box": [580, 100, 620, 200]}]

        self.follow.tick(dog_right, False, 10.0, Scene(settled=True))

        self.assertEqual(self.ptz.moves, ["left"],
                         "the return must beat following and head home")
        self.assertGreaterEqual(self.p.next_scan_at, 190.0,
                                "after a manual return a new search waits the full timeout")

    def test_manual_return_cancels_unfinished_sector(self):
        self.p.state = "scan"
        self.p._queue = ["left"]
        self.p._pending = 2

        self.assertTrue(self.follow.request_home(20.0))

        self.assertEqual(self.p.state, "returning")
        self.assertEqual(self.p._queue, [])
        self.assertEqual(self.p._pending, 0)


class FlickeringFalseDog(unittest.TestCase):
    """A case from the live camera (2026-09-06, 18:14-18:20).

    The three-class model confidently took an office chair for a dog (conf 0.35
    and 0.51), and the "dog" flickered: in frame, then not. On frames with it
    following pulled the camera LEFT, on frames without it the patrol pulled it
    RIGHT, towards home. The two loops drove the camera against each other: 554
    pans in 40 minutes. Every left following step honestly reached the
    odometry, and the return never completed: the offset drifted to -7.21 widths,
    and the safety cutoff stayed silent because it only counted scan pulses.
    """

    def test_flickering_dog_does_not_drive_motor(self):
        w = World(blind=True)
        p = patrol(w)
        t = 1.0
        # The dog flickers once every three seconds, just like false detections.
        for i in range(1500):
            if i % 15 == 0:
                p.note_dog(t)
                p.note_pulse("left")        # following stepped after the "dog"
            else:
                p.tick(t, w.scene())
            t += 0.2
        self.assertEqual(w.moves, [],
                         f"the patrol made {len(w.moves)} pulses fighting following")

    def test_offset_does_not_run_away(self):
        w = World(blind=True)
        p = patrol(w)
        p.note_dog(0.0)
        # Following chases a false dog to the left for a long time, as in production.
        for _ in range(45):
            p.note_pulse("left")
        p.tick(1.0, w.scene())
        self.assertLessEqual(abs(p.pos), 3.0,
                             f"odometry ran away to {p.pos:+.2f} widths, as happened in production")
        self.assertFalse(p.pos_known, "lost odometry must not be treated as known")

    def test_cutoff_counts_return_too(self):
        # Home is unreachable: the camera is at the right edge, no travel to the right.
        w = World(pos=-0.5, limit=(-10.0, -0.5), blind=True)
        p = patrol(w, max_pulses=12)
        p.note_dog(0.0)
        t = run(p, w, 1.0, 400.0)
        if w.refused:
            p.note_edge("right")
        run(p, w, t, 200.0)
        self.assertIn("safety cutoff", " ".join(p.notes).lower(),
                      "the safety cutoff must cover the return too, not just the sector")
