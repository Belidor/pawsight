"""Label review queue: what to show a human first.

Auto-labelling places boxes from the model's own predictions, so a frame
enters the dataset with the model's mistakes, and they go into training as
ground truth. Measured on the live dataset: of 186 frames, 93 had ONLY the dog
labelled, and among them were boxes on an office chair's base and on empty
floor, while people in the frame were not labelled at all.

The module does not decide what is correct; it only sorts so that likely
mistakes end up on top. stdlib only: runs on the host, next to the dataset.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .labels import DATASET_CLASSES, DATASET_IDS

# Auto-collected name: dog_YYYYmmdd_HHMMSS_<conf*100>.jpg
AUTO_RE = re.compile(r"^dog_\d{8}_\d{6}_(\d{2,3})$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

# A box smaller than this fraction of the frame is almost certainly junk; a
# larger one is almost certainly "the model boxed half the room".
TINY_AREA = 0.004
HUGE_AREA = 0.55


def read_label(path: Path) -> list[dict]:
    out = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for row in text.split("\n"):
        parts = row.split()
        if len(parts) != 5:
            continue
        try:
            cid = int(parts[0])
            cx, cy, w, h = (float(x) for x in parts[1:])
        except ValueError:
            continue
        if cid not in DATASET_CLASSES:
            continue
        out.append({"cls": DATASET_CLASSES[cid], "cx": cx, "cy": cy, "w": w, "h": h})
    return out


def score(stem: str, boxes: list[dict], human: bool) -> tuple[float, list[str]]:
    """Higher means more suspicious. Also returns human-readable reasons."""
    why: list[str] = []
    s = 0.0

    if human:
        # A human already edited this frame; their judgement beats any heuristic.
        # This way the queue cleans itself without a separate "reviewed" button.
        return -1.0, ["edited by a human"]

    kinds = {b["cls"] for b in boxes}
    if not boxes:
        s += 1.0
        why.append("no labels")
    if boxes and kinds == {"dog"}:
        s += 2.0
        why.append("only the dog is labelled")
    if "bowl" not in kinds:
        s += 0.5
        why.append("no bowls")

    m = AUTO_RE.match(stem)
    if m:
        conf = int(m.group(1)) / 100.0
        if conf < 0.30:
            # Exactly the band where auto-collection picks up both hard poses and junk.
            s += (0.30 - conf) * 10
            why.append(f"confidence {conf:.2f}")

    for b in boxes:
        a = b["w"] * b["h"]
        if a < TINY_AREA:
            s += 1.5
            why.append(f"tiny {b['cls']} box")
        elif a > HUGE_AREA:
            s += 1.5
            why.append(f"{b['cls']} box covers half the frame")
    return s, why


def _human_set(root: Path) -> set[str]:
    f = root / "human_labeled.json"
    if not f.exists():
        return set()
    try:
        return set(json.loads(f.read_text()))
    except Exception:
        return set()


def listing(root: Path, order: str = "suspicious", limit: int = 400) -> dict:
    img_dir, lbl_dir = root / "images", root / "labels"
    if not img_dir.exists():
        return {"items": [], "total": 0, "counts": {}}
    human = _human_set(root)
    items = []
    counts = {"all": 0, "human": 0, "no_bowl": 0, "dog_only": 0, "empty": 0}
    for p in sorted(img_dir.glob("*.jpg")):
        boxes = read_label(lbl_dir / f"{p.stem}.txt")
        is_human = p.stem in human
        sc, why = score(p.stem, boxes, is_human)
        kinds = {b["cls"] for b in boxes}
        counts["all"] += 1
        counts["human"] += is_human
        counts["no_bowl"] += "bowl" not in kinds
        counts["dog_only"] += bool(boxes) and kinds == {"dog"}
        counts["empty"] += not boxes
        try:
            ts = p.stat().st_mtime
        except OSError:
            ts = 0.0
        items.append({"name": p.stem, "ts": ts, "score": round(sc, 2), "why": why,
                      "n": len(boxes), "kinds": sorted(kinds), "human": is_human})
    if order == "new":
        items.sort(key=lambda x: -x["ts"])
    else:
        items.sort(key=lambda x: (-x["score"], -x["ts"]))
    return {"items": items[:limit], "total": len(items), "counts": counts,
            "classes": list(DATASET_CLASSES.values())}


def to_lines(boxes: list[dict]) -> str:
    """Labels back to file text. Clamp to the frame: a box may have been dragged past the edge."""
    out = []
    for b in boxes:
        cx, cy, w, h = float(b["cx"]), float(b["cy"]), float(b["w"]), float(b["h"])
        x1, y1 = max(0.0, cx - w / 2), max(0.0, cy - h / 2)
        x2, y2 = min(1.0, cx + w / 2), min(1.0, cy + h / 2)
        cx, cy, w, h = (x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1
        if w < 0.005 or h < 0.005:
            continue
        out.append(f"{DATASET_IDS[b['cls']]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
    return "\n".join(out) + ("\n" if out else "")
