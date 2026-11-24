"""Zone editor: the owner draws zones and picks which send Telegram.

The file is the contract between the editor, the detector and the brain, so
these tests go through the real zones.example.yaml and the real loader.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from pawsight import setup, zone_edit
from pawsight.anchors import AnchoredZone
from pawsight.config import Config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "zones.example.yaml"
FRAME = (640, 360)
PAIR = [(447, 159, 471, 175), (469, 166, 497, 185)]


def editor_view(raw: dict) -> dict:
    """What the page sends back when the owner changes nothing."""
    d = zone_edit.describe(raw)
    return {"notify_outside": d["notify_outside"],
            "zones": [{k: v for k, v in z.items() if k != "select"} for z in d["zones"]]}


class ZoneEdit(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.path = d / "zones.yaml"
        shutil.copy(EXAMPLE, self.path)
        self.raw = yaml.safe_load(self.path.read_text())

    def test_describe_marks_builtin_zones(self):
        d = zone_edit.describe(self.raw)
        self.assertEqual([z["name"] for z in d["zones"]], ["bowls", "water", "food"])
        self.assertTrue(all(z["kind"] == "builtin" and z["notify"] for z in d["zones"]))
        self.assertTrue(d["notify_outside"])

    def test_unchanged_edit_keeps_geometry(self):
        new = zone_edit.save(self.path, editor_view(self.raw), FRAME)
        for name in ("bowls", "water", "food"):
            self.assertEqual(new["zones"][name]["anchor"], self.raw["zones"][name]["anchor"])
        self.assertEqual(yaml.safe_load(self.path.read_text()), new)
        self.assertEqual(self.path.with_name("zones.yaml.bak").read_text(), EXAMPLE.read_text())

    def test_header_comments_survive(self):
        zone_edit.save(self.path, editor_view(self.raw), FRAME)
        text = self.path.read_text()
        self.assertTrue(text.startswith("# The camera PANS AND TILTS"))
        self.assertIn("# WATER BOWL = LEFT", text)

    def test_new_zones_and_notify_flags(self):
        edit = editor_view(self.raw)
        edit["zones"][1]["notify"] = False
        edit["notify_outside"] = False
        edit["zones"].append({"label": "By the door", "notify": True, "kind": "bowls",
                              "shape": [[-262, -104], [-190, -103], [-186, -56], [-274, -52]]})
        edit["zones"].append({"label": "Sofa", "notify": False, "kind": "fixed",
                              "polygon": [[10, 10], [200, 10], [200, 120]]})
        zone_edit.save(self.path, edit, FRAME)

        cfg_zones = Config._load_zones(self.path)
        by = {z.name: z for z in cfg_zones}
        self.assertEqual([z.name for z in cfg_zones], ["bowls", "water", "food", "by_the_door", "sofa"])
        self.assertFalse(by["water"].notify)
        self.assertTrue(by["food"].notify)
        self.assertEqual(by["by_the_door"].anchor_class, "bowl")
        self.assertEqual(by["by_the_door"].anchor_select, "all")
        self.assertEqual(by["by_the_door"].anchor_shape[0], (-262, -104))
        self.assertFalse(by["sofa"].notify)
        self.assertEqual(by["sofa"].polygon[0], (10, 10))
        self.assertFalse(Config._load_notify_outside(self.path))

        # Read back by the editor: the drawn zones keep their kind.
        kinds = {z["name"]: z["kind"] for z in zone_edit.describe(yaml.safe_load(self.path.read_text()))["zones"]}
        self.assertEqual(kinds["by_the_door"], "bowls")
        self.assertEqual(kinds["sofa"], "fixed")

    def test_builtin_zones_cannot_be_deleted_or_reshaped(self):
        edit = editor_view(self.raw)
        edit["zones"] = edit["zones"][1:]
        with self.assertRaises(zone_edit.ZoneEditError):
            zone_edit.merge(self.raw, edit, FRAME)
        edit = editor_view(self.raw)
        edit["zones"][0]["shape"] = [[0, 0], [1, 0], [0, 1]]
        new = zone_edit.merge(self.raw, edit, FRAME)
        self.assertEqual(new["zones"]["bowls"]["anchor"], self.raw["zones"]["bowls"]["anchor"])

    def test_builtin_zones_stay_first(self):
        edit = editor_view(self.raw)
        edit["zones"].insert(0, {"label": "rug", "kind": "fixed",
                                 "polygon": [[0, 0], [50, 0], [50, 50]]})
        new = zone_edit.merge(self.raw, edit, FRAME)
        self.assertEqual(list(new["zones"]), ["bowls", "water", "food", "rug"])

    def test_bad_input_is_refused(self):
        bad = [
            {"label": "", "kind": "fixed", "polygon": [[0, 0], [1, 0], [0, 1]]},
            {"label": "x", "kind": "fixed", "polygon": [[0, 0], [1, 0]]},
            {"label": "x", "kind": "fixed", "polygon": [[0, 0], [1, 0], [0, 999]]},
            {"label": "x", "kind": "fixed", "polygon": [[0, 0], [1, 0], ["a", 1]]},
            {"label": "x", "kind": "magic", "polygon": [[0, 0], [1, 0], [0, 1]]},
            {"label": "x", "name": "../etc", "kind": "fixed", "polygon": [[0, 0], [1, 0], [0, 1]]},
        ]
        for z in bad:
            with self.subTest(z=z):
                edit = editor_view(self.raw)
                edit["zones"].append(z)
                with self.assertRaises(zone_edit.ZoneEditError):
                    zone_edit.save(self.path, edit, FRAME)
                self.assertEqual(self.path.read_text(), EXAMPLE.read_text(), "file untouched")

    def test_duplicate_labels_get_distinct_ids(self):
        edit = editor_view(self.raw)
        for _ in range(2):
            edit["zones"].append({"label": "Rug", "kind": "fixed",
                                  "polygon": [[0, 0], [50, 0], [50, 50]]})
        new = zone_edit.merge(self.raw, edit, FRAME)
        self.assertIn("rug", new["zones"])
        self.assertIn("rug_2", new["zones"])

    def test_setup_page_can_still_swap_bowls(self):
        """The setup page edits the TEXT of zones.yaml; it must still work on
        a file the editor rewrote."""
        zone_edit.save(self.path, editor_view(self.raw), FRAME)
        setup.set_water_bowl("right", path=self.path, today="2026-11-24")
        z = yaml.safe_load(self.path.read_text())["zones"]
        self.assertEqual(z["water"]["anchor"]["select"], "right")
        self.assertEqual(z["food"]["anchor"]["select"], "left")

    def test_save_writes_in_place(self):
        """Docker mounts this single file into the brain: a rename would leave
        the container looking at the old inode."""
        ino = self.path.stat().st_ino
        zone_edit.save(self.path, editor_view(self.raw), FRAME)
        self.assertEqual(self.path.stat().st_ino, ino)

    def test_pair_point_matches_anchored_zone(self):
        p = zone_edit.pair_point([list(b) for b in PAIR])
        self.assertEqual(p, (472, 185))
        # A drawn offset lands where the detector later puts it.
        shape = ((-10, -5), (10, -5), (0, 10))
        a = AnchoredZone(name="t", label="", anchor_class="bowl", select="all", shape=shape)
        a.update([("bowl", 0.9, b) for b in PAIR], 1.0)
        self.assertEqual(a.polygon(1.0), [(p[0] + dx, p[1] + dy) for dx, dy in shape])
        self.assertIsNone(zone_edit.pair_point([list(PAIR[0])]))


class NotifyGate(unittest.TestCase):
    def test_brain_follows_zone_switches(self):
        from pawsight import brain as brain_mod

        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        cfg = Config(db_path=d / "events.db", snap_dir=d)
        raw = yaml.safe_load(EXAMPLE.read_text())
        raw["zones"]["water"]["notify"] = False
        raw["notify_outside_zones"] = False
        cfg.apply_zones(raw)
        b = brain_mod.Brain(cfg)
        self.addCleanup(b.close, 2)
        self.assertFalse(b._wants_telegram("water"))
        self.assertTrue(b._wants_telegram("food"))
        self.assertFalse(b._wants_telegram(None))
        self.assertTrue(b._wants_telegram("no_such_zone"))

    def test_old_files_notify_by_default(self):
        zones = Config.zones_from_raw(yaml.safe_load(EXAMPLE.read_text()))
        self.assertTrue(all(z.notify for z in zones))


if __name__ == "__main__":
    unittest.main()
