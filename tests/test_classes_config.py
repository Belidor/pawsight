"""PAWSIGHT_CLASSES: extra classes on top of dog/bowl/person, as in the shared model."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pawsight import labels
from pawsight.labels import BASE_CLASSES, parse_classes


class ParseClasses(unittest.TestCase):
    def test_empty_is_base(self):
        self.assertEqual(parse_classes(None), BASE_CLASSES)
        self.assertEqual(parse_classes("  "), BASE_CLASSES)

    def test_shared_preset_names_match_shared_model(self):
        # names, not order, must match shared-dataset/data.yaml: training and
        # the detector resolve classes by NAME
        got = parse_classes("shared")
        self.assertEqual(got[:3], ("dog", "bowl", "person"))
        self.assertEqual(set(got), {"dog", "person", "bowl", "couch", "bed", "chair",
                                    "dining table", "rug"})

    def test_list_keeps_order_and_normalises_spaces(self):
        self.assertEqual(parse_classes("dog, bowl,person ,  dining   table,rug"),
                         ("dog", "bowl", "person", "dining table", "rug"))

    def test_must_start_with_base_in_order(self):
        # label files store indices 0,1,2 for these: reordering would silently
        # turn every bowl on disk into a person
        for bad in ("dog,person,bowl,rug", "bowl,dog,person", "dog,bowl", "rug,dog,bowl,person"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_classes(bad)

    def test_rejects_duplicates_and_odd_names(self):
        for bad in ("dog,bowl,person,rug,rug", "dog,bowl,person,a:b", "dog,bowl,person," + "x" * 40):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_classes(bad)


try:
    from pawsight import train
except ImportError:        # brain image: no OpenCV/yaml stack
    train = None


@unittest.skipIf(train is None, "train.py needs the detector's dependencies")
class TrainClassCheck(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def _pairs(self, rows):
        out = []
        for i, text in enumerate(rows):
            img, lbl = self.d / f"f{i}.jpg", self.d / f"f{i}.txt"
            img.write_bytes(b"")
            lbl.write_text(text)
            out.append((img, lbl))
        return out

    def test_index_outside_list_is_refused(self):
        pairs = self._pairs(["0 .5 .5 .1 .1\n", "7 .5 .5 .1 .1\n"])
        with mock.patch.object(train, "DATASET_CLASSES", dict(enumerate(BASE_CLASSES))), \
             self.assertRaises(SystemExit) as e:
            train._check_class_ids(pairs)
        self.assertIn("f1.txt", str(e.exception))

    def test_frames_without_extra_classes_are_reported(self):
        shared = dict(enumerate(parse_classes("shared")))
        pairs = self._pairs(["0 .5 .5 .1 .1\n", "0 .5 .5 .1 .1\n3 .2 .2 .1 .1\n"])
        with mock.patch.object(train, "DATASET_CLASSES", shared), \
             mock.patch.object(train, "EXTRA_CLASSES", tuple(shared.values())[3:]), \
             self.assertLogs("pawsight.train", "WARNING") as logs:
            train._check_class_ids(pairs)
        self.assertIn("1 of 2 frames", logs.output[0])


class LabelsModule(unittest.TestCase):
    def test_coco_map_follows_the_list(self):
        self.assertEqual(labels.COCO_TO_DATASET[16], labels.DATASET_IDS["dog"])
        self.assertEqual(labels.COCO_TO_DATASET[45], labels.DATASET_IDS["bowl"])


if __name__ == "__main__":
    unittest.main()
