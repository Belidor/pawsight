"""Floor zones: the polygon is built from the anchor's floor contact point using
the shape the owner drew, not by stretching the bowl box into a rectangle."""
import unittest

from pathlib import Path

from pawsight.anchors import AnchoredZone, anchors_for
from pawsight.config import Zone

ROOT = Path(__file__).resolve().parent.parent

LEFT = ("bowl", .9, (440.0, 150.0, 470.0, 175.0))
RIGHT = ("bowl", .9, (470.0, 160.0, 500.0, 185.0))
SHAPE = ((-10, -8), (-85, 37), (-112, 4), (-25, -18))


class ShapePolygon(unittest.TestCase):
    def test_left_bowl_shape_is_offset_from_its_ground_point(self):
        z = AnchoredZone("water", "water bowl", "bowl", select="left", shape=SHAPE)
        z.update([LEFT, RIGHT], 10)
        gx, gy = 455, 175          # ((440+470)/2, y2) of the left bowl
        self.assertEqual(z.polygon(10), [(gx + dx, gy + dy) for dx, dy in SHAPE])

    def test_right_bowl_uses_its_own_ground_point(self):
        z = AnchoredZone("food", "food bowl", "bowl", select="right", shape=SHAPE)
        z.update([LEFT, RIGHT], 10)
        gx, gy = 485, 185
        self.assertEqual(z.polygon(10), [(gx + dx, gy + dy) for dx, dy in SHAPE])

    def test_all_uses_ground_point_of_the_pair(self):
        z = AnchoredZone("bowls", "at the bowls", "bowl", select="all", shape=SHAPE)
        z.update([LEFT, RIGHT], 10)
        gx, gy = 470, 185          # bounding box 440..500, bottom 185
        self.assertEqual(z.polygon(10), [(gx + dx, gy + dy) for dx, dy in SHAPE])

    def test_without_shape_the_rectangle_stays(self):
        """Regression: a zone without shape behaves as before."""
        z = AnchoredZone("water", "water bowl", "bowl", select="left", expand=(50, 40, 50, 110))
        z.update([LEFT, RIGHT], 10)
        self.assertEqual(z.polygon(10), [(390, 110), (520, 110), (520, 285), (390, 285)])

    def test_shape_wins_over_expand(self):
        z = AnchoredZone("water", "water bowl", "bowl", select="left",
                         expand=(50, 40, 50, 110), shape=SHAPE)
        z.update([LEFT, RIGHT], 10)
        # The expand rectangle also has four vertices, so compare the vertices themselves.
        self.assertEqual(z.polygon(10), [(455 + dx, 175 + dy) for dx, dy in SHAPE])

    def test_vertices_are_ints_even_for_fractional_boxes(self):
        z = AnchoredZone("water", "water bowl", "bowl", select="left", shape=SHAPE)
        z.update([("bowl", .9, (440.4, 150.2, 470.9, 175.7)), RIGHT], 10)
        for x, y in z.polygon(10):
            self.assertIsInstance(x, int)
            self.assertIsInstance(y, int)


class AnchorBox(unittest.TestCase):
    """The anchor box is needed by the patrol odometry: the centre of the floor
    shape is shifted towards the mat and cannot serve as home."""

    def setUp(self):
        self.z = AnchoredZone("bowls", "at the bowls", "bowl", select="all",
                              memory_sec=20, shape=SHAPE)
        self.z.update([LEFT, RIGHT], 10)

    def test_box_of_the_selected_anchor(self):
        self.assertEqual(self.z.anchor_box(10), (440.0, 150.0, 500.0, 185.0))

    def test_box_is_remembered_as_long_as_the_polygon(self):
        self.assertIsNotNone(self.z.anchor_box(29))
        self.assertIsNone(self.z.anchor_box(31))

    def test_current_only_rejects_memory(self):
        self.assertIsNotNone(self.z.anchor_box(10, current_only=True))
        self.assertIsNone(self.z.anchor_box(15, current_only=True))


class FromZone(unittest.TestCase):
    def test_carries_shape_and_select(self):
        zone = Zone(name="water", label="water bowl", anchor_class="bowl",
                    anchor_select="left", anchor_shape=SHAPE, anchor_memory_sec=7.0)
        a = AnchoredZone.from_zone(zone)
        a.update([LEFT, RIGHT], 1)
        self.assertEqual(a.select, "left")
        self.assertEqual(a.memory_sec, 7.0)
        self.assertEqual(a.shape, SHAPE)
        self.assertEqual(a.polygon(1), [(455 + dx, 175 + dy) for dx, dy in SHAPE])

    def test_without_shape_builds_the_old_rectangle(self):
        zone = Zone(name="water", label="water bowl", anchor_class="bowl",
                    anchor_select="left", anchor_expand=(50, 40, 50, 110))
        a = AnchoredZone.from_zone(zone)
        a.update([LEFT, RIGHT], 1)
        self.assertEqual(a.polygon(1), [(390, 110), (520, 110), (520, 285), (390, 285)])


class AnchorsFor(unittest.TestCase):
    """The detector and the tracker build zones with ONE piece of code: a config
    key that reaches one and is forgotten in the other would break silently: the
    tracker would count rectangles, the detector would draw strips."""

    def test_builds_only_anchored_zones_with_their_shapes(self):
        zones = [
            Zone(name="water", label="water bowl", anchor_class="bowl", anchor_select="left",
                 anchor_shape=SHAPE),
            Zone(name="fixed", label="corner", polygon=[(0, 0), (10, 0), (10, 10)]),
        ]
        anchors = anchors_for(zones)
        self.assertEqual(list(anchors), ["water"])
        anchors["water"].update([LEFT, RIGHT], 1)
        self.assertEqual(anchors["water"].polygon(1), [(455 + dx, 175 + dy) for dx, dy in SHAPE])

    def test_detector_goes_through_the_helper(self):
        """Tripwire: an explicit AnchoredZone(...) in the detector is a second copy
        of the assembly again, which will diverge from anchors_for on the next key.
        (The second consumer, pawsight/tracker.py, was dead code and was removed.)"""
        for mod in ("pawsight/detector.py",):
            src = (ROOT / mod).read_text(encoding="utf-8")
            self.assertIn("anchors_for(", src, mod)
            self.assertNotIn("AnchoredZone(", src, mod)
