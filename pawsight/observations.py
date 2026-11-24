"""Sequential processing of observations without blocking the MQTT network thread."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

log = logging.getLogger("pawsight.observations")


class ObservationWorker:
    """The single writer of Brain state; the queue keeps frame order and timing.

    512 observations cover more than the 90 s VLM timeout at the usual 5 fps.
    A sustained overload must not eat all memory or block MQTT again: a refused
    frame is logged explicitly.
    """

    def __init__(self, observe: Callable[[dict, bytes | None], None],
                 max_pending: int = 512) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._observe = observe
        self._queue: queue.Queue = queue.Queue(maxsize=max_pending)
        self._stop = threading.Event()
        self._dropped = 0
        self._thread = threading.Thread(target=self._run, name="brain-observations",
                                        daemon=True)
        self._thread.start()

    def submit(self, obs: dict, jpeg: bytes | None = None) -> bool:
        """Called from MQTT: only enqueue the frame, never wait for the VLM."""
        if self._stop.is_set():
            return False
        try:
            self._queue.put_nowait((obs, jpeg))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1:
                log.warning("observation queue is full, new frames are being dropped")
            return False
        if self._dropped:
            log.warning("observations accepted again; frames lost: %d", self._dropped)
            self._dropped = 0
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                obs, jpeg = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._observe(obs, jpeg)
            except Exception:
                log.exception("processing an observation failed, continuing with the next frame")
            finally:
                self._queue.task_done()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        self._thread.join(timeout)
