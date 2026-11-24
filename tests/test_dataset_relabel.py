"""Editing a dataset frame's labels: what must not be erased silently.

The "Remove all boxes" button on the dashboard clears the labels in one click,
and that is exactly why the same guard as for saving a freeze-frame sits here:
a vanished last `bowl` box teaches the model that the bowl is background, and
ALL of the project's zones rely on the `bowl` class. The error is silent: after
such training the dog is found as if nothing happened.

The dog is deliberately not part of the guard: a frame without a dog is a
legitimate negative example, and it is how a box on a chair base gets fixed.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

try:
    from pawsight.dataset import Capture
    from pawsight.detector import Detector
except ImportError:                     # the "brain" image: OpenCV is not needed here
    Detector = None


class FakeCfg:
    def __init__(self, root: Path) -> None:
        self.dataset_dir = root


@unittest.skipIf(Detector is None, "no OpenCV: this is the \"brain\" image")
class MissingClassGuard(unittest.TestCase):
    """A detector without its constructor: that one reaches for the PTZ and the
    tilt reference, which have nothing to do with editing labels."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cap = Capture(self.dir)
        (self.cap.img_dir / "dog_20260906_120000_42.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        (self.cap.lbl_dir / "dog_20260906_120000_42.txt").write_text(
            "0 0.500000 0.500000 0.200000 0.300000\n"      # dog
            "1 0.800000 0.800000 0.070000 0.060000\n")     # bowl
        self.det = Detector.__new__(Detector)
        self.det.cfg = FakeCfg(self.dir)
        self.det.capture = self.cap
        self.det.on_changed = lambda: None
        self.replies = []
        self.det.on_event = lambda kind, msg, blob=None: self.replies.append(msg)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def save(self, boxes, **extra):
        self.det._dataset_cmd("dataset_save", {
            "id": "r1", "name": "dog_20260906_120000_42", "boxes": boxes, **extra})
        return self.replies[-1]

    def labels(self) -> str:
        return (self.cap.lbl_dir / "dog_20260906_120000_42.txt").read_text()

    def test_last_bowl_does_not_vanish_without_confirmation(self):
        r = self.save([{"cls": "dog", "cx": .5, "cy": .5, "w": .2, "h": .3}])
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_confirm"])
        self.assertEqual(r["missing"], ["bowl"])
        self.assertIn("1 0.800000", self.labels(), "the file must not have changed")

    def test_confirmation_lets_it_through(self):
        r = self.save([{"cls": "dog", "cx": .5, "cy": .5, "w": .2, "h": .3}],
                           confirm_empty=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["labels"], 1)
        self.assertNotIn("\n1 ", self.labels())

    def test_remove_all_also_asks(self):
        """\"Remove all boxes\" is the shortest path to an empty file."""
        r = self.save([])
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing"], ["bowl"])

    def test_empty_labels_after_confirmation_are_legitimate(self):
        """A negative example: there is nothing in the frame, and that must be expressible."""
        r = self.save([], confirm_empty=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["labels"], 0)
        self.assertEqual(self.labels(), "")

    def test_missing_dog_does_not_ask(self):
        r = self.save([{"cls": "bowl", "cx": .8, "cy": .8, "w": .07, "h": .06}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["labels"], 1)

    def test_one_bowl_of_two_is_no_reason_to_ask(self):
        """The guard is about the CLASS, not the number of boxes: a cup the
        model called a bowl must be removable without ceremony."""
        (self.cap.lbl_dir / "dog_20260906_120000_42.txt").write_text(
            "1 0.800000 0.800000 0.070000 0.060000\n"
            "1 0.200000 0.300000 0.050000 0.050000\n")
        r = self.save([{"cls": "bowl", "cx": .8, "cy": .8, "w": .07, "h": .06}])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["labels"], 1)

    def test_foreign_class_is_rejected_before_writing(self):
        r = self.save([{"cls": "cat", "cx": .5, "cy": .5, "w": .2, "h": .2}])
        self.assertFalse(r["ok"])
        self.assertIn("1 0.800000", self.labels())


if __name__ == "__main__":
    unittest.main()
