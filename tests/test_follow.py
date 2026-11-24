"""Following when the dog leaves the frame: the clock and the motor are faked."""
import unittest

from pawsight.follow import COOLDOWN_SEC, DogFollow, SEARCH_STEPS, SEARCH_WINDOW_SEC
from pawsight.patrol import Patrol, Scene


def dog(cx, width=40):
    return [{"box": [cx - width / 2, 100, cx + width / 2, 220]}]


class Worker:
    def __init__(self):
        self.busy = False
        self.commands = []
        self.results = []

    def submit(self, direction, tag):
        self.commands.append((direction, tag))
        self.busy = True

    def finish(self, ok=True, error=None):
        direction, tag = self.commands[-1]
        self.results.append((direction, tag, ok, error, .1))
        self.busy = False

    def poll(self):
        results, self.results = self.results, []
        return results


class Tracking(unittest.TestCase):
    def setUp(self):
        self.worker = Worker()
        self.patrol = Patrol()
        self.follow = DogFollow(object(), 640, worker=self.worker, patrol=self.patrol)

    def test_exit_during_cooldown_uses_latest_position(self):
        self.follow.tick(dog(240), False, 100)
        self.worker.finish()
        self.follow.tick(dog(620), False, 101)  # side change during the pause
        self.follow.tick([], False, 104)
        self.assertEqual(self.worker.commands, [("left", "follow"), ("right", "follow")])

    def test_exit_during_camera_motion_is_not_forgotten(self):
        self.follow.tick(dog(320), False, 100)
        self.follow.tick(dog(620), True, 101)
        self.assertEqual(self.worker.commands, [])
        self.follow.tick([], False, 102)
        self.assertEqual(self.worker.commands, [("right", "follow")])

    def test_large_dog_touching_edge_can_be_reacquired(self):
        self.follow._next_ok = 102
        self.follow.tick(dog(470, 340), False, 100)  # centre +0.23, the box already at the edge
        self.follow.tick([], False, 102)
        self.assertEqual(self.worker.commands, [("right", "follow")])

    def test_three_catchup_steps_fit_window_but_do_not_continue_forever(self):
        self.follow.tick(dog(620), False, 100)
        self.worker.finish()
        self.follow.tick(dog(620), True, 100.2)
        for n in range(SEARCH_STEPS):
            now = 100 + COOLDOWN_SEC * (n + 1) + .3
            self.follow.tick([], False, now)
            self.worker.finish()
            self.follow.tick([], True, now + .1)
        self.assertEqual(self.worker.commands, [("right", "follow")] * (SEARCH_STEPS + 1))
        self.follow.tick([], False, 100 + SEARCH_WINDOW_SEC + 1, Scene())
        self.assertEqual(sum(tag == "follow" for _, tag in self.worker.commands), SEARCH_STEPS + 1)

    def test_disappearance_in_center_does_not_start_blind_chase(self):
        self.follow.tick(dog(320), False, 100)
        self.follow.tick([], False, 101)
        self.assertEqual(self.worker.commands, [])

    def test_old_target_after_stream_gap_does_not_start_chase(self):
        self.follow.tick(dog(620), True, 100)
        self.follow.stream_stalled(True)
        self.follow.tick([], False, 101)
        self.follow.stream_stalled(False)
        self.follow.tick([], False, 100 + SEARCH_WINDOW_SEC + 1)
        self.assertEqual(self.worker.commands, [])

    def test_opposite_motion_releases_old_edge_for_follow_and_patrol(self):
        self.follow._hit_edge("right", 100, "overflow")
        self.follow._pending_move = ("left", 101)
        self.follow._note_motion(False, 101.1)
        self.assertGreater(self.follow._blocked_until["right"], 102)
        self.follow._note_motion(True, 101.2)
        self.follow.tick(dog(620), False, 102)
        self.assertEqual(self.worker.commands, [("right", "follow")])
        self.assertIsNone(self.patrol._edge)

    def test_overflow_still_blocks_requested_direction(self):
        self.follow.tick(dog(620), False, 100)
        self.worker.finish(False, "overflow")
        self.follow.tick(dog(620), False, 104)
        self.assertEqual(len(self.worker.commands), 1)
        self.assertIn("edge", self.follow.state()["status"])

    def test_status_explains_cooldown_while_target_position_changes(self):
        self.follow._next_ok = 104
        self.follow.tick(dog(620), False, 101)
        state = self.follow.state()
        self.assertIn("pause", state["status"])
        self.assertAlmostEqual(state["offset"], .47, places=2)

    def test_return_to_center_cancels_previous_exit_direction(self):
        self.follow._next_ok = 104
        self.follow.tick(dog(620), False, 100)
        self.follow.tick(dog(320), False, 101)
        self.follow.tick([], False, 104)
        self.assertEqual(self.worker.commands, [])

    def test_reenabling_does_not_chase_target_from_before_disable(self):
        self.follow.tick(dog(620), True, 100)
        self.follow.set_enabled(False)
        self.follow.set_enabled(True)
        self.follow.tick([], False, 102)
        self.assertEqual(self.worker.commands, [])

    def test_home_request_has_priority_over_fresh_dog(self):
        self.follow.tick(dog(620), True, 100)
        self.follow.request_home(101)
        self.follow.tick(dog(620), False, 102, Scene(bowls=0, settled=True))
        self.assertEqual(self.worker.commands, [])
        self.assertFalse(self.patrol.return_requested)
        self.assertTrue(self.follow.enabled)

    def test_failed_command_does_not_confirm_motion_away_from_edge(self):
        self.follow._hit_edge("right", 100, "overflow")
        self.follow._pending_move = ("left", 101)
        self.follow._hit_edge("left", 102, "overflow")
        self.follow._note_motion(True, 102.1)
        self.assertGreater(self.follow._blocked_until["right"], 103)

    def test_sync_motor_also_respects_cooldown(self):
        class Motor:
            def __init__(self):
                self.moves = []

            def rotate(self, direction):
                self.moves.append(direction)

        motor = Motor()
        follow = DogFollow(motor, 640)
        follow.tick(dog(620), False, 100)
        follow.tick(dog(620), False, 100.2)
        self.assertEqual(motor.moves, ["right"])


if __name__ == "__main__":
    unittest.main()
