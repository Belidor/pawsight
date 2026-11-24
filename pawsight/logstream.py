"""Log stream to the dashboard: secret redaction, line parsing, ring buffer.

Lives in the CONTAINER, so stdlib only: no cv2, no numpy here.

Three sources merge into one buffer:
  * the MQTT topic pawsight/logs: lines the detector reads from its own log
    on the HOST. Reading that file from the container does not work: measured,
    VirtioFS serves it with up to tens of seconds of lag while the host
    process holds the file open. Live data goes over the bus only;
  * BufferHandler on the brain's ROOT logger: its own records, including
    aiohttp and paho; one day anything can break, not just pawsight.*;
  * a one-off load of the detect.log tail at startup: it is historical, so
    filesystem lag does not matter.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("pawsight.logstream")

# ---------- secret redaction ----------

# WHY THIS IS NEEDED AT ALL. notify.py promises in its docstring that the token
# is not logged, and it does not log it itself. But basicConfig(level=INFO)
# enables the httpx logger, which prints the full request URL, bot token
# included. Verified on a live container. The cause is fixed in
# __main__._log(), but one measure is not enough: ffmpeg and ultralytics
# write bytes bypassing logging entirely.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"(?i)\bauthorization\s*:\s*\S+"), "Authorization: <redacted>"),
    # [\w-]* in front: CAMERA_TOKEN and BOT_TOKEN have no word boundary before
    # "token" (underscore is a word character too), so \b would not fire there.
    (re.compile(r"(?i)\b[\w-]*(token|api[_-]?key|secret|passwd|password)"
                r"(\s*[:=]\s*)\S+"), r"<redacted>\2<redacted>"),
    # The Mi account token that go2rtc appends to its own config.
    (re.compile(r"\bV1:[A-Za-z0-9+/=]{10,}"), "V1:<redacted>"),
]

# There is deliberately NO generic [0-9a-f]{32} rule: it would mangle
# ultralytics checksums and frame hashes. The device token is caught as a
# literal from the environment: it is exact there, with zero false positives.
_SECRET_ENV = ("TELEGRAM_BOT_TOKEN", "CAMERA_TOKEN", "TELEGRAM_CHAT_ID")
_literals: list[str] = []


def refresh_literals() -> None:
    """Literal secrets from the environment. Anything shorter than 8 characters
    is skipped: cutting a three-character string out of the logs would riddle
    the whole text."""
    global _literals
    vals = []
    for name in _SECRET_ENV:
        v = (os.getenv(name) or "").strip()
        if len(v) >= 8:
            vals.append(v)
    _literals = sorted(set(vals), key=len, reverse=True)


refresh_literals()


def redact(s: str) -> str:
    if not s:
        return s
    for lit in _literals:          # literals first: zero false positives
        if lit in s:
            s = s.replace(lit, "<redacted>")
    for rx, repl in _PATTERNS:
        s = rx.sub(repl, s)
    return s


class RedactFilter(logging.Filter):
    """Rewrites the record's already formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            clean = redact(msg)
            if clean != msg:
                record.msg, record.args = clean, ()
        except Exception:
            pass
        return True


def install_redaction() -> None:
    """The filter is attached to HANDLERS, not to the logger.

    Logger.callHandlers walks the ancestors and calls hdlr.handle(), which
    applies the HANDLER's filters. Ancestor loggers' filters are never
    consulted for propagated records, so a filter on the root logger would
    not fire for a single record from pawsight.*.
    """
    f = RedactFilter()
    for h in logging.getLogger().handlers:
        if not any(isinstance(x, RedactFilter) for x in h.filters):
            h.addFilter(f)


# ---------- line parsing ----------

# Accept both the new format with a date and the old one without: rotated
# logs from past runs stay in the old format forever.
LINE_RE = re.compile(
    r"^(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?"
    r"(?P<time>\d{2}:\d{2}:\d{2})(?:[.,]\d+)? +"
    r"(?P<level>[A-Z]+) +"
    r"(?P<logger>[\w.]+): "
    r"(?P<text>.*)$"
)

LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30,
          "error": 40, "critical": 50, "fatal": 50}


def level_no(name: str) -> int:
    return LEVELS.get((name or "info").lower(), 20)


class LineParser:
    """Lines -> records, taking continuations into account.

    A line that does not match the pattern is either ffmpeg/ultralytics output
    (`[hevc @ 0x...] Could not find ref with POC 0`) or a continuation of a
    traceback from log.exception. It must not become a separate record: the
    traceback would fall apart. It is glued to the previous record while
    that one is still "warm".
    """

    GLUE_MAX_LINES = 40
    # A traceback continuation either starts with indentation, or is the
    # "Traceback ..." header, or is a final line like "ValueError: text".
    # ffmpeg output (`[hevc @ 0x...] ...`) matches none of these and stays a
    # record of its own, and that is exactly what is needed to diagnose
    # stream gaps.
    GLUE_START = re.compile(r"^(\s+|Traceback \(most recent call last\):)")
    GLUE_TAIL = re.compile(r"^(\w+(\.\w+)*(Error|Exception|Warning|Interrupt)\b"
                           r"|During handling of|The above exception)")

    def __init__(self, src: str, day: str | None = None) -> None:
        self.src = src
        self.day = day or time.strftime("%Y-%m-%d")
        self._pending: dict | None = None
        self._glued = 0
        self._in_glue = False

    def feed(self, raw: str) -> dict | None:
        """Line -> the COMPLETED previous record, or None.

        The current record is held until it is clear that it has ended: a
        traceback from log.exception arrives as separate lines, and they
        cannot be appended to a record already handed out, since it has gone
        to clients. So records are released one line late, and flush() picks
        up the tail at the end of a batch.
        """
        line = raw.rstrip("\n\r")
        if not line.strip():
            return None
        m = LINE_RE.match(line)
        if not m:
            glue = self._pending is not None and self._glued < self.GLUE_MAX_LINES and (
                self.GLUE_START.match(line)
                or (self._in_glue and self.GLUE_TAIL.match(line)))
            if glue:
                self._pending["text"] += "\n" + redact(line)
                self._glued += 1
                self._in_glue = True
                return None
            rec = {"ts": time.time(), "level": "info", "logger": "raw",
                   "text": redact(line), "src": self.src, "raw": True}
            return self._swap(rec)

        d = m.group("date") or self.day
        try:
            ts = time.mktime(time.strptime(f"{d} {m.group('time')}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            ts = time.time()
        rec = {"ts": ts, "level": m.group("level").lower(),
               "logger": m.group("logger"), "text": redact(m.group("text")),
               "src": self.src, "raw": False}
        return self._swap(rec)

    def _swap(self, rec: dict) -> dict | None:
        done, self._pending = self._pending, rec
        self._glued, self._in_glue = 0, False
        return done

    def flush(self) -> dict | None:
        """Release the held record. Called at the end of a batch of lines.

        A traceback split across two batches loses its gluing, and the tail
        arrives as separate lines. The reader hands over everything it read in
        one go, so this is rare, but a rare split record is better than one
        stuck forever.
        """
        done, self._pending = self._pending, None
        self._glued, self._in_glue = 0, False
        return done


def backfill(path: Path, src: str = "detect", max_bytes: int = 200_000) -> list[dict]:
    """Tail of the log file, for the tab's first render.

    The date of undated lines is reconstructed by a backward pass from the
    file's mtime: going from the end, as soon as the time of day INCREASES
    while moving backwards, we have crossed midnight, so we step back a day.
    That is noticeably more honest than stamping everything with today's date.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()               # drop the truncated first line
            data = f.read()
        mtime = path.stat().st_mtime
    except OSError:
        return []

    lines = data.decode("utf-8", errors="replace").splitlines()
    day = time.strftime("%Y-%m-%d", time.localtime(mtime))
    parser = LineParser(src, day)
    out: list[dict] = []
    for ln in lines:
        rec = parser.feed(ln)
        if rec is not None:
            out.append(rec)
    tail = parser.flush()
    if tail is not None:
        out.append(tail)

    # backward pass: fix the date where time "runs backwards" incorrectly
    shift = 0.0
    prev = None
    for rec in reversed(out):
        if rec.get("raw"):
            continue
        t = rec["ts"]
        if prev is not None and t > prev + 1:
            shift += 86400.0               # crossed midnight going backwards
        prev = t
        rec["ts"] = t - shift
    return out


# ---------- ring buffer ----------

class LogBuffer:
    """Shared buffer for all sources. Read from the event loop, written from
    the MQTT and logging threads, hence the lock."""

    def __init__(self, maxlen: int = 3000) -> None:
        self.maxlen = maxlen
        self._items: list[dict] = []
        self._seq = 0
        self._dropped = 0
        self._lock = threading.Lock()
        self._subs: list = []

    def subscribe(self, fn) -> None:
        """fn(rec) is called from ANOTHER thread and must not block."""
        self._subs.append(fn)

    def add(self, rec: dict) -> dict:
        rec = dict(rec)
        rec["text"] = redact(str(rec.get("text", "")))
        with self._lock:
            self._seq += 1
            rec["seq"] = self._seq
            self._items.append(rec)
            if len(self._items) > self.maxlen:
                cut = len(self._items) - self.maxlen
                del self._items[:cut]
                self._dropped += cut
        for fn in self._subs:
            try:
                fn(rec)
            except Exception:
                pass
        return rec

    def query(self, since: int = 0, limit: int = 300, level: str | None = None,
              q: str | None = None, src: str | None = None) -> list[dict]:
        min_lvl = level_no(level) if level else 0
        ql = (q or "").lower().strip()
        with self._lock:
            items = list(self._items)
        out = []
        for r in items:
            if r["seq"] <= since:
                continue
            if min_lvl and level_no(r.get("level")) < min_lvl:
                continue
            if src and r.get("src") != src:
                continue
            if ql and ql not in (r.get("text", "") + " " + r.get("logger", "")).lower():
                continue
            out.append(r)
        return out[-limit:]

    @property
    def dropped(self) -> int:
        return self._dropped

    def last_seq(self) -> int:
        with self._lock:
            return self._seq


class BufferHandler(logging.Handler):
    """The brain's own records go into the same buffer as the detector's lines."""

    def __init__(self, buf: LogBuffer, src: str = "brain") -> None:
        super().__init__(level=logging.INFO)
        self.buf = buf
        self.src = src

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + self.format_exc(record)
            self.buf.add({"ts": record.created, "level": record.levelname.lower(),
                          "logger": record.name, "text": text, "src": self.src,
                          "raw": False})
        except Exception:
            pass

    def format_exc(self, record: logging.LogRecord) -> str:
        import traceback
        return "".join(traceback.format_exception(*record.exc_info)).rstrip()
