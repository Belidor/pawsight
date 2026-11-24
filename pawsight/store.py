"""Event history in SQLite.

Store is the ONLY door into the database. There are no bare sqlite3.connect
calls left in the package, and a test checks that: the gate below is useless
if a second opener appears next to it (there was one in web.drinks_today, and
it failed on every dashboard request).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

from .config import db_is_foreign

log = logging.getLogger(__name__)


# Row cap for reports. The dashboard's other read routes cap their responses
# (activity feed: 300 rows, log: 1000), and reports must be no exception:
# without a cap, whoever sends `since` decides the response size. The value is
# generous: a day accumulates on the order of three hundred events.
REPORT_LIMIT = 5000


class ForeignDatabase(RuntimeError):
    """The database is owned by the brain in the container; the host must not open it."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    track_id        INTEGER,
    zone            TEXT,
    heuristic_state TEXT,
    vlm_text        TEXT,
    snapshot_path   TEXT,
    conflict        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_zone ON events(zone);

-- Bowl visit: from entering the zone until leaving it for longer than BOUT_GAP.
-- We count VISITS and TIME, not volume: millilitres cannot be measured from video.
CREATE TABLE IF NOT EXISTS bouts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    zone         TEXT    NOT NULL,
    start_ts     REAL    NOT NULL,
    end_ts       REAL    NOT NULL,
    duration_sec REAL    NOT NULL,
    track_id     INTEGER,
    -- 'drink' | 'eat' | 'unknown', decided by the VLM from the visit's frames
    kind         TEXT    NOT NULL DEFAULT 'unknown',
    -- which zone fired geometrically: water | food | NULL
    zone_hint    TEXT,
    -- 1 if geometry and the VLM disagree
    conflict     INTEGER NOT NULL DEFAULT 0,
    vlm_text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_bouts_zone_ts ON bouts(zone, start_ts);

-- Camera actions: search, following, edge of travel, tilt, stream gaps, manual pans.
CREATE TABLE IF NOT EXISTS camera_log (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    text  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_camlog_ts ON camera_log(ts);
"""


class Store:
    def __init__(self, path: Path) -> None:
        # Refusing BEFORE connect is the whole point. Even opening read-only
        # creates events.db-wal and -shm next to it, and closing deletes them:
        # that is exactly how the host orphaned the brain's connection on
        # 2026-09-06. A file that is never opened cannot be damaged.
        if db_is_foreign(path):
            raise ForeignDatabase(
                "storage/events.db belongs to the brain running in the container. "
                "It must not be opened with a second connection from the host: SQLite "
                "locks do not cross the Docker Desktop VM boundary, and the brain is "
                "left with an orphaned WAL, writing into nowhere (AGENTS.md, section 8). "
                "Get reports with `pawsight today` and `pawsight drinks`: on the host "
                "they ask the brain over HTTP and never touch the database. "
                "A native run requires the container to be stopped: ./stop.sh"
            )
        self.path = Path(path)
        self._wal_ref: tuple | None = None
        self._wal_warned = False
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=10.0)
        # WAL, not a rollback journal. Measured on the live system: one
        # "connect + insert + commit" cycle took 3.74 ms with a full fsync, and
        # it ran on the MQTT thread for every activity feed message. With the
        # extended feed the rate grows about tenfold. WAL removes both the fsync
        # cost and readers and the writer blocking each other.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        # The fingerprint is taken right after the first commit: by then the WAL
        # exists, and any later inode change means it was swapped from outside.
        self._wal_ref = self._wal_fingerprint()

    # ---- orphaned WAL watchdog ----

    def _wal_fingerprint(self) -> tuple:
        out = []
        for suffix in ("-wal", "-shm"):
            try:
                s = os.stat(f"{self.path}{suffix}")
                out.append((s.st_dev, s.st_ino))
            except OSError:
                out.append(None)
        return tuple(out)

    def wal_orphaned(self) -> bool:
        """SQLite's side files were swapped under us: writes go nowhere.

        The second line of defence, not the first: the door is closed by the
        gate above. But if it is bypassed anyway (a foreign script, a manual
        sqlite3, a future edit), the failure must be VISIBLE. In the 2026-09-06
        incident nobody noticed the swap: SQLite raised no exceptions, and the
        brain wrote into a deleted inode for three hours forty minutes while
        logging success.
        """
        now = self._wal_fingerprint()
        if self._wal_ref is None or all(x is None for x in self._wal_ref):
            if any(x is not None for x in now):
                self._wal_ref = now
            return False
        if now == self._wal_ref:
            return False
        if not self._wal_warned:
            self._wal_warned = True
            log.error("SQLite side files of %s were swapped from outside: the connection "
                      "is left on deleted inodes, and everything written from now on will "
                      "be lost. The database was opened by a second process; see "
                      "AGENTS.md, section 8", self.path.name)
        return True

    # Every access to the connection is under the lock. WHY: there is one
    # shared connection: the brain writes the activity feed from the MQTT
    # thread, the dashboard reads the summary from a pool thread.
    # sqlite3.threadsafety=3 allows sharing a connection but does not prevent a
    # cursor interleaving with someone else's commit: measured on this code,
    # 4 readers against one writer give fourteen failures in 6 s
    # ("InterfaceError: bad parameter or other API misuse") and corrupted rows
    # (IndexError while unpacking a tuple). This went unseen before only
    # because the dashboard opened a SEPARATE connection per request, the very
    # one that had to go (docs/architecture.md, "The database has one owner").
    # The lock is cheap: operations here take fractions of a millisecond.

    def add_camlog_many(self, rows: list[tuple]) -> None:
        """In a batch: the feed is written from the MQTT thread, and a commit per row is expensive."""
        if not rows:
            return
        with self.lock:
            self.db.executemany(
                "INSERT INTO camera_log (ts,kind,level,text) VALUES (?,?,?,?)", rows)
            self.db.commit()

    def camlog_trim(self, before_ts: float) -> int:
        with self.lock:
            cur = self.db.execute("DELETE FROM camera_log WHERE ts < ?", (before_ts,))
            self.db.commit()
            return cur.rowcount or 0

    def camlog_count_before(self, before_ts: float) -> int:
        return self.scalar("SELECT COUNT(*) FROM camera_log WHERE ts < ?", (before_ts,))

    def vacuum(self) -> None:
        """VACUUM takes an exclusive lock and may fail if the brain is writing an
        event at that moment. The exception propagates: we do not pretend it
        worked (the caller handles it)."""

        with self.lock:
            self.db.execute("VACUUM")

    def scalar(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
        return row[0] if row else 0

    def add(
        self,
        ts: float,
        track_id: int | None,
        zone: str | None,
        heuristic_state: str,
        vlm_text: str | None,
        snapshot_path: str | None,
        conflict: bool = False,
    ) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO events (ts,track_id,zone,heuristic_state,vlm_text,snapshot_path,"
                "conflict) VALUES (?,?,?,?,?,?,?)",
                (ts, track_id, zone, heuristic_state, vlm_text, snapshot_path, int(conflict)),
            )
            self.db.commit()
            return cur.lastrowid

    def add_bout(self, zone: str, start_ts: float, end_ts: float, track_id: int | None,
                 kind: str = "unknown", zone_hint: str | None = None,
                 conflict: bool = False, vlm_text: str | None = None) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO bouts (zone,start_ts,end_ts,duration_sec,track_id,kind,zone_hint,"
                "conflict,vlm_text) VALUES (?,?,?,?,?,?,?,?,?)",
                (zone, start_ts, end_ts, end_ts - start_ts, track_id, kind, zone_hint,
                 int(conflict), vlm_text),
            )
            self.db.commit()
            return cur.lastrowid

    def _rows(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def bouts_by_kind(self, since_ts: float, kind: str,
                      limit: int = REPORT_LIMIT) -> list[tuple]:
        return self._rows(
            "SELECT start_ts,duration_sec,conflict,vlm_text FROM bouts"
            " WHERE start_ts>=? AND kind=? ORDER BY start_ts LIMIT ?",
            (since_ts, kind, limit))

    def bout_summary(self, since_ts: float) -> list[tuple]:
        return self._rows(
            "SELECT kind, COUNT(*), SUM(duration_sec), SUM(conflict) FROM bouts"
            " WHERE start_ts>=? GROUP BY kind ORDER BY COUNT(*) DESC", (since_ts,))

    def add_camlog(self, ts: float, kind: str, level: str, text: str) -> None:
        with self.lock:
            self.db.execute("INSERT INTO camera_log (ts,kind,level,text) VALUES (?,?,?,?)",
                            (ts, kind, level, text))
            self.db.commit()

    def camlog_recent(self, limit: int = 60) -> list[dict]:
        rows = self._rows(
            "SELECT ts,kind,level,text FROM camera_log ORDER BY ts DESC LIMIT ?", (limit,))
        return [{"ts": r[0], "kind": r[1], "level": r[2], "text": r[3]} for r in rows]

    def events_recent(self, limit: int = 8) -> list[tuple]:
        return self._rows(
            "SELECT ts,zone,heuristic_state,vlm_text FROM events"
            " ORDER BY ts DESC LIMIT ?", (limit,))

    def today(self, since_ts: float, limit: int = REPORT_LIMIT) -> list[tuple]:
        return self._rows(
            "SELECT ts,zone,heuristic_state,vlm_text FROM events"
            " WHERE ts >= ? ORDER BY ts LIMIT ?", (since_ts, limit))

    def close(self) -> None:
        with self.lock:
            self.db.close()
