"""Validation of labels coming from the dashboard. stdlib only: both sides need
it, the detector before writing and the brain before sending the command.

Labels arrive NORMALISED: cx, cy, w, h in [0,1]. That is already a YOLO line,
so that is exactly what must be checked, not some intermediate representation.
"""
from __future__ import annotations

import math

from .labels import DATASET_IDS

# Under half a percent of the frame is a mis-click, not a box.
MIN_SIDE = 0.005


def validate_boxes(boxes) -> tuple[bool, str | None]:
    if not isinstance(boxes, list):
        return False, "labels must be a list"
    if len(boxes) > 64:
        return False, "too many boxes"
    for b in boxes:
        if not isinstance(b, dict):
            return False, "a box must be an object"
        cls = b.get("cls")
        if cls not in DATASET_IDS:
            # A foreign class crashes ultralytics' dataset scan, but only after
            # the person has waited hours for training.
            return False, f"unknown class: {cls!r}"
        try:
            v = [float(b[k]) for k in ("cx", "cy", "w", "h")]
        except (KeyError, TypeError, ValueError):
            return False, "the box has no cx/cy/w/h or they are not numbers"
        if not all(math.isfinite(x) for x in v):
            return False, "non-numeric coordinates"
        if max(v) > 1.5:
            # A sure sign someone sent pixels instead of fractions.
            return False, ("coordinates must be normalised [0..1]; "
                           "looks like pixels were sent")
        if v[2] < MIN_SIDE or v[3] < MIN_SIDE:
            return False, "degenerate box"
    return True, None


def to_yolo(boxes) -> str:
    """Label lines. Clamped to the frame: the box may have been dragged past the edge."""
    lines = []
    for b in boxes:
        cx, cy = float(b["cx"]), float(b["cy"])
        w, h = float(b["w"]), float(b["h"])
        x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
        x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
        cx, cy, w, h = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
        if w < MIN_SIDE or h < MIN_SIDE:
            continue
        lines.append(f"{DATASET_IDS[b['cls']]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    # An empty file is a legitimate negative example: YOLO reads it as "background".
    return "\n".join(lines) + ("\n" if lines else "")
