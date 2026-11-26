"""The cross-platform supervisor (python -m pawsight run) used by the desktop app.

Real child processes (tiny Python one-liners), no go2rtc, no torch: the tests
check the supervisor's own promises: restart with backoff, the brain's port
being taken is a refusal, a .env change restarts the detector, logs rotate
instead of being truncated.
"""
from __future__ import annotations

import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pawsight import runner
from pawsight.runner import Runner, Service

SLEEP = [sys.executable, "-c", "import time; time.sleep(60)"]
DIE = [sys.executable, "-c", "raise SystemExit(3)"]


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "storage").mkdir()
        self.clock = FakeClock()
        self.r = Runner(self.root, go2rtc=Path("/nonexistent/go2rtc"), clock=self.clock)
        self.r.services = []

    def tearDown(self) -> None:
        self.r.stop_all()
        self.tmp.cleanup()

    def svc(self, name, argv, **kw) -> Service:
        s = Service(name, argv, self.root / "storage" / f"{name}.log", **kw)
        self.r.services.append(s)
        return s

    def wait_dead(self, s: Service) -> None:
        end = time.monotonic() + 10
        while s.proc is not None and s.proc.poll() is None and time.monotonic() < end:
            time.sleep(0.05)


class Restarts(_Base):
    def test_crashed_child_is_restarted_with_growing_pause(self):
        s = self.svc("detector", DIE)
        self.assertTrue(self.r.start(s))
        self.wait_dead(s)
        self.r.tick()                       # notices the death
        self.assertIsNone(s.proc)
        self.assertEqual(s.failures, 1)
        self.assertEqual(s.next_start, self.clock.t + 1)
        self.r.tick()                       # too early: not restarted yet
        self.assertIsNone(s.proc)
        self.clock.t += 1
        self.r.tick()
        self.assertIsNotNone(s.proc)
        self.wait_dead(s)
        self.r.tick()
        self.assertEqual(s.failures, 2)
        self.assertEqual(s.next_start, self.clock.t + 2)

    def test_backoff_is_capped(self):
        s = self.svc("detector", DIE)
        s.failures = 20
        self.r.start(s)
        self.wait_dead(s)
        self.r.tick()
        self.assertEqual(s.next_start - self.clock.t, runner.BACKOFF_MAX)

    def test_backoff_resets_after_healthy_minute(self):
        s = self.svc("brain", SLEEP)
        s.failures = 4
        self.r.start(s)
        self.clock.t += runner.HEALTHY_AFTER + 1
        self.r.tick()
        self.assertEqual(s.failures, 0)

    def test_stop_all_stops_children(self):
        a, b = self.svc("a", SLEEP), self.svc("b", SLEEP)
        self.r.start(a)
        self.r.start(b)
        procs = [a.proc, b.proc]
        self.r.stop_all()
        for p in procs:
            self.assertIsNotNone(p.poll())


class Ports(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]

    def tearDown(self) -> None:
        self.sock.close()
        super().tearDown()

    def test_taken_brain_port_is_a_refusal(self):
        s = self.svc("brain", SLEEP, port=self.port, port_taken_is_error=True)
        self.assertFalse(self.r.start(s))
        self.assertIsNone(s.proc)

    def test_someone_elses_broker_is_used_not_duplicated(self):
        s = self.svc("broker", SLEEP, port=self.port)
        self.assertTrue(self.r.start(s))
        self.assertIsNone(s.proc)
        self.assertTrue(s.external)
        self.r.tick()                       # an external service is not "dead"
        self.assertEqual(s.failures, 0)


class EnvChange(_Base):
    def test_env_change_restarts_detector_after_debounce(self):
        env = self.root / ".env"
        env.write_text("A=1\n")
        self.r._env_mtime = self.r._mtime(env)
        det = self.svc("detector", SLEEP)
        brain = self.svc("brain", SLEEP)
        self.r.start(det)
        self.r.start(brain)
        first_det, first_brain = det.proc.pid, brain.proc.pid
        env.write_text("A=2\n")
        import os
        os.utime(env, (time.time() + 5, time.time() + 5))
        self.r.tick()                       # change seen, debounce running
        self.assertEqual(det.proc.pid, first_det)
        self.clock.t += runner.ENV_DEBOUNCE
        self.r.tick()
        self.assertNotEqual(det.proc.pid, first_det)
        self.assertEqual(brain.proc.pid, first_brain)


class Logs(_Base):
    def test_previous_log_is_kept_as_dot_one(self):
        s = self.svc("detector", [sys.executable, "-c", "print('second run')"])
        s.log_path.write_text("first run\n")
        self.r.start(s)
        self.wait_dead(s)
        self.assertEqual((self.root / "storage" / "detector.log.1").read_text(), "first run\n")
        self.assertIn("second run", s.log_path.read_text())


class Definition(unittest.TestCase):
    def test_services_and_ports_from_env(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / ".env").write_text("PAWSIGHT_MQTT_PORT=1999\nPAWSIGHT_WEB_PORT=8111\n")
            fake = root / "go2rtc-bin"
            r = Runner(root, python="py", go2rtc=fake)
            names = [s.name for s in r.services]
            self.assertEqual(names, ["go2rtc", "broker", "brain", "detector"])
            self.assertEqual(r.service("broker").port, 1999)
            self.assertEqual(r.service("brain").port, 8111)
            self.assertTrue(r.service("brain").port_taken_is_error)
            self.assertEqual(r.service("detector").argv, ["py", "-m", "pawsight", "detect"])
            self.assertEqual(r.service("go2rtc").argv[1:], ["-config", "go2rtc/go2rtc.yaml"])

    def test_local_go2rtc_build_wins(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            exe = root / "bin" / ("go2rtc.exe" if sys.platform == "win32" else "go2rtc")
            exe.parent.mkdir()
            exe.write_text("")
            self.assertEqual(runner.go2rtc_binary(root), exe)

    def test_children_run_native_stack(self):
        with tempfile.TemporaryDirectory() as d:
            env = Runner(Path(d), go2rtc=Path("x"))._child_env()
            self.assertEqual(env["PAWSIGHT_STACK"], "native")
            self.assertEqual(env["PYTHONUNBUFFERED"], "1")


if __name__ == "__main__":
    unittest.main()
