"""Tailing the log file, on the HOST, inside the detector process.

WHY NOT IN THE CONTAINER. The ./storage volume is mounted, and it is tempting
to read /app/storage/detect.log from inside. Measured: while the host process
holds the file open, the container sees it lagging by anywhere from zero to
~2.5 KB, catching up and falling behind again; VirtioFS caches the attributes.
It is consistent but not live, and a "live log" on the dashboard would
stutter and arrive tens of seconds late.

Hence the rule: the container does not poll host-written files for live
data. The host reads and sends lines over the bus; the container needs the
file only for a one-off history load at startup, where lag does not matter.

WHY A FILE AND NOT A LOG HANDLER. detect.log receives ffmpeg's native stderr
("[hevc @ 0x…] Could not find ref with POC 0") and ultralytics prints
("WARNING ⚠️ not enough matching points"). They never go through logging at
all, and they are exactly the lines used to diagnose stream gaps.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger("pawsight.logtail")


def stdout_is(path: Path) -> bool:
    """Whether our stdout is this very file.

    start.sh launches the detector as `nohup … > storage/detect.log`. If it
    was started by hand in a terminal, there is nothing to tail, and the
    caller installs a regular log handler instead.
    """
    try:
        a, b = os.fstat(1), path.stat()
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return False


class FileTailer(threading.Thread):
    """Follows the file and hands out new lines in batches."""

    def __init__(self, path: Path, on_lines: Callable[[list[str], int], None],
                 from_end: bool = True, poll: float = 0.25,
                 burst_cap: int = 200, name: str = "logtail") -> None:
        super().__init__(name=name, daemon=True)
        self.path = Path(path)
        self.on_lines = on_lines
        self.poll = poll
        self.burst_cap = burst_cap
        self._from_end = from_end
        self._stop = threading.Event()
        self._fh = None
        self._key: tuple[int, int] | None = None
        self._tail = b""          # unfinished last line
        self._n = 0               # line sequence number within this process

    def stop(self) -> None:
        self._stop.set()

    # ---- internals ----

    def _open(self, at_end: bool) -> bool:
        try:
            fh = self.path.open("rb")
            st = os.fstat(fh.fileno())
        except OSError:
            return False
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = fh
        self._key = (st.st_dev, st.st_ino)
        self._tail = b""
        if at_end:
            fh.seek(0, os.SEEK_END)
        return True

    def _rotated(self) -> bool:
        """Rotation is detected by inode: start.sh does `mv`, so the file is
        replaced entirely. Truncation is detected by the size shrinking."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        if (st.st_dev, st.st_ino) != self._key:
            return True
        try:
            return st.st_size < self._fh.tell()
        except Exception:
            return False

    def _emit(self, lines: list[str]) -> None:
        if not lines:
            return
        n0 = self._n
        if len(lines) > self.burst_cap:
            skipped = len(lines) - self.burst_cap
            lines = lines[:self.burst_cap] + [f"… {skipped} more lines omitted (burst)"]
        self._n += len(lines)
        try:
            self.on_lines(lines, n0)
        except Exception:
            log.exception("log line handler crashed")

    def run(self) -> None:
        while not self._stop.is_set():
            if self._fh is None:
                if not self._open(self._from_end):
                    self._stop.wait(1.0)
                    continue
            if self._rotated():
                log.info("log %s was replaced; reading from the start", self.path.name)
                if not self._open(at_end=False):
                    self._fh = None
                    self._stop.wait(1.0)
                    continue
            try:
                chunk = self._fh.read()
            except OSError:
                self._fh = None
                continue
            if chunk:
                data = self._tail + chunk
                parts = data.split(b"\n")
                self._tail = parts.pop()          # the tail without \n is read later
                self._emit([p.decode("utf-8", errors="replace") for p in parts])
            self._stop.wait(self.poll)
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
