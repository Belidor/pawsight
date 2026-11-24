"""Deleting a frame from the dataset: files, registries and the route guard.

The expensive part here is not the unlink itself but the two registries next
to it. `val_manifest.json` sets the validation fraction by which
`train.py::_split` decides whether the acceptance gate means anything; a
deleted frame's name left in it would inflate that fraction, and the check
would silently stop checking. `.hashes.json`, on the contrary, must not be
touched: without the hash, auto-capture would bring back a nearly identical
frame within a minute.

The dataset module pulls in OpenCV: it is in the native `.venv` but not in
the "brain" image. So without it some tests are skipped rather than failing.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from pawsight.config import Config
from pawsight.web import Dashboard

try:
    from pawsight.dataset import Capture
except ImportError:                     # the "brain" image: OpenCV is not needed here
    Capture = None

# The same header the guard middleware requires on every mutating request: a
# foreign page cannot send it without a preflight request.
GUARD = {"X-Pawsight": "1"}


def add_frame(cap, stem: str, label: str = "0 0.5 0.5 0.2 0.2\n") -> None:
    """A "picture + labels" pair in the dataset. The picture content does not
    matter: delete() does not decode it."""
    (cap.img_dir / f"{stem}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    if label is not None:
        (cap.lbl_dir / f"{stem}.txt").write_text(label)


@unittest.skipIf(Capture is None, "no OpenCV: this is the \"brain\" image")
class DeleteFrame(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cap = Capture(self.dir)
        add_frame(self.cap, "dog_20260906_120000_42")
        add_frame(self.cap, "dog_20260906_130000_51")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_picture_and_labels_go_together(self):
        r = self.cap.delete("dog_20260906_120000_42")
        self.assertFalse((self.cap.img_dir / "dog_20260906_120000_42.jpg").exists())
        self.assertFalse((self.cap.lbl_dir / "dog_20260906_120000_42.txt").exists())
        self.assertTrue(r["had_label"])
        self.assertEqual(r["pairs"], 1, "the second frame must stay")
        self.assertTrue((self.cap.img_dir / "dog_20260906_130000_51.jpg").exists())

    def test_unlabelled_frame_is_deleted_too(self):
        """Unlabelled frames in the dataset are normal, and they are the ones cleaned most often."""
        add_frame(self.cap, "dog_20260906_140000_20", label=None)
        r = self.cap.delete("dog_20260906_140000_20")
        self.assertFalse(r["had_label"])
        self.assertFalse((self.cap.img_dir / "dog_20260906_140000_20.jpg").exists())

    def test_edited_by_human_mark_is_removed(self):
        """Manual frame names are built from the time to the second: a freed
        name may be taken by a new frame that would inherit someone else's "✓"."""
        self.cap._mark_human("dog_20260906_120000_42")
        self.cap._mark_human("dog_20260906_130000_51")
        self.cap.delete("dog_20260906_120000_42")
        left = json.loads((self.dir / "human_labeled.json").read_text())
        self.assertEqual(left, ["dog_20260906_130000_51"])

    def test_frame_is_struck_from_the_validation_split(self):
        """Otherwise the validation fraction in _split would count nonexistent
        names, and the VAL_MIN_FRACTION threshold would stop meaning anything."""
        (self.dir / "val_manifest.json").write_text(json.dumps(
            ["dog_20260906_120000_42", "dog_20260906_130000_51"]))
        r = self.cap.delete("dog_20260906_120000_42")
        self.assertTrue(r["was_val"])
        self.assertEqual(json.loads((self.dir / "val_manifest.json").read_text()),
                         ["dog_20260906_130000_51"])

    def test_other_validation_entries_are_untouched(self):
        (self.dir / "val_manifest.json").write_text(json.dumps(["dog_20260906_130000_51"]))
        r = self.cap.delete("dog_20260906_120000_42")
        self.assertFalse(r["was_val"])
        self.assertEqual(json.loads((self.dir / "val_manifest.json").read_text()),
                         ["dog_20260906_130000_51"])

    def test_hash_stays_in_the_deduplicator(self):
        """A decision, not an oversight: the frame is deleted because it is bad,
        and auto-capture catches the scene; without the hash it would save a
        nearly identical frame."""
        (self.dir / ".hashes.json").write_text(json.dumps([111, 222]))
        self.cap.delete("dog_20260906_120000_42")
        self.assertEqual(json.loads((self.dir / ".hashes.json").read_text()), [111, 222])

    def test_training_copies_are_untouched(self):
        """train.py::_split rebuilds images/train every run, and a fine-tuning
        run in progress reads its files from exactly there."""
        (self.cap.img_dir / "train").mkdir()
        copy = self.cap.img_dir / "train" / "dog_20260906_120000_42.jpg"
        copy.write_bytes(b"\xff\xd8\xff\xd9")
        self.cap.delete("dog_20260906_120000_42")
        self.assertTrue(copy.exists())

    def test_missing_frame_is_a_refusal(self):
        with self.assertRaises(FileNotFoundError):
            self.cap.delete("dog_20990101_000000_99")

    def test_name_with_a_path_is_rejected(self):
        """The name comes from the browser, and the command through an anonymous broker."""
        for bad in ("../../.env", "dog/../../x", "", "a b", "na/me"):
            with self.assertRaises(ValueError, msg=bad):
                self.cap.delete(bad)

    def test_outward_link_does_not_lead_unlink_away(self):
        """A regex is not enough: a symlink inside images/ would lead the deletion outside the dataset."""
        outsider = self.dir.parent / "outsider.jpg"
        outsider.write_bytes(b"\xff\xd8\xff\xd9")
        try:
            os.symlink(outsider, self.cap.img_dir / "link.jpg")
        except OSError:
            self.skipTest("symlink unavailable")
        with self.assertRaises(ValueError):
            self.cap.delete("link")
        self.assertTrue(outsider.exists())
        outsider.unlink()


class FakeCfg:
    def __init__(self, root: Path) -> None:
        self.dataset_dir = root


@unittest.skipIf(Capture is None, "no OpenCV: this is the \"brain\" image")
class DetectorCommand(unittest.TestCase):
    """The detector stands between the dashboard and the disk: the name comes
    from the browser through an anonymous broker, and the detector is what checks it."""

    def setUp(self):
        from pawsight.detector import Detector

        self.dir = Path(tempfile.mkdtemp())
        self.cap = Capture(self.dir)
        add_frame(self.cap, "dog_20260906_120000_42")
        self.det = Detector.__new__(Detector)
        self.det.cfg = FakeCfg(self.dir)
        self.det.capture = self.cap
        self.det.on_changed = lambda: None
        self.replies = []
        self.det.on_event = lambda kind, msg, blob=None: self.replies.append(msg)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def delete(self, name):
        self.det._dataset_cmd("dataset_delete", {"id": "r1", "name": name})
        return self.replies[-1]

    def test_frame_leaves_the_disk(self):
        r = self.delete("dog_20260906_120000_42")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pairs"], 0)
        self.assertFalse((self.cap.img_dir / "dog_20260906_120000_42.jpg").exists())

    def test_name_with_a_path_never_reaches_the_disk(self):
        r = self.delete("../../.env")
        self.assertFalse(r["ok"])
        self.assertIn("invalid frame name", r["error"])

    def test_unknown_frame_is_a_refusal_not_an_exception(self):
        r = self.delete("dog_20990101_000000_99")
        self.assertFalse(r["ok"])
        self.assertIn("not in the dataset", r["error"])


class DeleteRoute(unittest.IsolatedAsyncioTestCase):
    """The route guard, and that the request reaches the detector at all."""

    async def asyncSetUp(self):
        # The database is temporary. The dashboard writes the deletion into the
        # camera activity feed, and Config by default points at the REAL
        # storage/events.db: the test would leave records of nonexistent frames
        # in the owner's feed.
        self.dir = Path(tempfile.mkdtemp())
        self.dash = Dashboard(Config(db_path=self.dir / "events.db",
                                     snap_dir=self.dir / "snapshots"))
        self.asked = []

        async def ask(cmd, payload=None, timeout=6.0):
            self.asked.append((cmd, payload))
            return {"ok": True, "name": (payload or {}).get("name"),
                    "pairs": 7, "was_val": True, "had_label": True}

        self.dash.ask = ask
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_deletion_lands_in_the_activity_feed(self):
        """An irreversible action must stay in the feed: otherwise a week later
        there is nothing to check "where did that frame go" against."""
        await self.client.delete("/api/dataset/dog_20260906_120000_42", headers=GUARD)
        lines = [t for _, _, _, t in self.dash._camlog_buf]
        self.assertTrue(any("deleted from the dataset" in t for t in lines), lines)

    async def test_no_header_no_delete(self):
        """Loopback is reachable from ANY page open in the browser."""
        r = await self.client.delete("/api/dataset/dog_20260906_120000_42")
        self.assertEqual(r.status, 403)
        self.assertEqual(self.asked, [], "the command must not have reached the detector")

    async def test_foreign_origin_does_not_delete(self):
        r = await self.client.delete("/api/dataset/dog_20260906_120000_42",
                                     headers={**GUARD, "Origin": "http://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual(self.asked, [])

    async def test_deletion_reaches_the_detector(self):
        self.dash._snaps["ds:dog_20260906_120000_42"] = {"jpeg": b"\xff\xd8"}
        r = await self.client.delete("/api/dataset/dog_20260906_120000_42", headers=GUARD)
        self.assertEqual(r.status, 200)
        self.assertEqual(await r.json(), {"ok": True, "name": "dog_20260906_120000_42",
                                          "pairs": 7, "was_val": True, "had_label": True})
        self.assertEqual(self.asked,
                         [("dataset_delete", {"name": "dog_20260906_120000_42"})])
        self.assertNotIn("ds:dog_20260906_120000_42", self.dash._snaps,
                         "the frame cache must expire along with the file")

    async def test_detector_refusal_does_not_become_success(self):
        async def ask(cmd, payload=None, timeout=6.0):
            return {"ok": False, "error": "frame is not in the dataset"}
        self.dash.ask = ask
        r = await self.client.delete("/api/dataset/no_such_frame", headers=GUARD)
        self.assertEqual(r.status, 409)

    async def test_picture_route_is_not_hijacked(self):
        """DELETE /api/dataset/{name} must not break GET .../{name}.jpg."""
        async def ask(cmd, payload=None, timeout=6.0):
            self.asked.append((cmd, payload))
            return {"ok": False, "error": "no frame"}
        self.dash.ask = ask
        r = await self.client.get("/api/dataset/dog_20260906_120000_42.jpg")
        self.assertEqual(r.status, 404, "the frame handler must answer, not a 405")
        self.assertEqual(self.asked[0][0], "dataset_frame")


if __name__ == "__main__":
    unittest.main()
