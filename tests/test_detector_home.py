"""The patrol must not mistake zone memory for new home coordinates."""
import unittest
from types import SimpleNamespace

from pawsight.anchors import AnchoredZone
from pawsight.patrol import Patrol, RIGHT_FRAC

try:
    from pawsight.detector import Detector
except ImportError:
    Detector = None


@unittest.skipIf(Detector is None, "no OpenCV: this is the brain image")
class HomeScene(unittest.TestCase):
    def setUp(self):
        self.anchor = AnchoredZone("bowls", "at the bowls", "bowl", memory_sec=20)
        self.bowls = [("bowl", .9, (294, 250, 314, 270)),
                      ("bowl", .9, (326, 250, 346, 270))]
        self.anchor.update(self.bowls, 100)
        self.det = Detector.__new__(Detector)
        self.det.cfg = SimpleNamespace(infer_w=640)
        self.det.anchors = {"bowls": self.anchor}
        self.det._names = {0: "refrigerator"}

    def test_old_bowls_do_not_undo_pan_or_confirm_home(self):
        patrol = Patrol()
        for _ in range(3):
            patrol.note_pulse("right")
        scene = self.det._home_scene([], True, 104)
        patrol._observe(104, scene)
        patrol._home(104)
        self.assertIsNone(scene.bowls)
        self.assertAlmostEqual(patrol.pos, 3 * RIGHT_FRAC)
        self.assertFalse(patrol.home_confirmed)
        self.assertIsNotNone(self.anchor.polygon(104), "ordinary zone memory is kept")

    def test_current_pair_corrects_odometry(self):
        shifted = [(name, conf, (x1-64, y1, x2-64, y2))
                   for name, conf, (x1, y1, x2, y2) in self.bowls]
        self.anchor.update(shifted, 104)
        scene = self.det._home_scene([], True, 104)
        patrol = Patrol()
        patrol._observe(104, scene)
        self.assertAlmostEqual(patrol.pos, .1)
        self.assertEqual(patrol._bowls_ts, 104)

    def test_single_bowl_does_not_refresh_old_pair(self):
        self.anchor.update(self.bowls[:1], 104)
        self.assertIsNone(self.det._home_scene([], True, 104).bowls)

    def test_fresh_fridge_is_used_when_bowls_are_cached(self):
        scene = self.det._home_scene([(0, .9, (250, 30, 326, 300))], True, 104)
        patrol = Patrol()
        patrol.fridge_abs = .2
        patrol._observe(104, scene)
        self.assertIsNone(scene.bowls)
        self.assertAlmostEqual(patrol.pos, .25)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(Detector is None, "no OpenCV: this is the brain image")
class HomeWithFloorShape(unittest.TestCase):
    """Home is the centre of the BOWL PAIR, not the centre of the floor shape: the
    shape stretches towards the mat, and by its centre the patrol would decide
    home is further left than it is."""

    def test_lopsided_floor_shape_does_not_move_home(self):
        anchor = AnchoredZone("bowls", "at the bowls", "bowl", memory_sec=20,
                              shape=((-125, -6), (26, -2), (9, 21), (-107, 54)))
        bowls = [("bowl", .9, (294, 250, 314, 270)), ("bowl", .9, (326, 250, 346, 270))]
        anchor.update(bowls, 104)
        det = Detector.__new__(Detector)
        det.cfg = SimpleNamespace(infer_w=640)
        det.anchors = {"bowls": anchor}
        det._names = {0: "refrigerator"}
        scene = det._home_scene([], True, 104)
        self.assertAlmostEqual(scene.bowls, 0.0)   # the pair sits exactly at the frame centre
