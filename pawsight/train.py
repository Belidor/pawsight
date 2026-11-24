"""Weekly fine-tuning for a specific dog and a specific room.

THE MAIN DANGER, and the reason this file looks more complex than expected.
Fine-tuning on a single "dog" class does not degrade the other 79 COCO
classes, it ERASES them: the head is rebuilt for nc=1, and the `bowl` class
ceases to exist. Every zone in the project (bowls/water/food) is tied to
`bowl`, so labelling would break at epoch zero, before any training, and it
would break SILENTLY: the dog would keep being detected perfectly.

Therefore:
  * data.yaml always has dog, bowl and person;
  * ultralytics carries over the pretrained head's rows by class NAME
    (cls_remap), so the COCO quality of the bowl detector is inherited for free;
  * before a new model is accepted, it is checked separately that `bowl` has
    not regressed.

Training is the only part of the project that cannot be containerised: MPS
works only on native macOS.
"""
from __future__ import annotations

import json
import logging
import random
import shutil
import time
from pathlib import Path

import yaml

from .labels import (BOWL_MAP50_FLOOR, DATASET_CLASSES, MIN_IMAGES,  # noqa: F401
                     VAL_FRACTION, VAL_MIN_FRACTION)

log = logging.getLogger("pawsight.train")


def refreeze_val(root: Path, seed: int = 1337) -> tuple[int, int]:
    """Rebuild the validation split from the CURRENT dataset contents.

    Returns (frames in the split, total pairs).

    The cost of the call is stated in the caller's log: after a rebuild, past
    runs' metrics are not comparable with future ones, because they were
    evaluated on different frames. That is cheaper than the alternative: an
    acceptance gate decided by a handful of old pictures, which therefore lets
    anything through.

    The selection is the same as at the first freeze: sort, shuffle with a
    fixed seed, take the first fifth. On the same dataset the result is the
    same; otherwise "rebuild" would mean "new metrics every time".
    """
    lbl = {p.stem for p in (root / "labels").glob("*.txt")}
    names = sorted(p.stem for p in (root / "images").glob("*.jpg") if p.stem in lbl)
    random.Random(seed).shuffle(names)
    val = sorted(names[: max(1, int(len(names) * VAL_FRACTION))])
    tmp = root / "val_manifest.json.tmp"
    tmp.write_text(json.dumps(val, indent=1))
    tmp.replace(root / "val_manifest.json")     # atomic: a reader never sees a half-written file
    return len(val), len(names)


def _split(root: Path, seed: int = 1337) -> tuple[int, int, dict | None]:
    """Distribute images/ and labels/ into train/ and val/.

    Returns (train, val, rebuild-or-None). The third element is not decoration:
    it is what goes into train_result.json and from there to the dashboard.
    Rebuilding the split devalues comparison with past runs, and the person
    must learn about it from the run card, not by guessing.

    The validation split is fixed once and does not change afterwards until
    its fraction shrinks (VAL_MIN_FRACTION): otherwise weekly metrics would be
    incomparable for no reason.
    """
    img_src, lbl_src = root / "images", root / "labels"
    frozen = root / "val_manifest.json"

    pairs = []
    for img in sorted(img_src.glob("*.jpg")):
        lbl = lbl_src / f"{img.stem}.txt"
        if lbl.exists():
            pairs.append((img, lbl))
    if len(pairs) < MIN_IMAGES:
        raise SystemExit(
            f"only {len(pairs)} labelled frames, at least {MIN_IMAGES} needed. "
            "Let the system run for a few more days."
        )

    refrozen = None
    if frozen.exists():
        val_names = set(json.loads(frozen.read_text()))
        # The split is frozen once for metric comparability. But if the dataset
        # has grown since while the split stayed the same, the acceptance gate
        # is decided by a handful of old frames: anything passes the bowl
        # threshold, and the zones break silently.
        #
        # This used to be a SystemExit advising a trip to the terminal. The
        # refusal was honest, but it had exactly one cure, one command, so it
        # just drove the person out of the dashboard, while the split shrinks
        # on its own as the dataset grows. So we fix it in place and say so loudly.
        share = len(val_names) / len(pairs)
        if share < VAL_MIN_FRACTION:
            was = len(val_names)
            n_val, n_all = refreeze_val(root)
            val_names = set(json.loads(frozen.read_text()))
            refrozen = {"was": was, "now": n_val, "pairs": n_all}
            log.warning(
                "the validation split was %d frames of %d (%.1f%%), rebuilt to %d. "
                "PAST RUNS' METRICS ARE NOW NOT COMPARABLE WITH FUTURE ONES: "
                "the model is evaluated on different frames.", was, n_all, share * 100, n_val)
    else:
        names = [p[0].stem for p in pairs]
        random.Random(seed).shuffle(names)
        val_names = set(names[: max(1, int(len(names) * VAL_FRACTION))])
        frozen.write_text(json.dumps(sorted(val_names), indent=1))
        log.info("validation split frozen: %d frames", len(val_names))

    for split in ("train", "val"):
        for kind in ("images", "labels"):
            d = root / kind / split
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)

    n_tr = n_va = 0
    for img, lbl in pairs:
        split = "val" if img.stem in val_names else "train"
        shutil.copy2(img, root / "images" / split / img.name)
        shutil.copy2(lbl, root / "labels" / split / lbl.name)
        if split == "val":
            n_va += 1
        else:
            n_tr += 1
    return n_tr, n_va, refrozen


def _write_data_yaml(root: Path) -> Path:
    f = root / "data.yaml"
    f.write_text(yaml.safe_dump({
        "path": str(root),
        "train": "images/train",
        "val": "images/val",
        "names": dict(DATASET_CLASSES),
    }, allow_unicode=True, sort_keys=False))
    return f


def _per_class(metrics, names: dict[int, str]) -> dict[str, dict]:
    """Per-class metrics.

    Careful: box.maps fills missing classes with the OVERALL mAP, i.e. it
    returns a convincing number for a class that was not in validation at all.
    So we rely on ap_class_index: the classes that were actually evaluated.
    """
    out = {}
    idx = list(getattr(metrics.box, "ap_class_index", []))
    for pos, cid in enumerate(idx):
        nm = names.get(int(cid), str(cid))
        out[nm] = {
            "map50": float(metrics.box.ap50[pos]),
            "map50_95": float(metrics.box.ap[pos]) if hasattr(metrics.box, "ap") else None,
            "recall": float(metrics.box.r[pos]),
            "precision": float(metrics.box.p[pos]),
        }
    return out


def _write_json(path: Path | None, data: dict) -> None:
    """Atomic: a temp file and os.replace. The reader (the detector, and through
    it the dashboard) must never see half-written JSON."""
    if path is None:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(path)
    except OSError as e:
        log.warning("training state not written: %s", e)


def run(root: Path, base_model: str, epochs: int = 80, imgsz: int = 960,
        batch: int = 16, device: str = "cpu",
        status_path: Path | None = None, result_path: Path | None = None) -> int:
    from ultralytics import YOLO

    t0 = time.time()
    # Name with SECONDS and no exist_ok: two runs within one minute used to
    # write into the same directory, overwriting each other's metrics.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"finetune_{stamp}"
    _write_json(status_path, {"state": "preparing", "run": name, "started": t0,
                              "epochs": epochs})

    try:
        n_tr, n_va, refrozen = _split(root)
    except SystemExit as e:
        _write_json(status_path, {"state": "error", "run": name, "started": t0,
                                  "finished": time.time(), "error": str(e)})
        raise
    data = _write_data_yaml(root)
    log.info("dataset: train=%d val=%d, classes=%s", n_tr, n_va, list(DATASET_CLASSES.values()))

    model = YOLO(base_model)

    def _epoch(trainer):
        done = getattr(trainer, "epoch", 0) + 1
        total = getattr(trainer, "epochs", epochs) or epochs
        per = (time.time() - t0) / max(done, 1)
        _write_json(status_path, {
            "state": "training", "run": name, "epoch": done, "epochs": total,
            "started": t0, "eta_sec": round(per * max(0, total - done)),
            # The estimate is MEASURED, not a constant: the 2.5 hours from the
            # docs refer to a different dataset size (results.csv of past runs
            # gave ~30 s per epoch).
            "sec_per_epoch": round(per, 1),
        })
    model.add_callback("on_fit_epoch_end", _epoch)

    model.train(
        data=str(data),
        epochs=epochs,
        patience=20,          # for small datasets, instead of the default 100
        imgsz=imgsz,
        batch=batch,
        device=device,
        # On MPS: amp="bf16" fails with RuntimeError, amp=True is a silent
        # no-op (training runs in FP32 anyway), workers is forced to 0.
        # On CUDA mixed precision is real and roughly halves the time.
        amp=str(device).startswith("cuda"),
        workers=0,
        project=str(root.parent / "runs"),
        name=name,
    )

    best = Path(model.trainer.best)
    log.info("best weights: %s", best)

    # --- acceptance gate ---
    fresh = YOLO(str(best))
    m = fresh.val(data=str(data), device=device, imgsz=imgsz, workers=0, verbose=False)
    per = _per_class(m, DATASET_CLASSES)
    print("\n=== per-class metrics ===")
    for nm, v in per.items():
        print(f"  {nm:<8} mAP50={v['map50']:.3f}  recall={v['recall']:.3f}  precision={v['precision']:.3f}")

    def _finish(accepted: bool, reason: str, weights: str | None) -> int:
        res = {"state": "done", "accepted": accepted, "run": name,
               "val_refrozen": refrozen,
               "run_dir": str(best.parent.parent), "reason": reason,
               "per_class": per, "weights": weights, "base_model": base_model,
               "epochs": epochs, "imgsz": imgsz, "batch": batch,
               "started": t0, "finished": time.time()}
        _write_json(result_path, res)
        _write_json(status_path, res)
        return 0 if accepted else 1

    if "bowl" not in per:
        msg = ("class `bowl` is not among the evaluated classes: there were no bowls in "
               "validation or they fell out of the model's head; zones will not work with it")
        print("\n  ✗ REJECTED: class `bowl` is not among the evaluated classes.")
        print("    So either there were no bowls in validation or they fell out of the head;")
        print("    zones will not work with this model. The model was NOT accepted.")
        return _finish(False, msg, None)

    bowl = per["bowl"]["map50"]
    if bowl < BOWL_MAP50_FLOOR:
        msg = (f"bowl mAP50={bowl:.3f} is below the threshold {BOWL_MAP50_FLOOR}: "
               "the zones rely on the bowls")
        print(f"\n  ✗ REJECTED: bowl mAP50={bowl:.3f} is below the threshold {BOWL_MAP50_FLOOR}.")
        print("    The zones rely on the bowls, so such a model must not be accepted.")
        return _finish(False, msg, None)

    target = root.parent / "yolo26s_finetuned.pt"
    shutil.copy2(best, target)
    print(f"\n  ✓ ACCEPTED: bowl mAP50={bowl:.3f}, dog mAP50={per.get('dog', {}).get('map50', 0):.3f}")
    print(f"    weights: {target}")
    print(f"    to enable: PAWSIGHT_MODEL={target.name} in .env, then restart the detector")

    return _finish(True, f"bowl mAP50={bowl:.3f}, dog mAP50={per.get('dog', {}).get('map50', 0):.3f}",
                   str(target))
