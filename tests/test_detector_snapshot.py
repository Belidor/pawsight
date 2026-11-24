"""The frame the detector hands out when a person asks for one.

Only `snapshot_jpeg` is tested, so the object is created without `__init__`:
the detector's constructor reaches for the PTZ and the tilt reference, which
have nothing to do with a snapshot.

The module needs OpenCV: it is in the native `.venv` (requirements.txt) but
not in the brain image, where OpenCV is deliberately absent. So without it
the test is skipped rather than failing.
"""
from __future__ import annotations

import time
import unittest
from collections import deque

try:
    import cv2
    import numpy as np

    from pawsight.detector import Detector
except ImportError:                     # brain image: no OpenCV needed here
    Detector = None


def ring(frame_, ts: float) -> list:
    """The detector's frame ring: (time, frame, raw detections)."""
    return [(ts, frame_, [])]


def detector(frames) -> "Detector":
    """A detector without its constructor: exactly one attribute is needed, the frame ring.

    The frame comes from the same ring the detection loop fills, so "stream
    not open" and "no frames yet" are the same state here: an empty ring.
    Both cases are kept as separate tests: they differ for a human even when
    the code path is the same.
    """
    det = Detector.__new__(Detector)
    det._ring = deque(frames, maxlen=6)
    return det


def frame(w: int = 640, h: int = 360):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, : w // 2] = (30, 60, 90)          # not a solid fill
    return img


@unittest.skipIf(Detector is None, "no OpenCV: this is the brain image")
class Snapshot(unittest.TestCase):
    def test_fresh_frame_comes_as_jpeg(self):
        jpeg, note = detector(ring(frame(), time.time())).snapshot_jpeg()
        self.assertIsNone(note)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"), "not a JPEG")
        back = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(back.shape, (360, 640, 3))

    def test_stream_not_open_says_so(self):
        """The brain came up before the detector: normal at startup."""
        jpeg, note = detector([]).snapshot_jpeg()
        self.assertIsNone(jpeg)
        self.assertIn("stream", note)

    def test_no_frames_yet_says_so(self):
        jpeg, note = detector([]).snapshot_jpeg()
        self.assertIsNone(jpeg)
        self.assertIn("frames", note)

    def test_old_frame_is_marked_not_passed_off_as_now(self):
        """The Xiaomi camera freezes for 6-19 s after a pan. Silently passing
        that off as "right now" would be lying exactly when the person is
        checking that everything at home is fine."""
        jpeg, note = detector(ring(frame(), time.time() - 9)).snapshot_jpeg()
        self.assertIsNotNone(jpeg)
        self.assertIn("9 s", note)

    def test_freshness_threshold_is_adjustable(self):
        _, note = detector(ring(frame(), time.time() - 1)).snapshot_jpeg(
            stale_after=0.5)
        self.assertIsNotNone(note)


if __name__ == "__main__":
    unittest.main()
