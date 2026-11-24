"""zones.yaml: the shape key of an anchored zone is a floor shape relative to
the anchor's floor contact point. A malformed shape must fail config loading
loudly instead of silently turning into "no zone"."""
import tempfile
import textwrap
import unittest
from pathlib import Path

from pawsight.anchors import AnchoredZone
from pawsight.config import Config
from pawsight.zones import _in_polygon


def load(text: str):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "zones.yaml"
        p.write_text(textwrap.dedent(text), encoding="utf-8")
        return Config._load_zones(p)


class ShapeParsing(unittest.TestCase):
    def test_shape_is_parsed_as_int_pairs(self):
        zones = load("""
            zones:
              water:
                label: "water"
                anchor:
                  class: bowl
                  select: left
                  shape: [[-10, -8], [-85, 37], [-112, 4]]
        """)
        self.assertEqual(zones[0].anchor_shape, ((-10, -8), (-85, 37), (-112, 4)))
        self.assertEqual(zones[0].anchor_select, "left")

    def test_without_shape_expand_still_works(self):
        zones = load("""
            zones:
              water:
                label: "water"
                anchor: {class: bowl, select: left, expand: [50, 40, 50, 110]}
        """)
        self.assertIsNone(zones[0].anchor_shape)
        self.assertEqual(zones[0].anchor_expand, (50, 40, 50, 110))

    def test_fewer_than_three_points_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load("""
                zones:
                  water:
                    label: "water"
                    anchor: {class: bowl, select: left, shape: [[0, 0], [10, 10]]}
            """)
        self.assertIn("water", str(cm.exception))

    def test_non_pair_vertex_is_rejected(self):
        with self.assertRaises(ValueError):
            load("""
                zones:
                  water:
                    label: "water"
                    anchor: {class: bowl, select: left, shape: [[0, 0], [10, 10], [5]]}
            """)

    def test_missing_file_means_no_zones(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Config._load_zones(Path(d) / "nope.yaml"), [])

    def test_example_zones_are_floor_shapes(self):
        """zones.example.yaml: three bowl zones, bowls first, all shapes on the floor."""
        root = Path(__file__).resolve().parent.parent
        zones = Config._load_zones(root / "zones.example.yaml")
        self.assertEqual([z.name for z in zones], ["bowls", "water", "food"])
        for z in zones:
            self.assertIsNotNone(z.anchor_shape, z.name)

    def test_room_fixture_zones_are_floor_shapes(self):
        """Room fixture: every zone has a floor shape, not a rectangle."""
        root = Path(__file__).resolve().parent.parent
        zones = {z.name: z for z in Config._load_zones(root / "tests" / "data" / "zones_room.yaml")}
        self.assertEqual(sorted(zones),
                         ["bowls", "carpet", "desk", "door", "food", "kitchen", "water"])
        for z in zones.values():
            self.assertIsNotNone(z.anchor_shape, z.name)
            self.assertGreaterEqual(len(z.anchor_shape), 3, z.name)


class ShippedZoneOrder(unittest.TestCase):
    """The order of zones in zones.yaml is not cosmetics, it is logic.

    Brain.observe takes the FIRST non-hint from the list, and the visit
    trigger is deliberately wide and overlaps the rug and the kitchen. If
    bowls slides down, a dog at the bowls starts counting as "on the rug",
    visits stop being counted, and there will be no error at all.
    """

    def test_bowls_is_first(self):
        root = Path(__file__).resolve().parent.parent
        names = [z.name for z in Config._load_zones(root / "tests" / "data" / "zones_room.yaml")]
        self.assertEqual(names[0], "bowls", f"zone order: {names}")

    def test_room_zones_are_anchored_to_the_pair(self):
        """Room zones are measured from the pair of bowls, not from one of them.

        With select: left/right the shape would follow the water bowl swap on
        the setup page, while the room does not change because of it.
        """
        root = Path(__file__).resolve().parent.parent
        zones = {z.name: z for z in Config._load_zones(root / "tests" / "data" / "zones_room.yaml")}
        for name in ("door", "carpet", "kitchen", "desk"):
            self.assertEqual(zones[name].anchor_select, "all", name)
            self.assertEqual(zones[name].anchor_class, "bowl", name)


class ShapeParsingBranches(unittest.TestCase):
    def test_scalar_shape_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            load("""
                zones:
                  water:
                    label: "water"
                    anchor: {class: bowl, select: left, shape: 12}
            """)
        self.assertIn("must be a list", str(cm.exception))

    def test_bool_vertex_is_rejected(self):
        """YAML reads true as a bool, and in Python bool is a subclass of int."""
        with self.assertRaises(ValueError):
            load("""
                zones:
                  water:
                    label: "water"
                    anchor: {class: bowl, select: left, shape: [[true, 1], [0, 0], [1, 1]]}
            """)

    def test_float_offsets_are_rounded_to_pixels(self):
        zones = load("""
            zones:
              water:
                label: "water"
                anchor: {class: bowl, select: left, shape: [[0.4, 0.6], [10.5, -0.5], [3.49, 3.51]]}
        """)
        self.assertEqual(zones[0].anchor_shape, ((0, 1), (10, 0), (3, 4)))


class ShippedTriggerCoversHints(unittest.TestCase):
    """bowls is the visit trigger: a dog in the water or food strip MUST also
    be "at the bowls", otherwise the visit is not counted (Brain.observe
    skips hints). Each strip is tied to its own bowl and the trigger to the
    pair, so detector noise on the box bottoms shifts them relative to each
    other; the trigger's margin must survive that."""

    LEFT = (446.7, 158.6, 471.2, 174.9)
    RIGHT = (469.3, 166.4, 497.1, 184.5)

    def test_hint_vertices_stay_inside_the_trigger_under_detector_noise(self):
        root = Path(__file__).resolve().parent.parent
        zones = Config._load_zones(root / "tests" / "data" / "zones_room.yaml")
        leaks = []
        for dyl in (-6, 0, 6):
            for dyr in (-6, 0, 6):
                for dx in (-5, 0, 5):
                    left = (self.LEFT[0] + dx, self.LEFT[1] + dyl, self.LEFT[2] + dx, self.LEFT[3] + dyl)
                    right = (self.RIGHT[0], self.RIGHT[1] + dyr, self.RIGHT[2], self.RIGHT[3] + dyr)
                    anchors = {z.name: AnchoredZone.from_zone(z) for z in zones}
                    for a in anchors.values():
                        a.update([("bowl", .9, left), ("bowl", .9, right)], 1.0)
                    trigger = anchors["bowls"].polygon(1.0)
                    self.assertIsNotNone(trigger, (dyl, dyr, dx))
                    for hint in ("water", "food"):
                        for pt in anchors[hint].polygon(1.0):
                            if not _in_polygon(pt, trigger):
                                leaks.append((dyl, dyr, dx, hint, pt))
        self.assertEqual(leaks, [], f"hint vertices outside the trigger: {leaks[:8]} ({len(leaks)} total)")
