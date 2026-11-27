"""Embedded MQTT broker for the desktop app: `python -m pawsight broker`.

Why not mosquitto. The desktop app has to run on Windows too, and there
mosquitto is a separate installer and a system service; on macOS it needs
Homebrew. amqtt is pure Python and installs into the same .venv, so the app
needs nothing from the system. The CLI install (install.sh + start.sh) keeps
mosquitto: nothing changes for it.

The rules are the same as for mosquitto/native.conf:
  * loopback only: the listener is bound to 127.0.0.1, never 0.0.0.0;
  * with a password when .env has PAWSIGHT_MQTT_USER/PAWSIGHT_MQTT_PASS
    (written by `pawsight.authsetup --mqtt`, which appinit runs on a new
    install); without them the broker is anonymous, like an old native install.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
from dataclasses import dataclass

log = logging.getLogger("pawsight.broker")

HOST = "127.0.0.1"

try:  # amqtt is only needed by the app; the brain container and the tests may lack it
    from amqtt.plugins.base import BaseAuthPlugin
except ImportError:  # pragma: no cover - exercised only without amqtt
    BaseAuthPlugin = object  # type: ignore[assignment,misc]


def _b(v) -> bytes:
    if v is None:
        return b""
    return v if isinstance(v, bytes) else str(v).encode()


def credentials_ok(user, password, want_user: str, want_pass: str) -> bool:
    """Constant-time check; an empty expected login means an anonymous broker."""
    if not want_user:
        return True
    return (hmac.compare_digest(_b(user), _b(want_user))
            and hmac.compare_digest(_b(password), _b(want_pass)))


class EnvAuthPlugin(BaseAuthPlugin):  # type: ignore[misc,valid-type]
    """Accepts exactly one login: the one in .env. No password file, no hash:
    the clear-text password is already in .env (the detector and the brain read
    it from there), so a second copy as a hash would add nothing."""

    @dataclass
    class Config:
        username: str = ""
        password: str = ""

    async def authenticate(self, *, session) -> bool:  # amqtt API
        ok = credentials_ok(session.username, session.password,
                            self.config.username, self.config.password)
        if not ok:
            log.warning("refused an MQTT login as %r", session.username)
        return ok


def broker_config(port: int, username: str, password: str) -> dict:
    return {
        "listeners": {"default": {"type": "tcp", "bind": f"{HOST}:{port}"}},
        "plugins": {
            "pawsight.broker.EnvAuthPlugin": {"username": username, "password": password},
            # rejects publishing to $SYS and the like, as mosquitto does
            "amqtt.plugins.topic_checking.TopicTabooPlugin": {},
        },
    }


async def serve(port: int, username: str, password: str,
                stop: asyncio.Event | None = None) -> None:
    from amqtt.broker import Broker

    broker = Broker(broker_config(port, username, password))
    await broker.start()
    log.info("MQTT broker listening on %s:%s (%s)", HOST, port,
             "with password" if username else "anonymous")
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: no signal handlers in asyncio
            pass
    try:
        await stop.wait()
    finally:
        await broker.shutdown()
        log.info("MQTT broker stopped")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    # amqtt logs every packet at INFO; keep only problems
    for noisy in ("amqtt", "transitions"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from .config import ROOT
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    port = int(os.getenv("PAWSIGHT_MQTT_PORT", "1883"))
    try:
        asyncio.run(serve(port, os.getenv("PAWSIGHT_MQTT_USER", ""),
                          os.getenv("PAWSIGHT_MQTT_PASS", "")))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
