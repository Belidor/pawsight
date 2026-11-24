"""The database has one owner.

Incident 2026-09-06: the brain lived in a container, storage/ came in as a
bind mount, and the host command `pawsight today` opened the same events.db
with its own connection. POSIX locks do not cross the Docker Desktop VM
boundary, so neither side saw the other: the host process recreated -wal and
-shm on exit, and the brain stayed bound to deleted inodes. Result: "disk I/O
error" on every dashboard request and 23 minutes of observations written into
an orphaned WAL and lost.

This checks that a repeat is impossible by design, not by convention.
"""
import contextlib
import io
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from pawsight import config as cfgmod
from pawsight.store import ForeignDatabase, Store


class StackNormalisation(unittest.TestCase):
    """PAWSIGHT_STACK is read leniently as to spelling and strictly as to meaning."""

    def test_native_is_recognised(self):
        for raw in ("native", "  native  ", "NATIVE", "'native'", '"native"',
                    "native  # broker from Homebrew"):
            with self.subTest(raw=raw):
                self.assertEqual(cfgmod._stack(raw), "native")

    def test_missing_key_means_docker(self):
        # start.sh picks the default the same way: no key means the docker stack.
        self.assertEqual(cfgmod._stack(None), "docker")
        self.assertEqual(cfgmod._stack(""), "docker")

    def test_unclear_value_means_docker(self):
        # Fail-closed: a typo must not silently open someone else's database to the host.
        for raw in ("nativ", "natve", "docker", "1", "no"):
            with self.subTest(raw=raw):
                self.assertEqual(cfgmod._stack(raw), "docker")


class WhoseDatabase(unittest.TestCase):
    def setUp(self):
        self._stack, self._cont = cfgmod.STACK, cfgmod.IN_CONTAINER
        self.addCleanup(self._restore)

    def _restore(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = self._stack, self._cont

    def test_host_is_not_owner_with_docker_stack(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        self.assertTrue(cfgmod.db_is_foreign(cfgmod.SHARED_DB))

    def test_inside_container_db_is_own(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", True
        self.assertFalse(cfgmod.db_is_foreign(cfgmod.SHARED_DB))

    def test_native_mode_db_is_own(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "native", False
        self.assertFalse(cfgmod.db_is_foreign(cfgmod.SHARED_DB))

    def test_worktree_db_is_not_blocked(self):
        """The container mounts the MAIN checkout's storage/, not the worktree's.

        Without this the gate would lock a database nobody mounted: there is no
        .env in the worktree, so STACK=docker, and its own database would be
        considered "foreign".
        """
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        own = cfgmod.ROOT / "storage" / "events.db"
        if cfgmod.SHARED_DB == own:
            self.skipTest("not running from a git worktree: nothing to check")
        self.assertFalse(cfgmod.db_is_foreign(own))
        self.assertTrue(cfgmod.db_is_foreign(cfgmod.SHARED_DB))

    def test_other_db_is_not_blocked(self):
        """Exactly the shared file is forbidden, not any SQLite.

        Otherwise tests with a temporary database, `pawsight batch` with its own
        path, and work in a git worktree, where storage/ is different
        altogether, would all break.
        """
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(cfgmod.db_is_foreign(Path(d) / "events.db"))


class DatabaseDoor(unittest.TestCase):
    def setUp(self):
        self._stack, self._cont = cfgmod.STACK, cfgmod.IN_CONTAINER
        self._shared = cfgmod.SHARED_DB
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _restore(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = self._stack, self._cont
        cfgmod.SHARED_DB = self._shared

    def test_store_refuses_to_open_foreign_db(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        with self.assertRaises(ForeignDatabase):
            Store(cfgmod.SHARED_DB)

    def test_refusal_happens_BEFORE_wal_is_created(self):
        """The key property: the host neither creates nor deletes -wal/-shm files.

        Recreating them from the host is exactly what orphaned the brain's connection.
        """
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        shared = Path(self.tmp.name) / "events.db"
        cfgmod.SHARED_DB = shared
        sqlite3.connect(shared).close()          # a database "as if from the brain"
        before = sorted(p.name for p in Path(self.tmp.name).iterdir())
        with self.assertRaises(ForeignDatabase):
            Store(shared)
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), before,
                         "the host must not leave a single SQLite file behind")

    def test_refusal_has_no_bypass_variable(self):
        """The refusal text must not hint at how to lift the gate.

        The incident was caused by exactly the one who reads error messages. A
        knob like PAWSIGHT_DB_UNSAFE=1 would bring the hole back the first time.
        """
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        with self.assertRaises(ForeignDatabase) as e:
            Store(cfgmod.SHARED_DB)
        text = str(e.exception)
        self.assertNotIn("UNSAFE", text.upper())
        self.assertNotIn("PAWSIGHT_STACK=native", text,
                         "a hint to switch the mode is exactly the gate bypass")

    def test_own_db_opens_as_before(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        st = Store(Path(self.tmp.name) / "own.db")
        self.addCleanup(st.close)
        self.assertEqual(st.today(0), [])


class OrphanedWalWatchdog(unittest.TestCase):
    """The second line: if the door is bypassed anyway, the system will not stay silent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.db"

    def test_intact_connection_is_not_orphaned(self):
        st = Store(self.path)
        self.addCleanup(st.close)
        st.add_camlog(1.0, "test", "info", "line")
        self.assertFalse(st.wal_orphaned())

    def test_wal_swap_from_outside_is_noticed(self):
        st = Store(self.path)
        self.addCleanup(st.close)
        st.add_camlog(1.0, "test", "info", "line")
        self.assertFalse(st.wal_orphaned())
        # exactly what the host process did on 2026-09-06 at 19:18:20:
        # the files at the path were replaced, while our connection stayed on the old inodes
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                p.unlink()
                p.write_bytes(b"")
        self.assertTrue(st.wal_orphaned(),
                        "a swap of SQLite side files must be noticed right away")


class OneDoor(unittest.TestCase):
    def test_exactly_one_sqlite3_connect_in_package(self):
        """A lock on the door.

        The gate sits in Store.__init__, and it is useless if a second bare
        sqlite3.connect appears next to it; there was exactly one in
        web.drinks_today, and it failed on every dashboard request.
        """
        root = Path(__file__).resolve().parent.parent / "pawsight"
        places = []
        for f in sorted(root.rglob("*.py")):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if "sqlite3.connect(" in line and not line.lstrip().startswith("#"):
                    places.append(f"{f.relative_to(root)}:{n}")
        self.assertEqual(len(places), 1,
                         f"database access must go only through Store, but found: {places}")
        self.assertTrue(places[0].startswith("store.py"),
                        f"the only connect must live in store.py, but it is in {places[0]}")


class HostReports(unittest.TestCase):
    """`pawsight today` and `drinks` must work on the host (AGENTS.md, 5)."""

    def setUp(self):
        self._stack, self._cont = cfgmod.STACK, cfgmod.IN_CONTAINER
        self.addCleanup(self._restore)

    def _restore(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = self._stack, self._cont

    def test_docker_mode_gets_data_from_brain_not_db(self):
        from pawsight import __main__ as m

        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        calls = []

        def fake(path, params):
            calls.append((path, params))
            return {"summary": [], "events": []}

        real, m._report_get = m._report_get, fake
        self.addCleanup(setattr, m, "_report_get", real)

        def forbidden(*a, **k):
            raise AssertionError("in docker mode the database must not be opened from the host")

        realstore, m.Store = m.Store, forbidden
        self.addCleanup(setattr, m, "Store", realstore)

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.today(cfgmod.Config()), 0)
        self.assertEqual(calls[0][0], "/api/report/today")
        self.assertIn("since", calls[0][1])

    def test_old_image_is_recognised_separately(self):
        """404 means "the brain runs an old image", not "the brain is not up".

        HTTPError subclasses URLError, so with a single except both cases gave
        the advice "check docker compose ps", which shows running in this
        state, leaving the person at a dead end. The real cure is a rebuild:
        the Dockerfile copies pawsight/ INTO the image.
        """
        import http.server
        import threading

        from pawsight import __main__ as m

        class NotFound(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                      # noqa: N802
                self.send_error(404)

            def log_message(self, *a):             # keep test output quiet
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), NotFound)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        os.environ["PAWSIGHT_WEB_PORT"] = str(srv.server_address[1])
        self.addCleanup(os.environ.pop, "PAWSIGHT_WEB_PORT", None)

        with self.assertRaises(m.BrainUnreachable) as e:
            m._report_get("/api/report/today", {"since": 0})
        text = str(e.exception)
        self.assertIn("old image", text)
        self.assertIn("./start.sh", text)
        self.assertNotIn("did not respond", text, "the brain did answer: this is a different case")

    def test_day_boundary_is_computed_on_host(self):
        """Otherwise the image's TZ would silently shift "today" to another day."""
        from pawsight import __main__ as m

        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False
        caught = {}

        def fake(path, params):
            caught.update(params)
            return {"rows": []}

        real, m._report_get = m._report_get, fake
        self.addCleanup(setattr, m, "_report_get", real)
        with contextlib.redirect_stdout(io.StringIO()):
            m.drinks(cfgmod.Config(), 3)
        self.assertAlmostEqual(float(caught["since"]), m._day_start() - 2 * 86400, places=3)


class SharedConnection(unittest.TestCase):
    """One connection, many threads.

    The brain writes the activity feed from the MQTT thread, the dashboard
    reads the summary from a pool thread, over the very same connection. The
    dashboard used to open ITS OWN per request and never crossed the writer;
    that connection had to go (it was the one failing with "disk I/O error"),
    and the accidental protection went with it. Without the lock the same code
    gives "InterfaceError: bad parameter or other API misuse" and corrupted
    tuples: measured, 14 failures in 6 s.
    """

    def test_writer_and_readers_do_not_interfere(self):
        import threading

        with tempfile.TemporaryDirectory() as d:
            st = Store(Path(d) / "events.db")
            self.addCleanup(st.close)
            day0 = 1788700000.0
            errors: list = []
            stop = threading.Event()

            def write():
                i = 0
                while not stop.is_set():
                    try:
                        st.add_camlog_many(
                            [(day0 + j, "manual", "info", f"line {j}") for j in range(64)])
                        st.add_bout("water", day0 + i, day0 + i + 5, 7, kind="drink")
                        i += 1
                    except Exception as e:
                        errors.append(repr(e))

            def read():
                while not stop.is_set():
                    try:
                        st.bouts_by_kind(0, "drink")
                        st.bout_summary(0)
                        st.events_recent(8)
                        st.camlog_recent(60)
                    except Exception as e:
                        errors.append(repr(e))

            threads = [threading.Thread(target=write),
                      *[threading.Thread(target=read) for _ in range(4)]]
            for t in threads:
                t.start()
            time.sleep(2.0)
            stop.set()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(errors, [], "the shared connection must be under the lock")
            self.assertGreater(st.scalar("SELECT COUNT(*) FROM camera_log"), 0,
                               "the writer should have written something")


class OutputMatches(unittest.TestCase):
    """Section 5's promise: `pawsight today` prints the same thing in both stacks.

    The match must hold by construction: the route through the brain returns
    RAW rows, and the very same CLI formats them. That is what is checked here,
    along with the tuples-to-lists conversion JSON does on the way.
    """

    def setUp(self):
        self._stack, self._cont, self._shared = (
            cfgmod.STACK, cfgmod.IN_CONTAINER, cfgmod.SHARED_DB)
        self.addCleanup(self._restore)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        from pawsight import __main__ as m
        self.m = m
        day = m._day_start()
        self.db = Path(self.tmp.name) / "events.db"
        st = Store(self.db)
        st.add_bout("water", day + 60, day + 107, 7, kind="drink")
        st.add_bout("food", day + 300, day + 340, 7, kind="eat")
        st.add(day + 61, 7, "water", "drinking", "drinking water", None)
        st.close()

        self.cfg = cfgmod.Config()
        self.cfg.db_path = self.db

    def _restore(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = self._stack, self._cont
        cfgmod.SHARED_DB = self._shared

    def _capture(self, call) -> str:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            call()
        return buf.getvalue()

    def _via_brain(self, report):
        """A dashboard with the database already open, like the brain inside the container."""
        import json

        from pawsight.web import Dashboard

        cfgmod.STACK, cfgmod.IN_CONTAINER = "native", False
        dash = Dashboard(self.cfg)
        dash.store()                       # the owner holds the connection
        self.addCleanup(dash.store().close)
        cfgmod.SHARED_DB = self.db
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False

        def fake(path, params):
            since = float(params["since"])
            data = (dash._report_today(since) if path.endswith("today")
                    else dash._report_drinks(since))
            return json.loads(json.dumps(data))   # tuples -> lists, as over HTTP

        real, self.m._report_get = self.m._report_get, fake
        self.addCleanup(setattr, self.m, "_report_get", real)
        return self._capture(report)

    def test_today_prints_the_same(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "native", False
        native_out = self._capture(lambda: self.m.today(self.cfg))
        via_brain = self._via_brain(lambda: self.m.today(self.cfg))
        self.assertEqual(native_out, via_brain)
        self.assertIn("drank", native_out)

    def test_drinks_prints_the_same(self):
        cfgmod.STACK, cfgmod.IN_CONTAINER = "native", False
        native_out = self._capture(lambda: self.m.drinks(self.cfg, 1))
        via_brain = self._via_brain(lambda: self.m.drinks(self.cfg, 1))
        self.assertEqual(native_out, via_brain)
        self.assertIn("visits: 1", native_out)

    def test_silent_brain_is_no_reason_to_open_db(self):
        """There is deliberately no fallback: it is exactly what would bring the incident back."""
        cfgmod.SHARED_DB = self.db
        cfgmod.STACK, cfgmod.IN_CONTAINER = "docker", False

        def forbidden(*a, **k):
            raise AssertionError("the CLI went into the database bypassing the brain")

        real, self.m.Store = self.m.Store, forbidden
        self.addCleanup(setattr, self.m, "Store", real)
        # Take a port that is known to be free. A constant like 8299 is an
        # assumption about the machine, not a property of the code: a busy port
        # either fails the test or (worse) quietly returns a real answer, and
        # the test passes without checking anything.
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.environ["PAWSIGHT_WEB_PORT"] = str(port)
        self.addCleanup(os.environ.pop, "PAWSIGHT_WEB_PORT", None)
        output = self._capture(lambda: self.assertEqual(self.m.today(self.cfg), 1))
        self.assertIn("The brain did not respond", output)
        self.assertNotIn("Bowl visits", output, "there must be no report data here")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
