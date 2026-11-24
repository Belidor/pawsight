"""Fine-tuning start gate: how many frames are enough and what to do with the split.

The owner's rule: 50 labelled frames means you can start, and start FROM THE
DASHBOARD, without going to a terminal. So a small validation split is no
longer a reason to refuse: it rebuilds itself, and the cost (past runs'
metrics become incomparable) is stated out loud, in the log and in
train_result.

No torch or ultralytics here: `_split` works with files and can be tested on
empty jpg stubs.
"""
from __future__ import annotations

import json
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pawsight import train
from pawsight.labels import MIN_IMAGES, VAL_MIN_FRACTION


def make_dataset(root: Path, n: int, *, with_bowl: bool = True) -> list[str]:
    """n "frame + labels" pairs. Names as in auto-capture, so the test does not
    drift from reality in the details."""
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "labels").mkdir(parents=True, exist_ok=True)
    stems = []
    for i in range(n):
        stem = f"dog_20260906_{i // 60:02d}{i % 60:02d}00_5{i % 10}"
        (root / "images" / f"{stem}.jpg").write_bytes(b"jpg")
        line = "0 0.5 0.5 0.2 0.2\n" + ("1 0.2 0.8 0.1 0.1\n" if with_bowl else "")
        (root / "labels" / f"{stem}.txt").write_text(line)
        stems.append(stem)
    return stems


class SplitGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dataset"
        self.addCleanup(self.tmp.cleanup)

    def test_below_fifty_frames_training_does_not_start(self) -> None:
        make_dataset(self.root, 49)
        with self.assertRaises(SystemExit) as e:
            train._split(self.root)
        self.assertIn("50", str(e.exception))

    def test_exactly_fifty_frames_is_enough(self) -> None:
        make_dataset(self.root, 50)
        n_tr, n_va, refrozen = train._split(self.root)
        self.assertEqual(n_tr + n_va, 50)
        self.assertGreater(n_va, 0)
        self.assertIsNone(refrozen)

    def test_tiny_split_is_rebuilt_instead_of_refusing(self) -> None:
        """This used to be a SystemExit advising a trip to the terminal."""
        stems = make_dataset(self.root, 60)
        (self.root / "val_manifest.json").write_text(json.dumps(stems[:2]))

        n_tr, n_va, refrozen = train._split(self.root)

        val = json.loads((self.root / "val_manifest.json").read_text())
        self.assertGreaterEqual(len(val) / 60, VAL_MIN_FRACTION)
        self.assertEqual(n_va, len(val))
        self.assertEqual(n_tr + n_va, 60)
        # The rebuild must be STATED: it devalues comparison with past runs,
        # and that goes into train_result.json.
        self.assertEqual(refrozen, {"was": 2, "now": len(val), "pairs": 60})

    def test_after_rebuild_val_files_match_the_manifest(self) -> None:
        stems = make_dataset(self.root, 60)
        (self.root / "val_manifest.json").write_text(json.dumps(stems[:2]))

        train._split(self.root)

        val = set(json.loads((self.root / "val_manifest.json").read_text()))
        on_disk = {p.stem for p in (self.root / "images" / "val").glob("*.jpg")}
        self.assertEqual(on_disk, val)
        self.assertEqual({p.stem for p in (self.root / "labels" / "val").glob("*.txt")}, val)

    def test_sufficient_split_stays_frozen(self) -> None:
        """No rebuilding without need: weekly metrics must stay comparable."""
        stems = make_dataset(self.root, 60)
        frozen = sorted(stems[:20])
        (self.root / "val_manifest.json").write_text(json.dumps(frozen))

        train._split(self.root)

        self.assertEqual(json.loads((self.root / "val_manifest.json").read_text()), frozen)


class RefreezeVal(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dataset"
        self.addCleanup(self.tmp.cleanup)

    def test_rebuild_takes_one_fifth(self) -> None:
        make_dataset(self.root, 100)
        val, pairs = train.refreeze_val(self.root)
        self.assertEqual(pairs, 100)
        self.assertEqual(val, 20)

    def test_rebuild_is_deterministic(self) -> None:
        make_dataset(self.root, 100)
        train.refreeze_val(self.root)
        first = json.loads((self.root / "val_manifest.json").read_text())
        train.refreeze_val(self.root)
        self.assertEqual(json.loads((self.root / "val_manifest.json").read_text()), first)

    def test_only_labelled_frames_enter_the_split(self) -> None:
        stems = make_dataset(self.root, 60)
        (self.root / "images" / "sirota.jpg").write_bytes(b"jpg")   # no labels
        val, pairs = train.refreeze_val(self.root)
        self.assertEqual(pairs, 60)
        names = json.loads((self.root / "val_manifest.json").read_text())
        self.assertNotIn("sirota", names)
        self.assertTrue(set(names) <= set(stems))


class TrainerCheck(unittest.TestCase):
    """What the "Start fine-tuning" button on the dashboard answers."""

    def setUp(self) -> None:
        from pawsight import trainer

        self.trainer_mod = trainer
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dataset"
        self.addCleanup(self.tmp.cleanup)
        # The lock goes to a temp dir: tests do not touch the live storage/.
        self._lock = trainer.LOCK
        trainer.LOCK = Path(self.tmp.name) / "train.lock"
        self.addCleanup(lambda: setattr(trainer, "LOCK", self._lock))

    def _trainer(self):
        cfg = types.SimpleNamespace(dataset_dir=self.root)
        return self.trainer_mod.Trainer(cfg)

    def test_small_split_no_longer_disables_the_button(self) -> None:
        stems = make_dataset(self.root, 60)
        (self.root / "val_manifest.json").write_text(json.dumps(stems[:2]))
        self.assertIsNone(self._trainer().check())

    def test_below_fifty_frames_button_is_disabled(self) -> None:
        make_dataset(self.root, 49)
        why = self._trainer().check()
        self.assertIsNotNone(why)
        self.assertIn("50", why)

    def test_refusal_does_not_send_to_the_terminal(self) -> None:
        """The whole point of the exercise: the dashboard is self-sufficient."""
        make_dataset(self.root, 49)
        self.assertNotIn("refreeze", self._trainer().check())


class DatasetStats(unittest.TestCase):
    """What the dashboard receives over the wire and draws the button from."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name) / "dataset"
        self.addCleanup(self.tmp.cleanup)

    def test_readiness_does_not_depend_on_split_size(self) -> None:
        from pawsight.hoststat import dataset_stats

        stems = make_dataset(self.root, 60)
        (self.root / "val_manifest.json").write_text(json.dumps(stems[:2]))
        st = dataset_stats(self.root)
        self.assertFalse(st["val_ok"])      # a warning is still due
        self.assertTrue(st["ready"])        # but it does not block the start

    def test_below_threshold_not_ready(self) -> None:
        from pawsight.hoststat import dataset_stats

        make_dataset(self.root, 49)
        self.assertFalse(dataset_stats(self.root)["ready"])


class Thresholds(unittest.TestCase):
    def test_start_threshold_is_fifty_frames(self) -> None:
        self.assertEqual(MIN_IMAGES, 50)

    def test_dashboard_does_not_send_to_terminal_for_a_command(self) -> None:
        """The promise of the whole exercise: from the dashboard, at any time.

        Checked crudely, by the page text, and that is enough: as long as the
        dashboard markup contains a console command, some path still ends in
        a terminal."""
        static = Path(__file__).resolve().parent.parent / "pawsight" / "static"
        html = "".join((static / f).read_text()
                       for f in ("dashboard.html", "dashboard.js", "dashboard.css"))
        self.assertFalse("refreeze-val" in html,
                         "the dashboard still sends the person to a terminal for a command")


if __name__ == "__main__":
    unittest.main()
