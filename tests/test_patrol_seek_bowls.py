"""The return hit the edge of travel with no bowls visible: home is on the other side.

A case from the live camera (2026-11-25, 10:35-11:14). Following took the camera
to the right limit, and odometry had the wrong sign: "home is 0.17 to the right",
while the bowls were almost a frame width to the left. The return went right,
hit the stop, the patrol reset the counter and declared the TV view home, and
stayed there for 3 minutes until the next cycle, again and again.
"""
from __future__ import annotations

import unittest

from pawsight.patrol import SEEK_MAX_PULSES, Patrol
from tests.test_patrol import World, patrol, run

RIGHT_EDGE = 0.80     # right limit of travel: the bowls (at zero) are not visible from here


class EdgeWorld(World):
    patrol: Patrol | None = None

    def step(self, direction: str) -> bool:
        before = self.refused
        ok = super().step(direction)
        if self.refused > before and self.patrol is not None:
            self.patrol.note_edge(direction)
        return ok


def lost_at_right_edge(**kw):
    w = EdgeWorld(pos=RIGHT_EDGE, limit=(-2.0, RIGHT_EDGE), fridge=False)
    p = patrol(w, **kw)
    w.patrol = p
    p.pos = -0.17            # odometry lies: "home is to the right"
    return w, p


class SeekBowlsAfterEdge(unittest.TestCase):
    def test_finds_bowls_on_the_other_side_and_settles_home(self):
        w, p = lost_at_right_edge()
        p.note_dog(0.0)
        with self.assertLogs("pawsight.patrol", level="INFO") as cm:
            run(p, w, 1.0, 120.0)
        text = "\n".join(cm.output)
        self.assertIn("searching for the bowls left", text)
        self.assertIn("bowls found", text)
        self.assertNotIn("odometry lost", text)
        self.assertEqual(p.state, "home")
        self.assertTrue(p.home_confirmed)
        self.assertLessEqual(abs(w.pos), 0.10, "the camera must settle on the bowls")

    def test_bowls_not_found_search_is_bounded(self):
        w, p = lost_at_right_edge()
        w.blind = True                   # dark: no bowls visible anywhere
        p.note_dog(0.0)
        with self.assertLogs("pawsight.patrol", level="INFO") as cm:
            run(p, w, 1.0, 150.0)       # before the dog-search cycle (180 s)
        self.assertLessEqual(w.lefts, SEEK_MAX_PULSES)
        self.assertIn("bowls not found", "\n".join(cm.output))
        self.assertEqual(p.state, "home")
        self.assertIsNone(p.state_dict()["seek"])

    def test_dog_interrupts_bowl_search(self):
        w, p = lost_at_right_edge()
        p.note_dog(0.0)
        t = run(p, w, 1.0, 20.0)         # the edge and the first search pulses
        moves = len(w.moves)
        t = run(p, w, t, 5.0, dogs=True)
        self.assertEqual(len(w.moves), moves, "while the dog is in frame, following owns the motor")


if __name__ == "__main__":
    unittest.main()
