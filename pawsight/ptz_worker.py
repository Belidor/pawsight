"""Camera commands run on their own thread, so the frame loop never waits for miIO.

A synchronous pan call cost up to 5 s of stalled frames on a timeout (the
camera is busy with the motor and does not answer UDP). Now the command is
queued and the loop moves on; the result is collected on a later tick.
"""
from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger("pawsight.ptzworker")


class PtzWorker:
    def __init__(self, ptz, gap_sec: float = 0.5) -> None:
        self.ptz = ptz
        self.gap = gap_sec
        self._q: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._inflight = False
        threading.Thread(target=self._run, name="ptz-worker", daemon=True).start()

    @property
    def busy(self) -> bool:
        return self._inflight or not self._q.empty()

    def submit(self, direction: str, tag: str = "") -> None:
        self._q.put((direction, tag))

    def poll(self) -> list[tuple[str, str, bool, str | None, float]]:
        """(direction, tag, ok, error, seconds) for every finished command."""
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    def _run(self) -> None:
        while True:
            direction, tag = self._q.get()
            self._inflight = True
            t0 = time.perf_counter()
            try:
                self.ptz.rotate(direction)
                self._results.put((direction, tag, True, None, time.perf_counter() - t0))
            except Exception as e:
                self._results.put((direction, tag, False, str(e), time.perf_counter() - t0))
            finally:
                self._inflight = False
            time.sleep(self.gap)      # the motor needs a pause between commands


class AsyncRotate:
    """Adapter with a .rotate(direction) interface that only queues the command.
    Used by the tilt loop: its steps are rare and it confirms them by measurement."""

    def __init__(self, worker: PtzWorker, tag: str = "tilt") -> None:
        self.w, self.tag = worker, tag

    def rotate(self, direction: str) -> str:
        self.w.submit(direction, self.tag)
        return "queued"
