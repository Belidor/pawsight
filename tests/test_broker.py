"""The desktop app's embedded broker: loopback only, the .env login, retained status.

The end-to-end part needs amqtt (installed with the app); it is skipped
without it, the credential check itself is always tested.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
import unittest

from pawsight import broker


class Credentials(unittest.TestCase):
    def test_exact_login_only(self):
        self.assertTrue(broker.credentials_ok("pawsight", b"pw", "pawsight", "pw"))
        self.assertFalse(broker.credentials_ok("pawsight", b"nope", "pawsight", "pw"))
        self.assertFalse(broker.credentials_ok("other", b"pw", "pawsight", "pw"))
        self.assertFalse(broker.credentials_ok(None, None, "pawsight", "pw"))

    def test_no_login_configured_means_anonymous(self):
        self.assertTrue(broker.credentials_ok(None, None, "", ""))

    def test_listener_is_loopback(self):
        cfg = broker.broker_config(1883, "u", "p")
        self.assertEqual(cfg["listeners"]["default"]["bind"], "127.0.0.1:1883")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(broker.BaseAuthPlugin is not object, "amqtt is not installed")
class EndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        self.port = _free_port()
        self.loop = asyncio.new_event_loop()
        self.stop = None
        ready = threading.Event()

        def run() -> None:
            asyncio.set_event_loop(self.loop)

            async def go():
                self.stop = asyncio.Event()
                ready.set()
                await broker.serve(self.port, "pawsight", "s3cret", self.stop)
            self.loop.run_until_complete(go())

        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        ready.wait(5)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)

    def tearDown(self) -> None:
        self.loop.call_soon_threadsafe(self.stop.set)
        self.t.join(10)

    def _client(self, user, pw):
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        c.username_pw_set(user, pw)
        rc = []
        c.on_connect = lambda c, u, f, r, p: rc.append(r)
        c.connect("127.0.0.1", self.port)
        c.loop_start()
        end = time.monotonic() + 5
        while not rc and time.monotonic() < end:
            time.sleep(0.05)
        return c, rc

    def test_wrong_password_is_refused(self):
        c, rc = self._client("pawsight", "wrong")
        try:
            self.assertTrue(rc and rc[0].is_failure)
        finally:
            c.loop_stop()

    def test_bus_traffic_and_retained_status(self):
        a, rc = self._client("pawsight", "s3cret")
        self.assertFalse(rc[0].is_failure)
        a.publish("pawsight/status", b"retained", qos=1, retain=True).wait_for_publish(5)
        got = []
        b, _ = self._client("pawsight", "s3cret")
        b.on_message = lambda c, u, m: got.append((m.topic, len(m.payload)))
        b.subscribe("pawsight/#")
        time.sleep(0.5)
        a.publish("pawsight/observations", b"x" * 200_000).wait_for_publish(5)
        end = time.monotonic() + 5
        while len(got) < 2 and time.monotonic() < end:
            time.sleep(0.05)
        a.loop_stop()
        b.loop_stop()
        self.assertIn(("pawsight/status", 8), got)
        self.assertIn(("pawsight/observations", 200_000), got)


if __name__ == "__main__":
    unittest.main()
