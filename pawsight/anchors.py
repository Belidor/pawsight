"""Zones tied to anchors in the frame rather than to fixed pixels.

The camera pans, so "at the bowl" is not a rectangle with constant coordinates
but an area around a bowl actually found. YOLO reliably detects them as the
bowl class, so the zone moves together with the camera.

When there are several anchors (two bowls), the right one is chosen by their
relative ORDER, not by absolute coordinates. Panning shifts the picture
horizontally but preserves the left-to-right order of objects: the camera
cannot mirror the scene. So "left"/"right" are robust to panning, and on this
scene reliable: the bowls are about 100 px apart in X, while their boxes differ
by only ~10 px in Y (so depth here is within detector noise and cannot serve as
a feature).

If the anchor is not visible in the frame (the camera is looking elsewhere),
the zone is treated as absent. That is more honest than applying a stale polygon.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

Det = tuple[str, float, tuple[float, float, float, float]]


def _cx(box: tuple[float, float, float, float]) -> float:
    return (box[0] + box[2]) / 2.0


def _depth_key(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """Smaller means FURTHER: a higher box CENTRE and a smaller area.

    Depth cannot be measured by the box bottom (y2): it depends on the object's
    height and on what covers it. In the bowl scene the bottoms coincide.
    """
    x1, y1, x2, y2 = box
    return ((y1 + y2) / 2.0, (x2 - x1) * (y2 - y1))


def _iou(a, b) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ar = (a[2] - a[0]) * (a[3] - a[1])
    br = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (ar + br - inter)


def _dedupe(boxes: list, iou_thresh: float = 0.5) -> list:
    """At low conf YOLO outputs several boxes per object.

    Without merging, "two bowls" become five and instance selection breaks.
    """
    out: list = []
    for b in sorted(boxes, key=lambda z: -((z[2] - z[0]) * (z[3] - z[1]))):
        if all(_iou(b, o) < iou_thresh for o in out):
            out.append(b)
    return out


@dataclass
class AnchoredZone:
    name: str
    label: str
    anchor_class: str
    min_conf: float = 0.20
    expand: tuple[int, int, int, int] = (60, 40, 60, 120)
    memory_sec: float = 20.0
    # left/right: the leftmost/rightmost instance. Robust to camera pan,
    #             because panning preserves relative order.
    # far/near:   the furthest/nearest by box centre. Only usable if the
    #             objects really are separated in depth.
    # all:        the bounding box of all found.
    select: str = "all"
    # A shape ON THE FLOOR: vertices as offsets from the anchor's floor contact
    # point ((x1+x2)/2, y2) in detector frame pixels. When set, the expand
    # rectangle is not built. Drawn by the owner (zones.yaml): the dog stands on
    # the floor in front of the bowl, not on the wall above it, where the
    # rectangle reached.
    shape: tuple[tuple[int, int], ...] | None = None

    _poly: list[tuple[int, int]] | None = field(default=None, repr=False)
    _box: tuple[float, float, float, float] | None = field(default=None, repr=False)
    _seen_ts: float = field(default=0.0, repr=False)

    @classmethod
    def from_zone(cls, z) -> "AnchoredZone":
        """From a zones.yaml entry (config.Zone). One place for the detector and the
        tracker, so a new config key cannot reach one and be forgotten in the other."""
        return cls(name=z.name, label=z.label, anchor_class=z.anchor_class,
                   min_conf=z.anchor_min_conf, expand=z.anchor_expand,
                   memory_sec=z.anchor_memory_sec, select=z.anchor_select,
                   shape=z.anchor_shape)

    def update(self, detections: list[Det], ts: float) -> None:
        boxes = [b for n, c, b in detections if n == self.anchor_class and c >= self.min_conf]
        if not boxes:
            return

        boxes = _dedupe(boxes)
        # Bowls only as a PAIR and only CLOSE TOGETHER: the water bowl and the
        # food bowl sit on one mat ~100 px apart. A single "bowl" or two "bowls"
        # at opposite ends of the room are cups and vases (verified in the living
        # room: the drinking zone stretched across the whole couch).
        if self.anchor_class == "bowl":
            if len(boxes) < 2:
                return
            boxes = sorted(boxes, key=lambda b: (b[0] + b[2]) / 2)
            best = None
            for i in range(len(boxes) - 1):
                a, b = boxes[i], boxes[i + 1]
                dx = abs((a[0] + a[2]) / 2 - (b[0] + b[2]) / 2)
                dy = abs(a[3] - b[3])
                w = max(a[2] - a[0], b[2] - b[0])
                if dx <= 4 * w and dy <= 1.5 * (a[3] - a[1]):
                    best = [a, b]
                    break
            if best is None:
                return
            boxes = best

        if self.select == "left":
            boxes = [min(boxes, key=_cx)]
        elif self.select == "right":
            boxes = [max(boxes, key=_cx)]
        elif self.select == "far":
            boxes = [min(boxes, key=_depth_key)]
        elif self.select == "near":
            boxes = [max(boxes, key=_depth_key)]

        x1 = min(b[0] for b in boxes); y1 = min(b[1] for b in boxes)
        x2 = max(b[2] for b in boxes); y2 = max(b[3] for b in boxes)
        self._box = (x1, y1, x2, y2)
        if self.shape:
            gx, gy = (x1 + x2) / 2.0, y2
            self._poly = [(int(gx + dx), int(gy + dy)) for dx, dy in self.shape]
        else:
            l, u, r, d = self.expand
            self._poly = [
                (int(x1 - l), int(y1 - u)), (int(x2 + r), int(y1 - u)),
                (int(x2 + r), int(y2 + d)), (int(x1 - l), int(y2 + d)),
            ]
        self._seen_ts = ts

    def polygon(self, ts: float | None = None, *,
                current_only: bool = False) -> list[tuple[int, int]] | None:
        ts = ts if ts is not None else time.time()
        if self._poly is None or ts - self._seen_ts > self.memory_sec:
            return None
        # Zone memory helps when the dog covers the bowls, but odometry can only
        # use a detection from the same frame: the camera may have panned.
        if current_only and self._seen_ts != ts:
            return None
        return self._poly

    def anchor_box(self, ts: float | None = None, *,
                   current_only: bool = False) -> tuple[float, float, float, float] | None:
        """The anchor box (the selected bowl or pair) with the same memory as the polygon.

        Needed by the patrol odometry: the centre of the floor shape is shifted
        towards the mat and cannot serve as home; the bowl pair itself is home.
        """
        if self.polygon(ts, current_only=current_only) is None:
            return None
        return self._box

    @property
    def fresh(self) -> bool:
        return self._poly is not None


def anchors_for(zones) -> dict[str, AnchoredZone]:
    """Anchored zones from the config, built by one piece of code for the detector and the tracker.

    Two copies of this assembly had already diverged: a config key that reaches
    one consumer and is forgotten in the other breaks silently: the tracker would
    count rectangles, the detector would draw strips, and nobody would notice.
    """
    return {z.name: AnchoredZone.from_zone(z) for z in zones if z.is_anchored}
