"""The "brain": observations -> states -> events -> VLM -> DB and Telegram.

No YOLO, no RTSP reading, no Metal here: only logic. That is why this part
lives happily in a container, unlike the detector, which needs Metal, and
go2rtc, which needs a P2P hop to the camera over the LAN.

Input is an observation of the form:
    {"ts": float, "camera_moving": bool, "settled": bool,
     "dogs": [{"track_id": int, "box": [x1,y1,x2,y2], "conf": float,
               "zones": ["bowls", "water"]}]}
plus an optional JPEG of the frame.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path

from .config import Config
from .heuristics import TrackHistory, state as heuristic_state
from .notify import Telegram
from .store import Store
from .vlm import VLM

log = logging.getLogger("pawsight.brain")

DEBOUNCE_SEC = 3.0
BOUT_GAP_SEC = 12.0
MIN_BOUT_SEC = 3.0
BOUT_ZONE = "bowls"
HINT_ZONES = ("water", "food")
VLM_FRAMES = 4
# A track not seen for longer than this is forgotten entirely. BoT-SORT hands
# out new track_ids all the time (every exit from the frame, every pan), and
# frames holds up to 12 JPEGs per track: without cleanup the brain's memory
# grew without bound. Two minutes is far more than both DEBOUNCE_SEC and
# BOUT_GAP_SEC: cleanup can cut short neither a debounce nor an open visit.
TRACK_TTL_SEC = 120.0
PRUNE_EVERY_SEC = 10.0


def _retry(fn, tries: int = 4, pause: float = 0.25):
    """Database write with retry.

    These two calls used to have no handler at all: any busy database (for
    example during the VACUUM run by cleanup) threw an exception into the bus
    message handler, and the OBSERVATION WAS SILENTLY LOST. An event is worth
    more than a quarter of a second of waiting.
    """
    import sqlite3
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e):
                raise
            if i == tries - 1:
                log.warning("database busy, write lost: %s", e)
                return None
            time.sleep(pause * (i + 1))


class ReportWorker:
    """Report thread: VLM, database write and Telegram, one at a time, in event
    order, and NOT on the observation thread.

    Brain._emit and _bout_flush used to call Ollama synchronously (90 s
    timeout), and all that time the brain's state did not advance while the
    observation queue (512 frames, ~100 s at 5 fps) approached overflow. The
    queue here is bounded: on overflow Brain writes a reduced event (no VLM).
    """

    def __init__(self, max_pending: int = 32) -> None:
        self._queue: queue.Queue = queue.Queue(maxsize=max_pending)
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="brain-reports", daemon=True)
        self._thread.start()

    def submit(self, job) -> bool:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self._dropped += 1
            log.warning("report queue full (VLM cannot keep up): event "
                        "written without VLM and without Telegram, %d so far", self._dropped)
            return False
        return True

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                if job is None:
                    return
                job(True)
            except Exception:
                log.exception("event report failed, moving on to the next one")
            finally:
                self._queue.task_done()

    def close(self, timeout: float | None = 5.0) -> None:
        try:
            self._queue.put(None, timeout=timeout)
        except queue.Full:
            return
        self._thread.join(timeout)


class Brain:
    def __init__(self, cfg: Config, async_reports: bool = True) -> None:
        self.cfg = cfg
        # async_reports=False: report right on the calling thread (tests).
        self._reports = ReportWorker() if async_reports else None
        self.store = Store(cfg.db_path)
        self.vlm = VLM(cfg.ollama_url, cfg.ollama_model)
        self.tg = Telegram(cfg.tg_token, cfg.tg_chats, cfg.rate_limit_sec)

        self.hist: dict[int, TrackHistory] = {}
        self.frames: dict[int, deque] = {}
        self.committed: dict[int, tuple] = {}
        self.pending: dict[int, tuple] = {}
        self.zone_since: dict[int, tuple] = {}
        self.last_seen: dict[int, float] = {}
        self._next_prune = 0.0
        self._open_bouts: dict[str, dict] = {}

    # ---------- helpers ----------

    def _zone_label(self, name: str | None) -> str:
        if name is None:
            return "outside zones"
        for z in self.cfg.zones:
            if z.name == name:
                return z.label
        return name

    def _wants_telegram(self, name: str | None) -> bool:
        """Whether the owner asked for notifications from this zone (zone editor).

        Only the Telegram message is gated: the event still goes to the
        database and the dashboard, so turning a zone off hides nothing.
        """
        if name is None:
            return self.cfg.notify_outside
        for z in self.cfg.zones:
            if z.name == name:
                return z.notify
        return True

    def _dwell(self, tid: int, zone: str | None, now: float) -> float:
        prev = self.zone_since.get(tid)
        if prev is None or prev[0] != zone:
            self.zone_since[tid] = (zone, now)
            return 0.0
        return now - prev[1]

    def reset_after_camera_move(self) -> None:
        """A camera pan makes past trajectories meaningless."""
        self.hist.clear()
        self.pending.clear()
        self.zone_since.clear()

    def _prune(self, now: float) -> None:
        """Forget tracks not seen for longer than TRACK_TTL_SEC.

        A visit (_open_bouts) is not keyed by track: it has its own copy of
        the frames and its own timeout, so cleanup does not touch it. The
        debounce (pending) lives DEBOUNCE_SEC, i.e. long expired for such a track.
        """
        if now < self._next_prune:
            return
        self._next_prune = now + PRUNE_EVERY_SEC
        stale = [t for t, ts in self.last_seen.items() if now - ts > TRACK_TTL_SEC]
        for t in stale:
            for d in (self.last_seen, self.hist, self.frames, self.committed,
                      self.pending, self.zone_since):
                d.pop(t, None)
        if stale:
            log.debug("forgot tracks with no observations > %.0f s: %s", TRACK_TTL_SEC, stale)

    # ---------- bowl visits ----------

    def _bout_seen(self, zones: list[str], tid: int, now: float, jpeg: bytes | None) -> None:
        if BOUT_ZONE not in zones:
            return
        b = self._open_bouts.get(BOUT_ZONE)
        if b is None:
            b = {"start": now, "last": now, "tid": tid,
                 "frames": deque(maxlen=VLM_FRAMES * 3), "hints": []}
            self._open_bouts[BOUT_ZONE] = b
        b["last"] = now
        if jpeg:
            b["frames"].append(jpeg)
        for h in HINT_ZONES:
            if h in zones:
                b["hints"].append(h)

    def _bout_flush(self, now: float) -> None:
        """Close visits whose gap has expired. State lives here, on the
        observation thread; VLM, database and Telegram run on the report thread."""
        for zone, b in list(self._open_bouts.items()):
            if now - b["last"] <= BOUT_GAP_SEC:
                continue
            del self._open_bouts[zone]
            if b["last"] - b["start"] < MIN_BOUT_SEC:
                continue
            jpegs = list(b["frames"])[-VLM_FRAMES:]
            hint = max(set(b["hints"]), key=b["hints"].count) if b["hints"] else None
            self._report(lambda use_vlm, zone=zone, b=b, jpegs=jpegs, hint=hint:
                         self._bout_report(zone, b["start"], b["last"], b["tid"],
                                           jpegs, hint, use_vlm))

    def _bout_report(self, zone: str, start: float, last: float, tid: int,
                     jpegs: list[bytes], hint: str | None, use_vlm: bool = True) -> None:
        dur = last - start
        kind, raw = (self.vlm.classify_bout(jpegs) if (jpegs and use_vlm)
                     else ("unknown", None))
        expected = {"water": "drink", "food": "eat"}.get(hint)
        conflict = bool(expected and kind != "unknown" and kind != expected)

        _retry(lambda: self.store.add_bout(
            zone, start, last, tid,
            kind=kind, zone_hint=hint, conflict=conflict, vlm_text=raw))

        human = {"drink": "drank", "eat": "ate", "unknown": "was at the bowls"}[kind]
        msg = f"🐕 {human} — {int(dur)} s"
        if conflict:
            msg += f" ⚠️ geometry says \"{hint}\", VLM says \"{kind}\""
        log.info(msg)
        if use_vlm and self._wants_telegram(zone):
            self.tg.send(msg, jpegs[-1] if jpegs else None, key=f"bout:{kind}")

    # ---------- state change event ----------

    def _emit(self, tid: int, zone: str | None, st: str, now: float) -> None:
        """Capture everything the report needs while it still exists, and hand
        the report to the thread.

        Frames are copied here: track cleanup (_prune) may forget this track
        before the queue gets to the report.
        """
        jpegs = list(self.frames.get(tid, []))[-VLM_FRAMES:]
        dwell = self._dwell(tid, zone, now)
        self._report(lambda use_vlm: self._emit_report(tid, zone, st, now, jpegs,
                                                       dwell, use_vlm))

    def _emit_report(self, tid: int, zone: str | None, st: str, now: float,
                     jpegs: list[bytes], dwell: float, use_vlm: bool = True) -> None:
        # The VLM is not asked for a pretty phrase: on detector false positives
        # it answers "NO DOG", and that is the only source that tells a dog
        # from an office chair (measurement in docs/architecture.md).
        verdict, vlm_line = (self.vlm.look(jpegs) if (jpegs and use_vlm)
                             else ("unknown", None))
        vlm_text = vlm_line if verdict == "dog" else None

        snap_path = None
        if jpegs:
            snap_path = str(self.cfg.snap_dir / f"{int(now)}_{tid}.jpg")
            Path(snap_path).write_bytes(jpegs[-1])

        conflict = False
        if vlm_text:
            moving = st in ("walking", "running")
            still = any(w in vlm_text.lower() for w in ("lying", "sleeping", "sitting", "asleep"))
            moves = any(w in vlm_text.lower() for w in ("walking", "running", "walks", "runs"))
            conflict = (moving and still) or (not moving and moves)

        # The VLM refusal is written to the database too: the event is not lost,
        # and the dashboard shows why there was no notification for it.
        _retry(lambda: self.store.add(now, tid, zone, st, vlm_line, snap_path, conflict))

        ts = time.strftime("%H:%M", time.localtime(now))
        line = f"🐕 {self._zone_label(zone)}, {ts}"
        if dwell > 60:
            line += f" ({int(dwell // 60)} min in zone)"
        if vlm_text and not conflict:
            line += f" — {vlm_text}"
        elif vlm_text and conflict:
            line += f" — heuristic: {st}; VLM: {vlm_text} ⚠️ disagree"
        else:
            line += f" — {st}"

        log.info(line)
        if not use_vlm:
            return
        if verdict == "no_dog":
            # Suppress ONLY an explicit refusal. VLM silence (verdict == "unknown")
            # does not cancel the notification: otherwise a crashed Ollama would
            # cut off alerts entirely, and nobody would notice.
            log.info("not sent: the VLM sees no dog in the frames")
            return
        if not self._wants_telegram(zone):
            return
        self.tg.send(line, jpegs[-1] if jpegs else None, key=str(zone))

    # ---------- reports: VLM, database, Telegram ----------

    def _report(self, job) -> None:
        """job(use_vlm) goes to the report thread; on overflow, a reduced one runs here.

        A reduced report is only the database write, no VLM and no Telegram:
        that takes milliseconds, and the event is not lost. Waiting for the VLM
        on this thread is not allowed: up to 90 s per call, while the
        observation queue is sized for ~100 s.
        """
        if self._reports is None:
            job(True)
            return
        if not self._reports.submit(job):
            try:
                job(False)
            except Exception:
                log.exception("reduced report failed")

    def close(self, timeout: float | None = 5.0) -> None:
        """Wait for queued reports (batch: until done; service: briefly)."""
        if self._reports is not None:
            self._reports.close(timeout)

    # ---------- input ----------

    def observe(self, obs: dict, jpeg: bytes | None = None) -> None:
        now = float(obs["ts"])

        if obs.get("camera_moving"):
            self.reset_after_camera_move()

        self._bout_flush(now)
        self._prune(now)

        if not obs.get("settled", True):
            return

        for d in obs.get("dogs", []):
            tid = int(d["track_id"])
            box = tuple(float(v) for v in d["box"])
            zones = list(d.get("zones") or [])
            self.last_seen[tid] = now

            h = self.hist.setdefault(tid, TrackHistory())
            h.add(box, now)
            if jpeg:
                self.frames.setdefault(tid, deque(maxlen=VLM_FRAMES * 3)).append(jpeg)

            zone = next((z for z in zones if z not in HINT_ZONES), None)
            st = heuristic_state(h)
            self._dwell(tid, zone, now)
            self._bout_seen(zones, tid, now, jpeg)

            key = (zone, st)
            if self.committed.get(tid) == key:
                self.pending.pop(tid, None)
                continue
            first = self.pending.get(tid)
            if first is None or first[0] != key:
                self.pending[tid] = (key, now)
                continue
            if now - first[1] < DEBOUNCE_SEC:
                continue

            self.committed[tid] = key
            self.pending.pop(tid, None)
            if st != "unknown":

                self._emit(tid, zone, st, now)
