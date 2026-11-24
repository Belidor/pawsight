#!/usr/bin/env python3
"""Restores pre-labels for frames that have none.

Needed if label files were lost (directory cleanup, git rollback) or if
frames were added to the dataset by hand. Existing labels are NOT touched:
human corrections are worth more than machine predictions.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pawsight.config import Config          # noqa: E402
from pawsight.labels import DATASET_CLASSES, ds_map_from_names  # noqa: E402


def pending(img_dir: Path, lbl_dir: Path, human: set[str]) -> list[Path]:
    """Frames that need pre-labels and that MAY be given them.

    Two exceptions, both about the same thing: human work is worth more than
    a machine prediction:

      * a label file exists: leave it alone, even if empty. An empty file
        does not mean "no data" but "nothing here", and that is exactly how
        a false dog on an office chair's base gets fixed;
      * a human edited the frame: leave it alone, even if the label file is
        gone. The frame was edited precisely because the model got it wrong;
        restoring it from the model's own prediction would bring the error back.
    """
    return [p for p in sorted(img_dir.glob("*.jpg"))
            if not (lbl_dir / f"{p.stem}.txt").exists() and p.stem not in human]


def main() -> int:
    from ultralytics import YOLO

    cfg = Config()
    img_dir = cfg.dataset_dir / "images"
    lbl_dir = cfg.dataset_dir / "labels"
    lbl_dir.mkdir(parents=True, exist_ok=True)

    # Frames labelled by a human on the dashboard are never touched, even if
    # their label file has somehow vanished. A machine prediction on such a
    # frame is known to be worse: it was labelled by hand because the model
    # got it wrong.
    human = set()
    hj = cfg.dataset_dir / "human_labeled.json"
    if hj.exists():
        try:
            import json
            human = set(json.loads(hj.read_text()))
        except Exception:
            pass
    todo = pending(img_dir, lbl_dir, human)
    if not todo:
        print("  all frames are already labelled")
        return 0
    print(f"  unlabelled: {len(todo)} frames, running detection")

    model = YOLO(cfg.model)
    # Classes are resolved by NAME from the model itself: in the fine-tuned one
    # dog=0, not 16, and the list [0,16,45] is simply invalid on a three-class model.
    ds_map = ds_map_from_names(dict(model.names))
    if not ds_map:
        print("  the model has none of the dataset classes; nothing to label")
        return 1
    classes = sorted(ds_map)
    written = 0
    for img in todo:
        r = model.predict(str(img), device=cfg.device, imgsz=cfg.imgsz,
                          conf=0.20, classes=classes, verbose=False)[0]
        h, w = r.orig_shape
        lines = []
        for b, c in zip(r.boxes.xyxy, r.boxes.cls):
            cid = ds_map.get(int(c))
            if cid is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in b)
            lines.append(
                f"{cid} {((x1+x2)/2)/w:.6f} {((y1+y2)/2)/h:.6f} "
                f"{(x2-x1)/w:.6f} {(y2-y1)/h:.6f}"
            )
        (lbl_dir / f"{img.stem}.txt").write_text("\n".join(lines) + "\n")
        written += 1
        names = [DATASET_CLASSES[int(l.split()[0])] for l in lines]
        print(f"    {img.name}: {len(lines)} objects {names}")
    print(f"  done: {written}")
    print("  CHECK the labels by eye: these are model predictions, not ground truth")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
