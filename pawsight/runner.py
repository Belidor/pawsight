"""`python -m pawsight run`: the whole system in one foreground process.

This is start.sh + stop.sh rewritten in Python so that it works the same on
macOS, Linux and Windows. The desktop app starts exactly this; from a
terminal it works too (Ctrl+C stops everything).

What it does, in order:
  go2rtc  -> embedded MQTT broker (pawsight.broker) -> brain -> detector
then watches them:
  * a child that dies is restarted, with a growing pause (1, 2, 4 ... 60 s),
    so a crash loop does not eat the CPU; the pause resets after a minute of
    normal work;
  * .env changed (the setup page writes the camera mode there) -> the
    detector is restarted, because it reads .env only at start;
  * on exit everything is stopped in reverse order.

The rules of start.sh hold here too:
  * every service listens on 127.0.0.1 only (go2rtc.yaml, broker, brain);
  * logs are rotated to *.1 before a start, never truncated: incidents are
    analysed from the previous run (AGENTS.md, section 5);
  * a service that is already listening on its port is NOT started a second
    time (someone else's go2rtc or broker), the brain's port being taken is an
    error: two brains on one database is the 2026-09-06 incident.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import procutil

log = logging.getLogger("pawsight.runner")

BACKOFF_MAX = 60.0
HEALTHY_AFTER = 60.0          # seconds of uptime after which the backoff resets
ENV_DEBOUNCE = 2.0


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def rotate(path: Path) -> None:
    """mv log log.1 (replacing the old .1). Done BEFORE the writer opens it."""
    if path.exists():
        try:
            os.replace(path, path.with_name(path.name + ".1"))
        except OSError as e:     # Windows: someone still holds the file
            log.warning("could not rotate %s: %s", path.name, e)


def go2rtc_binary(root: Path) -> Path | None:
    """bin/go2rtc in the install (a patched build wins), else the app's own copy."""
    exe = "go2rtc.exe" if procutil.WINDOWS else "go2rtc"
    local = root / "bin" / exe
    if local.exists():
        return local
    env = os.getenv("PAWSIGHT_GO2RTC_BIN")
    if env and Path(env).exists():
        return Path(env)
    found = shutil.which("go2rtc")
    return Path(found) if found else None


@dataclass
class Service:
    name: str
    argv: list[str]
    log_path: Path
    port: int | None = None       # what it listens on, if anything
    port_taken_is_error: bool = False
    proc: subprocess.Popen | None = None
    started: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    external: bool = False        # someone else already serves the port
    env: dict = field(default_factory=dict)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


class Runner:
    def __init__(self, root: Path, python: str | None = None,
                 go2rtc: Path | None = None, clock=time.monotonic) -> None:
        self.root = root
        self.python = python or sys.executable
        self.go2rtc = go2rtc if go2rtc is not None else go2rtc_binary(root)
        self.clock = clock
        self.stopping = threading.Event()
        self._env_mtime = self._mtime(root / ".env")
        self._env_changed_at: float | None = None
        self.services = self._services()

    # ---- definition ----

    def _services(self) -> list[Service]:
        from dotenv import dotenv_values

        env = dotenv_values(self.root / ".env") if (self.root / ".env").exists() else {}
        mqtt_port = int(env.get("PAWSIGHT_MQTT_PORT") or 1883)
        web_port = int(env.get("PAWSIGHT_WEB_PORT") or 8099)
        st = self.root / "storage"
        py = [self.python, "-m", "pawsight"]
        out = []
        if self.go2rtc is not None:
            out.append(Service("go2rtc", [str(self.go2rtc), "-config", "go2rtc/go2rtc.yaml"],
                               self.root / "go2rtc" / "go2rtc.log", port=1984))
        else:
            log.error("go2rtc not found: put it in bin/ or set PAWSIGHT_GO2RTC_BIN")
        out += [
            Service("broker", py + ["broker"], st / "broker.log", port=mqtt_port),
            Service("brain", py + ["brain"], st / "brain.log", port=web_port,
                    port_taken_is_error=True),
            Service("detector", py + ["detect"], st / "detect.log"),
        ]
        return out

    def service(self, name: str) -> Service:
        return next(s for s in self.services if s.name == name)

    # ---- process control ----

    def _child_env(self) -> dict:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PAWSIGHT_STACK": "native"}
        env.pop("PYTHONHOME", None)
        return env

    def start(self, s: Service) -> bool:
        if s.alive():
            return True
        if s.port and port_open(s.port):
            if s.port_taken_is_error:
                log.error("%s: port %s is already taken; is another pawsight running? "
                          "Not starting a second one.", s.name, s.port)
                return False
            if not s.external:
                log.info("%s: something already listens on %s, using it", s.name, s.port)
            s.external = True
            return True
        s.external = False
        s.log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate(s.log_path)
        fh = open(s.log_path, "wb")
        try:
            s.proc = subprocess.Popen(
                s.argv, cwd=str(self.root), stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env={**self._child_env(), **s.env},
                creationflags=procutil.NO_WINDOW if procutil.WINDOWS else 0)
        except OSError as e:
            log.error("%s did not start: %s", s.name, e)
            return False
        finally:
            fh.close()                 # the child keeps its own handle
        s.started = self.clock()
        log.info("%s started (pid %s)", s.name, s.proc.pid)
        return True

    def stop(self, s: Service, timeout: float = 10.0) -> None:
        p = s.proc
        if p is None or p.poll() is not None:
            s.proc = None
            return
        try:
            if procutil.WINDOWS:
                p.terminate()
            else:
                p.send_signal(signal.SIGTERM)
            p.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("%s did not stop in %.0f s, killing it", s.name, timeout)
            p.kill()
            p.wait(5)
        except OSError:
            pass
        s.proc = None
        log.info("%s stopped", s.name)

    def wait_port(self, s: Service, timeout: float) -> bool:
        if not s.port:
            return True
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.stopping.is_set():
            if port_open(s.port):
                return True
            if s.proc is not None and s.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return port_open(s.port)

    # ---- lifecycle ----

    def start_all(self) -> bool:
        ok = True
        for s in self.services:
            if self.stopping.is_set():
                return False
            if not self.start(s):
                if s.port_taken_is_error:
                    return False
                ok = False
                continue
            if s.port and not s.external and not self.wait_port(s, 30):
                log.warning("%s is not answering on %s yet; see %s", s.name, s.port,
                            s.log_path.relative_to(self.root))
            if s.name == "go2rtc":
                self._xiaomi_substream()
        return ok

    def stop_all(self) -> None:
        for s in reversed(self.services):
            self.stop(s)

    def tick(self) -> None:
        """One pass of the watch loop; separate from run() for the tests."""
        now = self.clock()
        for s in self.services:
            if s.external or s.alive():
                if s.alive() and s.failures and now - s.started > HEALTHY_AFTER:
                    s.failures = 0
                continue
            if s.proc is not None:            # it was running and died
                rc = s.proc.returncode
                s.proc = None
                s.failures += 1
                delay = min(BACKOFF_MAX, 2.0 ** (s.failures - 1))
                s.next_start = now + delay
                log.warning("%s exited (code %s); restarting in %.0f s, see %s",
                            s.name, rc, delay, s.log_path.name)
                continue
            if now >= s.next_start:
                self.start(s)
        self._watch_env(now)

    def _watch_env(self, now: float) -> None:
        m = self._mtime(self.root / ".env")
        if m != self._env_mtime:
            self._env_mtime = m
            self._env_changed_at = now
        if self._env_changed_at is not None and now - self._env_changed_at >= ENV_DEBOUNCE:
            self._env_changed_at = None
            det = self.service("detector")
            if det.alive():
                log.info(".env changed: restarting the detector so it picks the new settings up")
                self.stop(det)
                det.failures = 0
                self.start(det)

    def run(self, watch_stdin: bool = False) -> int:
        (self.root / "storage").mkdir(exist_ok=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, lambda *_: self.stopping.set())
            except (ValueError, OSError):
                pass
        if watch_stdin:
            # The desktop app holds our stdin. When the app dies, even by
            # force, the pipe closes and we stop everything: no orphaned
            # detector holding the GPU after the app is gone.
            threading.Thread(target=self._stdin_watch, daemon=True).start()
        try:
            if not self.start_all():
                if any(s.port_taken_is_error and not s.alive() for s in self.services):
                    return 1
            log.info("pawsight is running: http://127.0.0.1:%s", self.service("brain").port)
            while not self.stopping.wait(1.0):
                self.tick()
        finally:
            self.stop_all()
        return 0

    def _stdin_watch(self) -> None:
        try:
            while sys.stdin.buffer.read(1):
                pass
        except Exception:
            pass
        log.info("the app closed our stdin: stopping")
        self.stopping.set()

    # ---- helpers ----

    @staticmethod
    def _mtime(p: Path) -> float | None:
        try:
            return p.stat().st_mtime
        except OSError:
            return None

    def _xiaomi_substream(self) -> None:
        """SD substream for the detector, Xiaomi cameras only (as start.sh does)."""
        try:
            import base64
            import urllib.parse
            import urllib.request

            import yaml

            g = yaml.safe_load((self.root / "go2rtc" / "go2rtc.yaml").read_text()) or {}
            base = (g.get("streams") or {}).get("dog_cam")
            if isinstance(base, list):
                base = base[0] if base else None
            if not base or not str(base).startswith("xiaomi://") or "subtype" in base:
                return
            src = base + ("&" if "?" in base else "?") + "subtype=sd"
            q = urllib.parse.urlencode({"name": "dog_cam_sd", "src": src})
            req = urllib.request.Request(f"http://127.0.0.1:1984/api/streams?{q}", method="PUT")
            api = g.get("api") or {}
            if api.get("username"):
                tok = base64.b64encode(
                    f'{api["username"]}:{api.get("password") or ""}'.encode()).decode()
                req.add_header("Authorization", "Basic " + tok)
            urllib.request.urlopen(req, timeout=20)
            log.info("SD substream set up")
        except Exception as e:
            log.info("SD substream: skipped (%s)", type(e).__name__)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pawsight run",
                                 description="Start go2rtc, the broker, the brain and the detector "
                                             "and keep them running.")
    ap.add_argument("--watch-stdin", action="store_true",
                    help="stop when stdin closes (used by the desktop app)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    from .config import ROOT

    return Runner(ROOT).run(watch_stdin=a.watch_stdin)
