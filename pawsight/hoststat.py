"""Model, dataset and training state, collected on the HOST.

All of this lives outside the volume mounted into the container: only the
detector sees dataset/, runs/ and the weights. So it builds the picture
itself and publishes it to pawsight/status with retain; otherwise a
dashboard opened between ticks would show an empty tab until the next publish.

This cannot be computed in the detection loop: two directory walks over 146
files against a 200 ms per-frame budget. Hence a separate thread with a slow tick.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .config import ROOT, Config
from .labels import DATASET_CLASSES, MIN_IMAGES, VAL_MIN_FRACTION

log = logging.getLogger("pawsight.hoststat")

TRAIN_LOCK = ROOT / "storage" / "train.lock"
TRAIN_STATUS = ROOT / "storage" / "train_status.json"
TRAIN_RESULT = ROOT / "storage" / "train_result.json"

_sha_cache: dict[tuple, str] = {}


def _sha12(p: Path) -> str:
    """Hash of the weights, to tell "the same file" from "the same name".
    Cached by (path, mtime, size): no point hashing 20 MB every tick."""
    try:
        st = p.stat()
    except OSError:
        return ""
    key = (str(p), st.st_mtime, st.st_size)
    if key in _sha_cache:
        return _sha_cache[key]
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    _sha_cache.clear()
    _sha_cache[key] = h.hexdigest()[:12]
    return _sha_cache[key]


def _dir_bytes(d: Path) -> int:
    total = 0
    if not d.exists():
        return 0
    for p in d.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def dataset_stats(root: Path) -> dict:
    img_dir, lbl_dir = root / "images", root / "labels"
    # glob is NOT recursive: images/train and images/val are copies that
    # _split recreates every run; they must not be counted twice.
    imgs = sorted(img_dir.glob("*.jpg")) if img_dir.exists() else []
    lbls = set(p.stem for p in lbl_dir.glob("*.txt")) if lbl_dir.exists() else set()
    pairs = [p for p in imgs if p.stem in lbls]
    manual = [p for p in imgs if p.name.startswith("manual_")]
    empty = 0
    for p in pairs:
        try:
            if not (lbl_dir / f"{p.stem}.txt").read_text().strip():
                empty += 1
        except OSError:
            pass
    val = 0
    vm = root / "val_manifest.json"
    if vm.exists():
        try:
            val = len(json.loads(vm.read_text()))
        except Exception:
            pass
    oldest = "—"
    if imgs:
        try:
            oldest = time.strftime("%Y-%m-%d",
                                   time.localtime(min(p.stat().st_mtime for p in imgs)))
        except OSError:
            pass
    n = len(pairs)
    # The gate counts PAIRS: train.py::_split drops frames without labels and
    # then raises SystemExit on what is left. Showing the image count would
    # enable a button that fails immediately.
    val_ok = n == 0 or val / n >= VAL_MIN_FRACTION
    return {
        "images": len(imgs), "labels": len(lbls), "pairs": n,
        "unlabeled": len(imgs) - n, "manual": len(manual), "empty_labels": empty,
        "negatives": len(list((root / "negatives").glob("*.jpg"))) if (root / "negatives").exists() else 0,
        "val": val, "val_ok": val_ok, "min_images": MIN_IMAGES,
        "min_val_fraction": VAL_MIN_FRACTION,
        "oldest": oldest, "need": max(0, MIN_IMAGES - n),
        # "Ready" = the button can be pressed. A small validation split no
        # longer counts here: it is rebuilt at startup (train.py::_split), and
        # val_ok stays a separate field since the cost still needs a warning.
        "ready": n >= MIN_IMAGES,
    }


def train_state() -> dict:
    """Training state, from the files the training process itself writes."""
    out = {"state": "idle", "pid": None, "run": None, "log": None, "paused": False,
           "epoch": None, "epochs": None, "eta_sec": None, "started": None}
    if TRAIN_LOCK.exists():
        try:
            lk = json.loads(TRAIN_LOCK.read_text())
            pid = int(lk.get("pid") or 0)
            from .procutil import pid_alive
            alive = pid_alive(pid)
            if alive:
                out.update(state="running", pid=pid, run=lk.get("run"),
                           log=lk.get("log"), started=lk.get("started"),
                           paused=bool(lk.get("paused")))
        except Exception:
            pass
    if TRAIN_STATUS.exists():
        try:
            st = json.loads(TRAIN_STATUS.read_text())
            for k in ("epoch", "epochs", "eta_sec", "started", "run"):
                if st.get(k) is not None:
                    out[k] = st[k]
            if out["state"] != "running" and st.get("state") in ("error",):
                out["state"] = st["state"]
        except Exception:
            pass
    if TRAIN_RESULT.exists():
        try:
            out["last"] = json.loads(TRAIN_RESULT.read_text())
        except Exception:
            pass
    return out


def collect(cfg: Config, detector=None) -> dict:
    weights = Path(cfg.model)
    if not weights.is_absolute():
        weights = ROOT / cfg.model
    try:
        st = weights.stat()
        wbytes, wmtime = st.st_size, st.st_mtime
    except OSError:
        wbytes = wmtime = 0
    runs = ROOT / "runs"
    classes = {}
    if detector is not None and getattr(detector, "_names", None):
        classes = {str(i): detector._names[i] for i in (detector.classes or [])
                   if i in detector._names}
    return {
        "ts": time.time(),
        "detector": {
            "device": cfg.device, "imgsz": cfg.imgsz, "conf": cfg.conf,
            "tracker": cfg.tracker, "fps_target": cfg.detect_fps,
            "fps_actual": round(getattr(detector, "fps", 0.0), 2) if detector else None,
            "infer_size": [cfg.infer_w, cfg.infer_h], "rtsp": cfg.detect_rtsp,
            "classes": classes,
            # Detection paused for fine-tuning: there are no observations on
            # purpose, and the dashboard must say so rather than "detector silent".
            "paused": bool(getattr(detector, "paused_detection", False)) if detector else False,
            "uptime_sec": int(time.time() - getattr(detector, "_started", time.time()))
            if detector else None,
        },
        "model": {
            # Show what the detector ACTUALLY loaded, not "there is a fine-tuned
            # file in the root": .env may point at the base model while the
            # fine-tuned one just sits next to it.
            "name": weights.name, "bytes": wbytes, "mtime": wmtime,
            "finetuned": "finetun" in weights.name,
            "sha256_12": _sha12(weights),
            "available": sorted(p.name for p in ROOT.glob("*.pt")),
        },
        "dataset": dataset_stats(cfg.dataset_dir),
        "train": train_state(),
        "disk": {
            "runs_bytes": _dir_bytes(runs),
            "runs_count": len([p for p in runs.glob("*") if p.is_dir()]) if runs.exists() else 0,
            "dataset_bytes": _dir_bytes(cfg.dataset_dir),
            "snapshots_bytes": _dir_bytes(cfg.snap_dir),
        },
        "classes": dict(DATASET_CLASSES),
    }


class StatusPublisher(threading.Thread):
    """A slow tick on its own thread: directory walks must not land in the
    detection loop or in the bus handler thread."""

    def __init__(self, cfg: Config, publish, detector=None, period: float = 30.0) -> None:
        super().__init__(name="status", daemon=True)
        self.cfg, self.publish, self.detector, self.period = cfg, publish, detector, period
        self._stop = threading.Event()
        self._wake = threading.Event()

    def poke(self) -> None:
        """Report right away: after a frame is saved or training starts."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.publish(collect(self.cfg, self.detector))
            except Exception:
                log.exception("failed to collect status")
            self._wake.wait(self.period)
            self._wake.clear()
