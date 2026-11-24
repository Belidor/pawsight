"""Garbage cleanup. stdlib only: half of the work runs in the container.

The split is the same as everywhere: storage/ and the database belong to the
brain, runs/ to the detector. A bus command carries ONLY a group name, never
a path, glob or regular expression: the broker is anonymous and any local
process can write to it. The executor resolves the target itself, relative
to ROOT, and every file is checked for containment before deletion.

WHAT MUST NOT BE TOUCHED AND WHY: the list below is not decoration. Half of
the items break silently: the system keeps running, and the results become
meaningless weeks later.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("pawsight.cleanup")

# Checked BEFORE every unlink. Cheaper than spending a week figuring out why
# everything works but the numbers lie.
NEVER = {
    # labels and their reproducibility
    "dataset/images", "dataset/labels", "dataset/negatives",
    # deleting it = the split gets rebuilt, and the acceptance gate starts
    # scoring the model on its own training data. No error, just a convincing number.
    "dataset/val_manifest.json",
    # the dedup memory is loaded ONLY from here, never recomputed: without the
    # file, auto-collection starts piling up near-identical frames, 40 an hour
    "dataset/.hashes.json",
    "dataset/data.yaml", "dataset/human_labeled.json",
    # tilt reference: tilt reads the anchors from the .json. It can be restored
    # with the recapture command, but ONLY if the anchors are visible right now,
    # and the log is full of "no anchors in frame"
    "storage/tilt_reference.jpg", "storage/tilt_reference.json",
    # events, bowl visits, drinking
    "storage/events.db",
    # live logs: opened without O_APPEND, truncation yields a sparse file of zeros
    "storage/detect.log", "go2rtc/go2rtc.log",
    # the only record of the previous session, kept for incident analysis
    "storage/detect.log.1",
    # Mi account token
    "go2rtc/go2rtc.yaml",
}


def _forbidden(root: Path, p: Path) -> bool:
    try:
        rel = p.resolve().relative_to(root.resolve())
    except ValueError:
        return True                      # outside ROOT: definitely not
    parts = rel.as_posix()
    for bad in NEVER:
        if parts == bad or parts.startswith(bad + "/"):
            return True
    # model weights live in the root; deleting the one in PAWSIGHT_MODEL kills
    # the detector on the next restart
    return len(rel.parts) == 1 and rel.suffix == ".pt"


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else sum(
            f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def _rm(root: Path, p: Path) -> int:
    """Delete ONE FILE AT A TIME, never rmtree on input data."""
    if _forbidden(root, p):
        log.warning("refusing to delete a protected path: %s", p)
        return 0
    freed = 0
    if p.is_dir():
        for f in sorted(p.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if _forbidden(root, f):
                continue
            try:
                if f.is_file() or f.is_symlink():
                    freed += f.stat().st_size
                    f.unlink()
                elif f.is_dir():
                    f.rmdir()
            except OSError:
                pass
        try:
            p.rmdir()
        except OSError:
            pass
    else:
        try:
            freed = p.stat().st_size
            p.unlink()
        except OSError:
            pass
    return freed


# ---------- detector side: runs/ ----------

def runs_targets(root: Path, keep: int = 1) -> list[Path]:
    runs = root / "runs"
    if not runs.exists():
        return []
    fine = sorted((p for p in runs.glob("finetune_*") if p.is_dir()),
                  key=lambda p: p.name)
    # The most recent run is always kept: it holds best.pt of REJECTED runs
    # (the only copy) and results.csv, the only training history.
    victims = fine[:-keep] if keep > 0 else fine
    out = list(victims)
    d = runs / "detect"                  # leftovers of ultralytics predict runs
    if d.exists():
        out.append(d)
    return out


def host_plan(root: Path, keep_runs: int = 1) -> list[dict]:
    vics = runs_targets(root, keep_runs)
    total = sum(_size(p) for p in vics)
    kept = sorted((p.name for p in (root / "runs").glob("finetune_*") if p.is_dir()))
    return [{
        "id": "runs", "owner": "detector",
        "label": "Training outputs (runs/)",
        "items": len(vics), "bytes": total,
        "sample": [f"{p.name} ({_size(p) // 2**20} MB)" for p in vics[:6]],
        "kept": ([f"{kept[-1]}: latest, kept"] if kept and keep_runs > 0 else []),
    }]


def host_apply(root: Path, keep_runs: int = 1) -> dict:
    freed = n = 0
    for p in runs_targets(root, keep_runs):
        b = _rm(root, p)
        if b:
            n += 1
            freed += b
    log.info("cleanup: %d runs deleted, %d MB freed", n, freed // 2**20)
    return {"id": "runs", "items": n, "bytes": freed}


# ---------- brain side: storage/ and the database ----------

ROTATED = ("detect.log.old", "live.log")


def storage_plan(storage: Path, days: int = 14) -> list[dict]:
    cut = time.time() - days * 86400
    snaps = [p for p in (storage / "snapshots").glob("*.jpg")
             if p.stat().st_mtime < cut] if (storage / "snapshots").exists() else []
    logs = [storage / n for n in ROTATED if (storage / n).exists()]
    # training logs: keep the latest, it documents the current weights
    tl = sorted(storage.glob("train_*.log"), key=lambda p: p.name)
    logs += tl[:-1]
    return [
        {"id": "snapshots", "owner": "brain",
         "label": f"Event snapshots older than {days} days",
         "items": len(snaps), "bytes": sum(_size(p) for p in snaps), "sample": [], "kept": []},
        {"id": "logs", "owner": "brain", "label": "Rotated logs",
         "items": len(logs), "bytes": sum(_size(p) for p in logs),
         "sample": [p.name for p in logs[:6]],
         "kept": ["detect.log and detect.log.1: live and previous session"]},
    ]


def storage_apply(root: Path, storage: Path, groups: set[str], days: int = 14) -> list[dict]:
    out = []
    cut = time.time() - days * 86400
    if "snapshots" in groups:
        freed = n = 0
        for p in (storage / "snapshots").glob("*.jpg"):
            try:
                if p.stat().st_mtime < cut:
                    b = _rm(root, p)
                    if b:
                        n += 1
                        freed += b
            except OSError:
                pass
        out.append({"id": "snapshots", "items": n, "bytes": freed})
    if "logs" in groups:
        freed = n = 0
        victims = [storage / x for x in ROTATED if (storage / x).exists()]
        victims += sorted(storage.glob("train_*.log"), key=lambda p: p.name)[:-1]
        for p in victims:
            b = _rm(root, p)
            if b:
                n += 1
                freed += b
        out.append({"id": "logs", "items": n, "bytes": freed})
    return out
