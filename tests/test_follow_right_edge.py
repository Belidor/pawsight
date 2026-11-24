"""Dog beyond the right edge of travel: the camera must not freeze.

A case from the live camera, 2026-09-08. From 00:30 to 06:00 the dog stayed at
+0.41 widths near the right frame edge, following asked for a `right` step once
a minute, the camera answered `overflow`, the direction was closed for 60 s, and
so on for 5.5 hours without a single successful pulse. The patrol never got the
camera: the visible dog refreshed its idle timer every few seconds. 587 rejected
commands in a day.

The same log shows that the edge of travel is a firmware counter, not a hard stop:

    06:00:35 patrol: edge of travel right, sector ended
    06:00:35..52 pan left -> ['OK']   (eight pulses)
    06:00:57..06:01:02 pan right -> ['OK']  (eight pulses went through)
    06:01:03 patrol: edge of travel right (overflow)   (the ninth refused again)

Moving left restores travel to the right one for one. So standing at the limit
is the worst thing to do: search towards the free side, which also gives the
camera back the ability to follow the dog to the right again.
"""
from __future__ import annotations

import unittest

from pawsight.follow import DEADBAND, DogFollow, LOST_GRACE_SEC
from pawsight.patrol import Patrol, Scene
from tests.test_follow import Worker, dog


class RightLimit(unittest.TestCase):
    def setUp(self):
        self.worker = Worker()
        self.patrol = Patrol()
        self.follow = DogFollow(object(), 640, worker=self.worker, patrol=self.patrol)

    def _follow_cmds(self) -> list:
        return [c for c in self.worker.commands if c[1] == "follow"]

    def _patrol_cmds(self) -> list:
        return [c for c in self.worker.commands if c[1] == "patrol"]

    def test_no_knocking_on_closed_side_every_minute(self):
        self.follow.tick(dog(620), False, 100)              # +0.47 -> right step
        self.assertEqual(self._follow_cmds(), [("right", "follow")])
        self.worker.finish(False, "overflow")               # the firmware refused
        t = 104.0
        for _ in range(4):                                  # four minutes in a row
            self.follow.tick(dog(620), False, t, Scene(settled=True))
            t += 61
        self.assertEqual(self._follow_cmds(), [("right", "follow")],
                         "587 rejected commands a day are not attempts, they are knocking")

    def test_visible_dog_beyond_limit_hands_camera_to_search(self):
        self.follow._hit_edge("right", 90, "overflow")      # right travel closed
        self.patrol.pos = 0.30                              # camera right of home: home is to the left
        t = 100.0
        for _ in range(8):
            self.follow.tick(dog(620), False, t, Scene(settled=True))
            t += 5
        self.assertEqual(self._follow_cmds(), [], "following does not step towards the closed side")
        self.assertEqual(self._patrol_cmds(), [("left", "patrol")],
                         "the camera must search, not stand at the limit for 5.5 hours")
        self.assertIn("edge of travel", self.follow.state()["status"])

    def test_reachable_dog_keeps_camera_from_search(self):
        self.follow._hit_edge("right", 90, "overflow")      # right travel closed
        self.patrol.pos = 0.30
        t = 100.0
        for _ in range(8):
            self.follow.tick(dog(190), False, t, Scene(settled=True))   # dog on the LEFT
            if self.worker.busy:
                self.worker.finish()
            t += 5
        self.assertEqual(self._patrol_cmds(), [],
                         "left travel is free: following works, the patrol does not get the camera")
        self.assertIn(("left", "follow"), self.worker.commands)

    def test_dog_near_centre_with_closed_edge_not_unreachable(self):
        self.follow._hit_edge("right", 90, "overflow")
        self.patrol.pos = 0.30
        off = (dog(340)[0]["box"][0] + dog(340)[0]["box"][2]) / 2 / 640 - .5
        self.assertLess(abs(off), DEADBAND, "the dog must be inside the dead zone")
        for t in (100.0, 105.0, 110.0, 115.0, 120.0):
            self.follow.tick(dog(340), False, t, Scene(settled=True))
        self.assertEqual(self._patrol_cmds(), [],
                         "dog in the dead zone: nowhere to pan, but no reason to leave either")

    def test_rejected_step_does_not_stay_in_dead_reckoning(self):
        # Log 2026-09-08 23:49:49 "edge of travel right (overflow)", and at 23:49:52
        # "not chasing: estimated already at the centre (+0.06)": the rejected step
        # stayed in dead reckoning although it never moved the camera.
        self.follow.tick(dog(422), False, 100)              # +0.16 -> right step
        self.assertEqual(self._follow_cmds(), [("right", "follow")])
        self.worker.finish(False, "overflow")
        self.follow.tick(dog(422), False, 100.4)            # dog in the same place: the camera did not move
        side, steps, why = self.follow._plan_chase(101)
        self.assertEqual(side, "right")
        self.assertGreater(steps, 0, f"dog at +0.16, but chase refused: {why}")

    def test_single_missed_frame_is_not_a_loss(self):
        # 05:50:05..05:50:30: eight "dog vanished ... 0.2 s ago" lines in twenty
        # seconds: at the frame edge detection flickers from frame to frame.
        self.follow._blocked_until["right"] = 500           # nowhere to step: we look at the plan
        self.follow.tick(dog(620), False, 100)
        self.follow.tick([], False, 100 + LOST_GRACE_SEC / 2)
        self.assertFalse(self.follow._chase_planned, "one frame without the dog is not a loss yet")
        self.follow.tick([], False, 100 + LOST_GRACE_SEC + .5)
        self.assertTrue(self.follow._chase_planned, "but now the dog really is gone")


class SearchFromFreeSide(unittest.TestCase):
    def test_sector_starts_from_free_side(self):
        patrol = Patrol()
        patrol.note_edge("right")
        patrol._start_cycle(100)
        self.assertEqual(patrol.direction, "left",
                         "a sector into the limit would end before it starts and waste the cycle")

    def test_without_edge_order_unchanged(self):
        patrol = Patrol()
        patrol._start_cycle(100)
        self.assertEqual(patrol.direction, "right")


if __name__ == "__main__":
    unittest.main()
