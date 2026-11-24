"""Manual relabelling: does it survive into training exactly as it was made.

This is the most expensive thing in the dataset. Frames are collected on their
own, but a human edits the boxes, and edits exactly the frames where the model
was wrong, i.e. the most valuable ones. They can be lost in three ways, each
of them silent:

  1. the write never reached the file (half-written file, wrong format, mixed-up class);
  2. the edit got overwritten by pre-labelling, auto-capture or cleanup;
  3. the edit exists but never reached training, e.g. because an "empty file"
     was somewhere taken to mean "no labels".

The third case is especially nasty: erasing all boxes is a legitimate and the
most frequent kind of edit (the model boxed a chair base and called it a dog).
An empty file is not "no data" but a training example of "no dog here".

The tests work with real files in a temp directory: cv2 is only needed by
save_labeled, and relabelling a frame already on disk does not touch the picture.
"""
from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pawsight import train
from pawsight.dataset import Capture

REPO = Path(__file__).resolve().parent.parent

DOG = {"cls": "dog", "cx": 0.5, "cy": 0.5, "w": 0.3, "h": 0.4}
BOWL = {"cls": "bowl", "cx": 0.2, "cy": 0.8, "w": 0.08, "h": 0.06}


def load_relabel_script():
    """scripts/relabel.py is not a package; load it by path.

    The module does not pull in ultralytics at top level (the import is inside
    main), so this is cheap.
    """
    spec = importlib.util.spec_from_file_location("relabel_script", REPO / "scripts" / "relabel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Saving(unittest.TestCase):
    """Step 1: the human edit reached the disk undistorted."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "dataset"
        self.cap = Capture(self.root)
        (self.root / "images" / "dog_20260906_101010_44.jpg").write_bytes(b"jpg")
        (self.root / "labels" / "dog_20260906_101010_44.txt").write_text("0 0.9 0.9 0.1 0.1\n")
        self.stem = "dog_20260906_101010_44"

    def test_relabel_rewrites_the_label_file(self) -> None:
        self.cap.relabel(self.stem, [DOG, BOWL])
        text = (self.root / "labels" / f"{self.stem}.txt").read_text()
        self.assertEqual(text.strip().split("\n")[0].split()[0], "0")     # dog
        self.assertEqual(text.strip().split("\n")[1].split()[0], "1")     # bowl
        self.assertNotIn("0.9 0.9", text)                                 # the old box is gone

    def test_frame_enters_the_human_label_registry(self) -> None:
        self.cap.relabel(self.stem, [DOG])
        names = json.loads((self.root / "human_labeled.json").read_text())
        self.assertIn(self.stem, names)

    def test_repeated_edit_does_not_duplicate_registry_entries(self) -> None:
        self.cap.relabel(self.stem, [DOG])
        self.cap.relabel(self.stem, [DOG, BOWL])
        names = json.loads((self.root / "human_labeled.json").read_text())
        self.assertEqual(names.count(self.stem), 1)

    def test_removing_all_boxes_gives_an_empty_file_not_a_missing_one(self) -> None:
        """The person erased a false dog. That is a negative example, not a gap."""
        n = self.cap.relabel(self.stem, [])
        f = self.root / "labels" / f"{self.stem}.txt"
        self.assertEqual(n, 0)
        self.assertTrue(f.exists())
        self.assertEqual(f.read_text().strip(), "")

    def test_editing_a_missing_frame_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.cap.relabel("no-such-frame", [DOG])


class NotOverwritten(unittest.TestCase):
    """Step 2: pre-labelling does not overwrite the human edit."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "dataset"
        (self.root / "images").mkdir(parents=True)
        (self.root / "labels").mkdir(parents=True)
        self.img, self.lbl = self.root / "images", self.root / "labels"

    def test_unlabelled_frame_goes_to_pre_labelling(self) -> None:
        (self.img / "dog_1.jpg").write_bytes(b"jpg")
        todo = load_relabel_script().pending(self.img, self.lbl, human=set())
        self.assertEqual([p.stem for p in todo], ["dog_1"])

    def test_labelled_frame_is_left_alone(self) -> None:
        (self.img / "dog_1.jpg").write_bytes(b"jpg")
        (self.lbl / "dog_1.txt").write_text("0 0.5 0.5 0.2 0.2\n")
        todo = load_relabel_script().pending(self.img, self.lbl, human=set())
        self.assertEqual(todo, [])

    def test_human_frame_is_left_alone_even_without_label_file(self) -> None:
        """The model got it wrong once: its prediction here is known to be worse
        than the human's, and must not be restored by pre-labelling."""
        (self.img / "dog_1.jpg").write_bytes(b"jpg")
        todo = load_relabel_script().pending(self.img, self.lbl, human={"dog_1"})
        self.assertEqual(todo, [])

    def test_empty_human_labels_are_not_taken_for_missing(self) -> None:
        (self.img / "dog_1.jpg").write_bytes(b"jpg")
        (self.lbl / "dog_1.txt").write_text("")
        todo = load_relabel_script().pending(self.img, self.lbl, human={"dog_1"})
        self.assertEqual(todo, [])


class ReachesTraining(unittest.TestCase):
    """Step 3: the edit reaches training, byte for byte and without loss."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "dataset"
        self.cap = Capture(self.root)
        self.stems = []
        for i in range(60):
            stem = f"dog_20260906_{i:04d}00_5{i % 10}"
            (self.root / "images" / f"{stem}.jpg").write_bytes(b"jpg")
            (self.root / "labels" / f"{stem}.txt").write_text("0 0.5 0.5 0.2 0.2\n")
            self.stems.append(stem)

    def _split_of(self, stem: str) -> str:
        for split in ("train", "val"):
            if (self.root / "images" / split / f"{stem}.jpg").exists():
                return split
        raise AssertionError(f"frame {stem} ended up in neither train nor val")

    def test_corrected_labels_arrive_byte_for_byte(self) -> None:
        stem = self.stems[0]
        self.cap.relabel(stem, [DOG, BOWL])
        source = (self.root / "labels" / f"{stem}.txt").read_bytes()

        train._split(self.root)

        split = self._split_of(stem)
        self.assertEqual((self.root / "labels" / split / f"{stem}.txt").read_bytes(), source)

    def test_frame_with_empty_labels_stays_in_training(self) -> None:
        """The negative example must arrive: it is what cures a false dog."""
        stem = self.stems[1]
        self.cap.relabel(stem, [])

        n_tr, n_va, _ = train._split(self.root)

        self.assertEqual(n_tr + n_va, 60)          # the frame did not drop out of the dataset
        split = self._split_of(stem)
        self.assertEqual((self.root / "labels" / split / f"{stem}.txt").read_text().strip(), "")

    def test_edit_after_run_start_does_not_enter_that_run(self) -> None:
        """By design: _split is a snapshot. But this must be known, otherwise
        the person expects something from the running run that is not in it."""
        stem = self.stems[2]
        train._split(self.root)
        split = self._split_of(stem)

        self.cap.relabel(stem, [])                 # edit after the split was laid out

        self.assertNotEqual(
            (self.root / "labels" / split / f"{stem}.txt").read_text().strip(),
            (self.root / "labels" / f"{stem}.txt").read_text().strip())


if __name__ == "__main__":
    unittest.main()
