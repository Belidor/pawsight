"""Chasing after losing the dog: the decision is based on the last position and trajectory.

A case from the live camera (2026-09-07, 20:03:05): the dog was last seen at
-0.22 widths, it dropped out of the tracks, the camera stood still, and 10 s
later the patrol took it in the opposite direction. Where the dog went could not
be told from the log, so the chase decision is now logged in full.
"""
from __future__ import annotations

import unittest

from pawsight.follow import COOLDOWN_SEC, DogFollow, SEARCH_STEPS
from pawsight.patrol import Patrol, Scene
from tests.test_follow import Worker, dog


class Chase(unittest.TestCase):
    def setUp(self):
        self.worker = Worker()
        self.patrol = Patrol()
        self.follow = DogFollow(object(), 640, worker=self.worker, patrol=self.patrol)

    def _chase(self, t: float, rounds: int = SEARCH_STEPS + 1) -> list:
        """No dog: give the chase as many ticks as there are steps to fit."""
        for _ in range(rounds):
            self.follow.tick([], False, t)
            if self.worker.busy:
                self.worker.finish()
            t += COOLDOWN_SEC + .1
        return [c for c in self.worker.commands if c[1] == "follow"]

    def test_loss_away_from_edge_chased_by_dead_reckoning(self):
        self.follow._next_ok = 103                    # motor pause: no step
        self.follow.tick(dog(180), False, 100)        # -0.22: not at the edge, not fast
        cmds = self._chase(103.5)
        # -0.22 + 0.16 = -0.06: the estimated position is already in the dead zone: exactly one step
        self.assertEqual(cmds, [("left", "follow")])

    def test_visible_step_already_counted_in_dead_reckoning(self):
        self.follow.tick(dog(180), False, 100)        # -0.22 -> left step made with the dog visible
        self.worker.finish()
        cmds = self._chase(103.6)
        self.assertEqual(cmds, [("left", "follow")],
                         "after the step the dog is estimated at the centre: no overshoot")

    def test_observation_during_pause_accounts_for_unfinished_step(self):
        # The frame lags the motor: the dog at -0.29 right after the left command
        # has not shifted in the frame yet. Counting from -0.29 overshoots; the step is on its way.
        self.follow.tick(dog(200), False, 100)        # -0.19 -> left step
        self.worker.finish()
        self.follow.tick(dog(135), False, 100.4)      # -0.29 during the pause
        cmds = self._chase(103.6)
        self.assertEqual(cmds, [("left", "follow")] * 2,
                         "unfinished step subtracted: -0.29+0.16=-0.13 -> one more chase step, not two")

    def test_dead_reckoning_at_most_two_steps(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(490), False, 100)        # +0.27 right: (0.27-0.10)/0.096 -> 2
        cmds = self._chase(103.5)
        self.assertEqual(cmds, [("right", "follow")] * 2)

    def test_dog_walking_to_edge_full_chase(self):
        for t, cx in ((100.0, 300), (100.4, 270), (100.8, 240), (101.2, 200)):
            self.follow.tick(dog(cx), False, t)       # -0.03 -> -0.19 in 1.2 s: ~0.13 widths/s
        self.assertEqual(self.worker.commands, [("left", "follow")], "step with the dog visible")
        self.worker.finish()
        cmds = self._chase(104.8)
        self.assertEqual(cmds, [("left", "follow")] * (SEARCH_STEPS + 1))

    def test_frames_during_pan_not_counted_as_walking(self):
        self.follow.tick(dog(300), False, 100.0)      # -0.03, a quiet frame
        self.follow._next_ok = 103                    # from here the camera is in the post-command pause
        self.follow._last_cmd_ts = 100.0
        for t, cx in ((100.4, 270), (100.8, 240), (101.2, 150)):
            self.follow.tick(dog(cx), False, t)       # frame shift from the motor, not the dog
        cmds = self._chase(103.5)
        self.assertEqual(len(cmds), 2, "-0.27 -> two dead reckoning steps, not a full chase")

    def test_box_jitter_is_not_walking(self):
        for t, cx in ((100.0, 300), (100.4, 250), (100.8, 290), (101.2, 200)):
            self.follow.tick(dog(cx), False, t)       # back and forth: not monotonic
        self.worker.finish()
        cmds = self._chase(104.8)
        self.assertEqual(cmds, [("left", "follow")], "a visible step, then estimated at the centre")

    def test_vanished_in_dead_zone_stay(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(300), False, 100)        # -0.03
        self.assertEqual(self._chase(103.5), [])

    def test_at_edge_full_chase_as_before(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(620), False, 100)        # +0.47
        cmds = self._chase(103.5)
        self.assertEqual(cmds, [("right", "follow")] * SEARCH_STEPS)

    def test_new_observation_resets_chase_plan(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(180), False, 100)
        self.follow.tick([], False, 101)              # loss (during the pause)
        self.follow.tick(dog(320), False, 102)        # came back to the centre
        self.assertEqual(self._chase(103.5), [])

    def test_blocked_direction_hands_camera_to_patrol(self):
        self.follow._hit_edge("right", 90, "overflow")
        self.follow._next_ok = 103
        self.follow.tick(dog(620), False, 100)        # +0.47, but right is locked
        self.patrol.pos = 0.30                        # the patrol has somewhere to go: left
        self.follow.tick([], False, 112, Scene(settled=True))
        self.assertEqual(self.worker.commands, [("left", "patrol")],
                         "chase impossible: the patrol must not wait 14.5 s")

    def test_after_exhausted_chase_patrol_waits_full_pause(self):
        # Case 20:24:21-20:24:35: three left chase steps and 3.5 s later four
        # rights in a burst towards home. The patrol pause must count from the last chase step.
        self.follow._next_ok = 103
        self.follow.tick(dog(620), False, 100)        # at the edge -> plan for 3 steps
        t = 103.5
        for _ in range(SEARCH_STEPS):
            self.follow.tick([], False, t)
            self.worker.finish()
            t += COOLDOWN_SEC + .1
        self.assertEqual(len(self.worker.commands), SEARCH_STEPS)
        self.patrol.pos = -0.5
        self.follow.tick([], False, t, Scene(settled=True))     # 14.3 s after the dog, 3.6 s after the chase
        self.assertEqual([c for c in self.worker.commands if c[1] == "patrol"], [],
                         "the patrol does not take the camera right after a chase")
        self.follow.tick([], False, t + 10, Scene(settled=True))
        self.assertEqual([c for c in self.worker.commands if c[1] == "patrol"], [("right", "patrol")],
                         "release_sec after the chase: return home")

    def test_vanished_mid_frame_patrol_waits_longer(self):
        # Case 20:08:04-20:08:18: a lying dog at -0.07 was not detected for 12 s,
        # the patrol jerked the camera right, the dog "moved" to -0.18, following stepped back.
        self.follow._next_ok = 103
        self.follow.tick(dog(275), False, 100)        # -0.07, near the centre
        self.patrol.pos = -0.30
        for t in (103.5, 115, 140):
            self.follow.tick([], False, t, Scene(settled=True))
        self.assertEqual(self.worker.commands, [], "the dog is most likely still there: leave the camera alone")
        self.assertIn("waiting", self.patrol.status)
        self.follow.tick([], False, 165, Scene(settled=True))
        self.assertEqual(self.worker.commands, [("right", "patrol")], "after a minute: home")

    def test_left_at_edge_patrol_waits_only_normal_pause(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(620), False, 100)        # at the edge: the dog left, no point waiting for it
        self.follow._blocked_until["right"] = 200     # chase impossible (edge)
        self.patrol.pos = 0.30
        self.follow.tick([], False, 112, Scene(settled=True))
        self.assertEqual(self.worker.commands, [("left", "patrol")])

    def test_dog_seen_again_old_hold_cleared(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(275), False, 100)        # -0.07 -> loss mid-frame -> hold
        self.follow.tick([], False, 103.5, Scene(settled=True))
        self.follow._blocked_until["right"] = 300     # edge: no way to step after it
        self.follow.tick(dog(620), False, 105)        # came back and left at the right edge
        self.patrol.pos = 0.30
        self.follow.tick([], False, 117, Scene(settled=True))
        self.assertEqual(self.worker.commands, [("left", "patrol")], "no point waiting a minute any more")

    def test_chase_decision_explained_in_log(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(180), False, 100)
        with self.assertLogs("pawsight.follow", level="INFO") as cm:
            self.follow.tick([], False, 103.5)
        text = "\n".join(cm.output)
        self.assertIn("vanished", text)
        self.assertIn("-0.22", text)
        self.assertIn("dead reckoning", text)

    def test_loss_at_centre_also_explained(self):
        self.follow._next_ok = 103
        self.follow.tick(dog(320), False, 100)
        with self.assertLogs("pawsight.follow", level="INFO") as cm:
            self.follow.tick([], False, 103.5)
        self.assertIn("not chasing", "\n".join(cm.output))

    def test_synchronous_motor_steps_one_at_a_time(self):
        class Motor:
            def __init__(self):
                self.moves = []

            def rotate(self, direction):
                self.moves.append(direction)

        motor = Motor()
        follow = DogFollow(motor, 640)
        follow.tick(dog(620), False, 100)
        self.assertEqual(motor.moves, ["right"])


if __name__ == "__main__":
    unittest.main()
