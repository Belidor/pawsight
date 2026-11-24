"""Zone membership by the ground contact point.

The box centre lies: a dog standing by the far wall has its centre inside a
foreground zone. The middle of the box's bottom edge is where the animal
actually stands on the floor, so that is the point used.
"""
from __future__ import annotations

from .config import Zone


def ground_point(box: tuple[float, float, float, float]) -> tuple[float, float]:
    """(x1,y1,x2,y2) -> ground contact point."""
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2.0, y2)


def _in_polygon(pt: tuple[float, float], poly: list[tuple[int, int]]) -> bool:
    """Ray casting. Works for any polygon, including non-convex ones."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def zone_of(box: tuple[float, float, float, float], zones: list[Zone]) -> str | None:
    """Name of the first zone containing the ground contact point, else None."""
    pt = ground_point(box)
    for z in zones:
        if _in_polygon(pt, z.polygon):
            return z.name
    return None
