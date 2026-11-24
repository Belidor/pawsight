"""Cheap heuristics over a box's history.

The VLM gets static scenes wrong (a sleeping dog is easily described as "lying
and watching"), while geometry does not. So the state is computed here and the
VLM is only asked for a verbal description. If the two disagree, the spec
requires showing both, not silently picking one.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

# Thresholds in box diagonals per second, so they depend neither on the
# resolution nor on whether the dog is close to the camera or far away.
STILL = 0.15
WALK = 1.2


@dataclass
class TrackHistory:
    """History of one track: centres, timestamps, box shape."""

    maxlen: int = 40
    pts: deque = field(default_factory=lambda: deque(maxlen=40))

    def add(self, box: tuple[float, float, float, float], ts: float) -> None:
        x1, y1, x2, y2 = box
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        diag = math.hypot(x2 - x1, y2 - y1) or 1.0
        aspect = (x2 - x1) / max(y2 - y1, 1.0)
        self.pts.append((ts, cx, cy, diag, aspect))

    def speed(self, window: float = 2.0) -> float | None:
        """Speed of the centre in box diagonals per second."""
        if len(self.pts) < 2:
            return None
        now = self.pts[-1][0]
        sel = [p for p in self.pts if now - p[0] <= window]
        if len(sel) < 2:
            return None
        dist = 0.0
        for a, b in zip(sel, sel[1:]):
            dist += math.hypot(b[1] - a[1], b[2] - a[2])
        dt = sel[-1][0] - sel[0][0]
        if dt <= 0:
            return None
        return (dist / dt) / sel[-1][3]

    def aspect(self) -> float | None:
        if not self.pts:
            return None
        n = min(5, len(self.pts))
        return sum(p[4] for p in list(self.pts)[-n:]) / n


def posture(aspect: float | None) -> str | None:
    """A wide, low box means lying; a tall one means standing."""
    if aspect is None:
        return None
    if aspect >= 1.5:
        return "lying"
    if aspect <= 0.9:
        return "standing"
    return None


def state(h: TrackHistory) -> str:
    """Final state from motion and shape."""
    v = h.speed()
    if v is None:
        return "unknown"
    if v < STILL:
        return posture(h.aspect()) or "still"
    if v < WALK:
        return "walking"
    return "running"
