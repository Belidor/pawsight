"""Starting fine-tuning from the dashboard. Lives in the detector process, on
the HOST: MPS is only available on native macOS, and this is the only part of
the project that cannot be containerised.

Three things here are non-obvious and important.

1. STDOUT IS REDIRECTED EXPLICITLY. start.sh runs the detector as
   `nohup … > storage/detect.log`, and the child process would inherit that
   file: ultralytics progress bars with carriage returns would pour straight
   into the log that the "Logs" tab reads.

2. A SEPARATE SESSION AND A LOCK FILE. A run takes tens of minutes and has no
   reason to die when the detector restarts, hence start_new_session. But an
   orphaned process that SILENTLY swaps the weights at the end is even worse:
   so the pid is written to storage/train.lock, the detector adopts a live run
   on startup, and stop.sh knows about this file.

3. THE DETECTOR KEEPS RUNNING (unless the person ticked "pause detection"; then
   the detection loop reads the stream but does not call the model, see
   detection_paused), and the danger here is not running out of memory but
   silence. train.py sets workers=0, i.e. loads data on the main thread; that
   steals CPU from the RTSP reader thread, the reader falls behind, go2rtc's
   receive buffer overflows, and the measured incident with 2-8 s gaps
   recurs. The cascade is silent from there: gaps -> "camera is moving" ->
   settled=False -> the brain returns early -> no events and the dataset stops
   growing, while the dashboard shows a live picture and a green dot. That is
   why the detector publishes the real fps.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import ROOT, Config
from . import procutil
from .labels import MIN_IMAGES

log = logging.getLogger("pawsight.train")

LOCK = ROOT / "storage" / "train.lock"
STATUS = ROOT / "storage" / "train_status.json"


def _alive(pid: int) -> bool:
    """Whether the process is alive AND it really is our training.

    os.kill(pid, 0) alone is not enough: pids are reused, and a day later
    anything may be running under the same number.
    """
    if not pid:
        return False
    if not procutil.pid_alive(pid):
        return False
    out = procutil.cmdline(pid)
    if out is None:
        return True          # could not check: assume alive, that is safer
    return "pawsight" in out and "train" in out


def read_lock() -> dict | None:
    if not LOCK.exists():
        return None
    try:
        d = json.loads(LOCK.read_text())
    except Exception:
        LOCK.unlink(missing_ok=True)
        return None
    if _alive(int(d.get("pid") or 0)):
        return d
    LOCK.unlink(missing_ok=True)      # training is over, the lock is stale
    return None


class Trainer:
    def __init__(self, cfg: Config, on_log=None) -> None:
        self.cfg = cfg
        self.on_log = on_log            # training log tail -> bus
        self._proc: subprocess.Popen | None = None
        self._tailer = None
        # pid of the run during which the person asked to pause detection
        # (a checkbox on the dashboard). Kept in memory rather than reading
        # train.lock on every frame: read_lock calls `ps`.
        self._pause_pid: int | None = None
        self.adopt()

    # ---- detection pause ----

    def detection_paused(self) -> bool:
        """A run started with the "pause detection" checkbox is in progress.

        Cheap: a liveness check without `ps`, so it can be called from the
        detection loop. Once the run ends (by itself, via "Stop", or killed by
        stop.sh), the pause lifts on its own.
        """
        pid = self._pause_pid
        if not pid:
            return False
        if self._proc is not None and self._proc.pid == pid:
            alive = self._proc.poll() is None
        else:
            alive = procutil.pid_alive(pid)
        if not alive:
            self._pause_pid = None
            log.info("training finished: detection resumed")
        return alive

    # ---- readiness ----

    def check(self) -> str | None:
        """The reason training cannot start, or None."""
        if read_lock():
            return "training is already running"
        d = self.cfg.dataset_dir
        lbl = set(p.stem for p in (d / "labels").glob("*.txt")) if (d / "labels").exists() else set()
        imgs = list((d / "images").glob("*.jpg")) if (d / "images").exists() else []
        pairs = [p for p in imgs if p.stem in lbl]
        if len(pairs) < MIN_IMAGES:
            # A cheap check instead of the SystemExit inside _split, which would
            # only happen after thirty seconds of loading the model.
            return f"{len(pairs)} labelled frames, {MIN_IMAGES} needed"
        # A small validation split is NO LONGER among the refusal reasons. It
        # shrinks on its own as the dataset grows, and the only cure was one
        # terminal command, so the refusal's only effect was "go to the
        # console". Now train.py::_split rebuilds it at startup and records
        # that in the run card.
        return None

    # ---- start ----

    def start(self, pause_detection: bool = False) -> dict:
        why = self.check()
        if why:
            return {"ok": False, "error": why}

        st = self.cfg.db_path.parent
        st.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = st / f"train_{stamp}.log"
        try:
            fh = log_path.open("wb")
        except OSError as e:
            return {"ok": False, "error": f"cannot open the training log: {e}"}

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pawsight", "train"],
                cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                **procutil.session_kwargs(),
            )
        except Exception as e:
            fh.close()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            try:
                fh.close()          # the file stays open in the child
            except Exception:
                pass

        self._proc = proc
        self._pause_pid = proc.pid if pause_detection else None
        LOCK.write_text(json.dumps({"pid": proc.pid, "started": time.time(),
                                    "run": stamp, "log": str(log_path),
                                    "paused": bool(pause_detection)}))
        self._tail(log_path)
        log.info("fine-tuning started: pid %s, log %s", proc.pid, log_path.name)
        return {"ok": True, "pid": proc.pid, "run": stamp, "log": log_path.name}

    def stop(self) -> dict:
        d = read_lock()
        if not d:
            return {"ok": False, "error": "training is not running"}
        pid = int(d["pid"])
        try:
            procutil.terminate(pid)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        log.warning("fine-tuning stopped manually (pid %s)", pid)
        LOCK.unlink(missing_ok=True)
        if self._pause_pid:
            self._pause_pid = None
            log.info("detection resumed")
        return {"ok": True}

    # ---- adopting a run after a detector restart ----

    def adopt(self) -> None:
        d = read_lock()
        if not d:
            return
        log.info("training already running (pid %s, started %s): adopting it",
                 d.get("pid"), time.strftime("%H:%M", time.localtime(d.get("started", 0))))
        if d.get("paused"):
            # Detector restarted mid-run: the pause promise still holds.
            self._pause_pid = int(d.get("pid") or 0) or None
        p = d.get("log")
        if p:
            self._tail(Path(p))

    def _tail(self, path: Path) -> None:
        if self.on_log is None:
            return
        from .logtail import FileTailer
        if self._tailer:
            self._tailer.stop()
        # from_end=False: when adopting a run, we also want what is already written

        self._tailer = FileTailer(path, self.on_log, from_end=False, poll=0.5)
        self._tailer.start()
