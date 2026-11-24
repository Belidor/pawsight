"""Detects motion of the CAMERA itself (not of objects in the frame).

The camera pans. While it moves, everything in the frame shifts and the speed
heuristic reports "running" for a sleeping dog. So no events are emitted while
the camera moves and for a while after, and zones are re-anchored.

HOW a pan is told apart from object motion. The first version took the median
flow over the whole frame, and a large figure close by (a person, the dog
itself) looked like a pan: following froze and search stayed silent for
30-60 s. Now:
  * points inside dog and person boxes are excluded, only background counts;
  * a pan shifts the whole background EQUALLY (a rigid field), object motion
    does not; a small spread of vectors around the median is required.
"""
from __future__ import annotations

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# Median background shift above which the camera is considered moving
# (in pixels of the 1280-wide detection frame between processed frames).
# A real pan step gives ~50 px/frame at 4 fps, a fivefold margin.
MOVE_PX = 10.0
# Spread (MAD) of vectors around the median: a fraction of the median magnitude + a constant.
RIGID_FRAC = 0.4
RIGID_MIN_PX = 4.0
MIN_BG_POINTS = 12
# Seconds after stopping during which the picture is not yet considered settled.
SETTLE_SEC = 2.0
WORK_W = 480


class CameraMotion:
    def __init__(self, move_px: float = MOVE_PX, settle_sec: float = SETTLE_SEC) -> None:
        self.move_px = move_px
        self.settle_sec = settle_sec
        self._prev_gray: np.ndarray | None = None
        self._last_move_ts: float = 0.0
        self._moving = False
        self.last_info: dict = {}

    def update(self, frame, ts: float, exclude_boxes: list | None = None) -> bool:
        """Return True if the camera is moving right now.

        exclude_boxes: [x1,y1,x2,y2] boxes in frame coordinates (dog, people);
        points inside them are left out of the estimate.
        """
        if cv2 is None:
            return False
        h, w = frame.shape[:2]
        scale = w / WORK_W
        small = cv2.resize(frame, (WORK_W, int(h / scale)))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if self._prev_gray is None or self._prev_gray.shape != gray.shape:
            self._prev_gray = gray
            return False

        mask = np.full(gray.shape, 255, np.uint8)
        for b in exclude_boxes or []:
            x1, y1, x2, y2 = b
            bw, bh = (x2 - x1) * 0.1, (y2 - y1) * 0.1
            xa, ya = int(max(0, (x1 - bw) / scale)), int(max(0, (y1 - bh) / scale))
            xb, yb = int(min(w, (x2 + bw)) / scale), int(min(h, (y2 + bh)) / scale)
            mask[ya:yb, xa:xb] = 0

        pts = cv2.goodFeaturesToTrack(self._prev_gray, maxCorners=150, qualityLevel=0.01,
                                      minDistance=8, mask=mask)
        if pts is None or len(pts) < MIN_BG_POINTS:
            # almost no background visible (dog fills the frame): use everything, but require rigidity
            pts = cv2.goodFeaturesToTrack(self._prev_gray, maxCorners=150, qualityLevel=0.01, minDistance=8)
        moving = False
        info = {}
        if pts is not None and len(pts) >= MIN_BG_POINTS:
            nxt, status, _ = cv2.calcOpticalFlowPyrLK(self._prev_gray, gray, pts, None)
            if nxt is not None and status is not None:
                ok = status.ravel() == 1
                if ok.sum() >= MIN_BG_POINTS:
                    d = (nxt[ok] - pts[ok]).reshape(-1, 2) * scale
                    med = np.median(d, axis=0)
                    mag = float(np.hypot(*med))
                    mad = float(np.median(np.hypot(d[:, 0] - med[0], d[:, 1] - med[1])))
                    rigid = mad < RIGID_FRAC * mag + RIGID_MIN_PX
                    moving = mag > self.move_px and rigid
                    info = {"mag": round(mag, 1), "mad": round(mad, 1), "n": int(ok.sum()), "rigid": rigid}

        self.last_info = info
        self._prev_gray = gray
        if moving:
            self._last_move_ts = ts
        self._moving = moving
        return moving

    @property
    def moving(self) -> bool:
        return self._moving

    def settled(self, ts: float) -> bool:
        """The picture has settled: the camera is still and settle_sec has passed since it stopped."""
        return not self._moving and (ts - self._last_move_ts) > self.settle_sec
