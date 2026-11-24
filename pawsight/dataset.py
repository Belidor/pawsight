"""Collecting frames for fine-tuning the model to a specific dog and a specific room.

Three things here are non-obvious and important.

1. SAVING EVERYTHING IS USELESS. At 5 fps a dog at the bowl yields 300 nearly
   identical frames per minute. Labelling them is wasted time, and training on
   them overfits the model to one pose. So frames are selected:
     * by UNCERTAINTY: the most valuable frames are where the model hesitates
       (conf in the middle band), not where it is already sure;
     * with DEDUPLICATION by perceptual hash: near-identical frames are
       dropped;
     * with an HOURLY CAP, so one active day does not flood the dataset.

2. NOT ONLY DOGS ARE LABELLED. Zones in this project rely on the `bowl` class.
   Fine-tune the model on a single "dog" class and it forgets the other 79
   COCO classes, and the zones fall apart. So the pre-labels include ALL the
   relevant classes, not just the dog.

3. NEGATIVE EXAMPLES ARE NEEDED TOO. A frame where the model saw a dog that is
   not there is worth ten correct ones. Such frames are stored separately.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2

log = logging.getLogger("pawsight.dataset")

# The class set lives in labels.py: the dashboard needs the same list, and it
# runs in the container, which has neither cv2 nor numpy, so this module is not
# imported there.
#
# WHY THERE IS MORE THAN THE DOG HERE. Fine-tuning on a single class does not
# "degrade" the other 79 COCO classes, it erases them: the head is rebuilt for
# nc=1, and the bowl class ceases to exist. Every zone in the project is tied
# to bowl, so labelling would break silently, while the dog keeps being
# detected as if nothing happened. So bowl must stay in the dataset.
#
# The flip side: EVERY listed class must be labelled in EVERY frame, otherwise
# it is trained as background. Hence the minimal set: three classes.
from .labels import (COCO_DOG, COCO_TO_DATASET, DATASET_CLASSES,  # noqa: E402,F401
                     DATASET_IDS, ds_map_from_names)

# for backward compatibility with the old pre-label format
KEEP_CLASSES = DATASET_CLASSES

# Uncertainty band: below it is junk, above it the model already copes.
UNCERTAIN_LO = 0.15      # a dog being held / in a cone gives 0.1-0.3: exactly what needs training
UNCERTAIN_HI = 0.80
# Perceptual hash difference threshold (Hamming distance over 64 bits).
DHASH_MIN_DIST = 12
# At most this many frames per hour.
PER_HOUR_CAP = 40


def dhash(img, size: int = 8) -> int:
    """Perceptual hash: robust to noise and small exposure changes."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, (size + 1, size))
    diff = g[:, 1:] > g[:, :-1]
    out = 0
    for bit in diff.flatten():
        out = (out << 1) | int(bit)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


@dataclass
class Capture:
    root: Path
    enabled: bool = True

    def __post_init__(self) -> None:
        self.img_dir = self.root / "images"
        self.lbl_dir = self.root / "labels"
        self.neg_dir = self.root / "negatives"
        for d in (self.img_dir, self.lbl_dir, self.neg_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._hashes: deque[int] = deque(maxlen=800)
        self._hour: list[float] = []
        self._load_existing_hashes()

    def _load_existing_hashes(self) -> None:
        """So that a restart does not start accumulating duplicates all over again."""
        f = self.root / ".hashes.json"
        if f.exists():
            try:
                self._hashes.extend(json.loads(f.read_text())[-800:])
            except Exception:
                pass

    def _save_hashes(self) -> None:
        try:
            (self.root / ".hashes.json").write_text(json.dumps(list(self._hashes)))
        except Exception:
            pass

    def _rate_ok(self, now: float) -> bool:
        self._hour = [t for t in self._hour if now - t < 3600]
        return len(self._hour) < PER_HOUR_CAP

    def _novel(self, frame) -> bool:
        h = dhash(frame)
        if any(hamming(h, o) < DHASH_MIN_DIST for o in self._hashes):
            return False
        self._hashes.append(h)
        return True

    def maybe_save(self, frame, dets: list[tuple[int, float, tuple]], now: float,
                   ds_map: dict[int, int] | None = None) -> str | None:
        """dets: [(class_id, conf, (x1,y1,x2,y2))] for the current frame.

        ds_map maps THIS model's indices to dataset indices. This used to be
        `d[0] == 16` and COCO_TO_DATASET, so after switching to the three-class
        model (dog=0) auto-capture silently stopped saving anything: there is
        simply no class 16 in it.

        Returns the path of the saved frame or None.
        """
        if not self.enabled:
            return None

        ds_map = COCO_TO_DATASET if ds_map is None else ds_map
        dog_ids = {m for m, d in ds_map.items() if d == DATASET_IDS["dog"]}
        dogs = [d for d in dets if d[0] in dog_ids]
        if not dogs:
            return None
        if not self._rate_ok(now):
            return None

        best = max(c for _, c, _ in dogs)
        # The model already gets confident detections: little to learn from them.
        if best > UNCERTAIN_HI:
            return None
        if best < UNCERTAIN_LO:
            return None
        if not self._novel(frame):
            return None

        # Directories can vanish under a running process (cleanup, git, volume
        # remount). Making sure they exist is cheaper than crashing the
        # detection loop over a missing folder.
        for d in (self.img_dir, self.lbl_dir, self.neg_dir):
            d.mkdir(parents=True, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        name = f"dog_{stamp}_{int(best * 100):02d}"
        img_path = self.img_dir / f"{name}.jpg"
        if not cv2.imwrite(str(img_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            # imwrite does not raise, it returns False: a silent failure that
            # would let frames pile up without labels.
            log.warning("could not write frame %s", img_path)
            return None

        h, w = frame.shape[:2]
        lines = []
        for cid, conf, (x1, y1, x2, y2) in dets:
            if cid not in ds_map:
                continue
            cid = ds_map[cid]
            cx = ((x1 + x2) / 2) / w
            cy = ((y1 + y2) / 2) / h
            bw = (x2 - x1) / w
            bh = (y2 - y1) / h
            lines.append(f"{cid} {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}")
        (self.lbl_dir / f"{name}.txt").write_text("\n".join(lines) + "\n")

        self._hour.append(now)
        self._save_hashes()
        log.info("to dataset: %s (conf=%.2f, objects labelled %d)", img_path.name, best, len(lines))
        return str(img_path)

    def save_labeled(self, jpeg: bytes, boxes: list[dict], now: float) -> str:
        """A frame labelled by a HUMAN on the dashboard.

        Bypasses every filter: the confidence band, deduplication and the
        hourly cap. Their purpose is to thin out the machine stream, and a
        human decision has nothing to thin out; moreover, the model's misses
        (confidence below threshold, or zero) are precisely the most valuable.

        But the HASH still goes into the deduplicator. A frame worth capturing
        by hand by definition falls into the uncertainty band, and auto-capture
        would save a nearly identical one a second later. The hourly slot is
        not spent, though: 40/hour is auto-capture's budget, not the human's.

        Bytes are written VERBATIM, without re-encoding: the file must match
        pixel for pixel what the person saw while drawing the boxes.
        """
        import numpy as _np

        from .labelcheck import to_yolo

        for d in (self.img_dir, self.lbl_dir, self.neg_dir):
            d.mkdir(parents=True, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
        # The prefix differs from auto-capture (dog_*): this way the model tab
        # counts manual frames without guessing by name, and they stand out when
        # reviewing the dataset.
        name = f"manual_{stamp}"
        n = 1
        while (self.img_dir / f"{name}.jpg").exists():
            name = f"manual_{stamp}-{n}"
            n += 1

        (self.img_dir / f"{name}.jpg").write_bytes(jpeg)
        (self.lbl_dir / f"{name}.txt").write_text(to_yolo(boxes))

        try:
            img = cv2.imdecode(_np.frombuffer(jpeg, _np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                self._hashes.append(dhash(img))
                self._save_hashes()
        except Exception:
            log.warning("manual frame hash not computed: auto-capture may duplicate it")

        self._mark_human(name)
        return name

    def relabel(self, stem: str, boxes: list[dict]) -> int:
        """Rewrite the labels of a frame ALREADY in the dataset.

        Unlike save_labeled: the picture is not touched at all, the name does
        not change and no hash goes into the deduplicator, since the frame is
        already there. An empty file is kept: it is a legitimate negative
        example, and it is exactly how a "dog" box on a chair base gets fixed.
        """
        from .review import to_lines

        img = self.img_dir / f"{stem}.jpg"
        if not img.exists():
            raise FileNotFoundError(f"frame {stem} is not in the dataset")
        self.lbl_dir.mkdir(parents=True, exist_ok=True)
        text = to_lines(boxes)
        tmp = self.lbl_dir / f"{stem}.txt.tmp"
        tmp.write_text(text)
        tmp.replace(self.lbl_dir / f"{stem}.txt")   # atomic
        self._mark_human(stem)
        return len([x for x in text.split("\n") if x.strip()])

    def _mark_human(self, stem: str) -> None:
        """Registry of human labels. relabel.py already skips frames that have
        labels, but human work is worth more than machine predictions: the
        safeguard costs one line."""
        f = self.root / "human_labeled.json"
        try:
            cur = json.loads(f.read_text()) if f.exists() else []
        except Exception:
            cur = []
        if stem in cur:
            return
        cur.append(stem)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(cur), indent=1))
        tmp.replace(f)          # atomic: a reader never sees a half-written file

    def _unmark_human(self, stem: str) -> None:
        """The inverse of _mark_human: the frame is gone, but its name would stay in the registry.

        Harmless in itself, but manual frame names are built from the time to
        the second (manual_YYYYmmdd_HHMMSS), and a freed name may be taken by a
        new frame, which would then inherit someone else's "edited by a human"
        mark and sink to the bottom of the queue unreviewed.
        """
        f = self.root / "human_labeled.json"
        if not f.exists():
            return
        try:
            cur = json.loads(f.read_text())
        except Exception:
            return
        if stem not in cur:
            return
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(x for x in cur if x != stem), indent=1))
        tmp.replace(f)          # atomic: a reader never sees a half-written file

    def _drop_from_val(self, stem: str) -> bool:
        """Strike the frame from the frozen validation split.

        This is not cosmetic. train.py::_split computes the validation fraction
        as len(val_manifest) / len(pairs on disk) and refuses to train below
        VAL_MIN_FRACTION, the only thing that stops the acceptance gate from
        deciding a model's fate on a handful of old pictures. A deleted frame's
        name left in the manifest would INFLATE that fraction: the file would
        say "validation 20%" while less was actually on disk. The check would
        silently stop checking, the very case it was introduced for.

        The split is not rebuilt here: only what no longer exists leaves it.
        Only a human rebuilds it (`pawsight dataset --refreeze-val`), because
        that breaks comparability with past weeks' metrics.
        """
        f = self.root / "val_manifest.json"
        if not f.exists():
            return False
        try:
            names = json.loads(f.read_text())
        except Exception:
            return False
        if stem not in names:
            return False
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted(x for x in names if x != stem), indent=1))
        tmp.replace(f)
        return True

    def delete(self, stem: str) -> dict:
        """Remove a frame from the dataset entirely: picture, labels, marks.

        The only place in the project where dataset files are deleted one by
        one. Cleanup (cleanup.py) deliberately does not touch them and will not
        start to: deletion there is BULK and by age, and "old" and "unfit for
        training" are different things. Here it is the opposite: a person looks
        at a specific frame and decides about it. A frame with a box on a chair
        base can be fixed, or it can be thrown away, and the latter used to be
        impossible.

        WHAT IS NOT HERE AND WHY.

        1. The frame's hash is NOT removed from .hashes.json. A frame is deleted
           because it is bad, and auto-capture catches the SCENE: removing the
           hash would allow a nearly identical frame to be saved within the
           next minute, and it would come back into the queue on its own, in
           the same place. The dedup memory is bounded (800 hashes), so the
           ban is not forever.
        2. Copies in images/train, images/val (and labels/) are not touched.
           train.py::_split wipes and rebuilds them every run, and if
           fine-tuning is running right now, ultralytics reads its files from
           exactly there: deleting under a running loader would crash the run.
           So a frame can be deleted even during training: it does not affect
           the current run, only the next one.
        """
        from .review import SAFE_NAME

        if not SAFE_NAME.match(stem or ""):
            raise ValueError("invalid frame name")
        img = self.img_dir / f"{stem}.jpg"
        lbl = self.lbl_dir / f"{stem}.txt"
        # The name comes from the browser, and the command through an anonymous
        # broker. A regex is not enough: a symlink placed inside images/ would
        # lead unlink outside the dataset.
        for p, d in ((img, self.img_dir), (lbl, self.lbl_dir)):
            if p.exists() and p.resolve().parent != d.resolve():
                raise ValueError("the frame is not inside the dataset")
        if not img.exists():
            raise FileNotFoundError(f"frame {stem} is not in the dataset")

        had_label = lbl.exists()
        img.unlink()
        if had_label:
            lbl.unlink()
        self._unmark_human(stem)
        was_val = self._drop_from_val(stem)
        log.info("frame deleted from dataset: %s (labels %s%s)", stem,
                 "present" if had_label else "absent",
                 ", removed from the validation split" if was_val else "")
        st = self.stats()
        return {"name": stem, "had_label": had_label, "was_val": was_val,
                "pairs": st.get("pairs", 0), "images": st.get("images", 0)}

    def stats(self) -> dict:
        # glob is NOT recursive: images/train and images/val are copies that
        # train.py::_split recreates every run.

        imgs = list(self.img_dir.glob("*.jpg"))
        lbls = set(p.stem for p in self.lbl_dir.glob("*.txt"))
        pairs = [p for p in imgs if p.stem in lbls]
        return {
            "images": len(imgs),
            "labels": len(lbls),
            "pairs": len(pairs),
            "unlabeled": len(imgs) - len(pairs),
            "manual": len([p for p in imgs if p.name.startswith("manual_")]),
            "negatives": len(list(self.neg_dir.glob("*.jpg"))),
            "oldest": time.strftime("%Y-%m-%d", time.localtime(min((p.stat().st_mtime for p in imgs), default=time.time()))) if imgs else "—",
        }
