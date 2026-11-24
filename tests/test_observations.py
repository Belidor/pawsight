"""The VLM may wait, but the MQTT thread must keep accepting frames and /snap replies."""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from pawsight.__main__ import _subscribe_observations
from pawsight.bus import Bus, TOPIC_CMD, TOPIC_OBS, TOPIC_SNAPSHOT, encode
from pawsight.observations import ObservationWorker
from pawsight.snapshot import BusSnapshot, reply


class Observations(unittest.TestCase):
    def test_slow_brain_does_not_block_snapshot_or_live_dashboard(self):
        entered, release, done = (threading.Event() for _ in range(3))
        seen = []

        def observe(obs, jpeg):
            entered.set()
            release.wait(2)
            seen.append((obs["ts"], jpeg))
            if len(seen) == 2:
                done.set()

        bus = Bus("unused")
        dash = Mock()
        worker = _subscribe_observations(bus, SimpleNamespace(observe=observe), dash)
        self.addCleanup(worker.stop)
        self.addCleanup(release.set)

        def dispatch(topic, payload):
            bus._on_message(None, None, SimpleNamespace(topic=topic, payload=payload))

        def publish(topic, payload, retain=False):
            self.assertEqual(topic, TOPIC_CMD)
            req = json.loads(payload)["req"]
            dispatch(TOPIC_SNAPSHOT, reply(req, b"fresh-jpeg", None))

        bus.publish = publish
        snap = BusSnapshot(bus, timeout=.2)
        bus.subscribe(TOPIC_SNAPSHOT, snap.on_reply)
        dispatch(TOPIC_OBS, encode({"ts": 1}, b"frame-1"))
        self.assertTrue(entered.wait(1))
        dispatch(TOPIC_OBS, encode({"ts": 2}, b"frame-2"))
        self.assertEqual(seen, [], "the brain is still waiting for the VLM")
        dash.on_observation.assert_called_with({"ts": 2})
        self.assertEqual(snap.grab(), (b"fresh-jpeg", None))
        release.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(seen, [(1, b"frame-1"), (2, b"frame-2")])

    def test_failed_observation_does_not_kill_worker(self):
        done = threading.Event()
        def observe(obs, jpeg):
            if obs["ts"] == 1:
                raise ValueError("bad observation")
            done.set()
        worker = ObservationWorker(observe)
        self.addCleanup(worker.stop)
        with self.assertLogs("pawsight.observations", level="ERROR"):
            worker.submit({"ts": 1})
            worker.submit({"ts": 2})
            self.assertTrue(done.wait(1))

    def test_full_queue_reports_loss_without_blocking_publisher(self):
        entered, release, second = (threading.Event() for _ in range(3))
        seen = []
        def observe(obs, jpeg):
            entered.set()
            release.wait(2)
            seen.append(obs["ts"])
            if len(seen) == 2:
                second.set()
        worker = ObservationWorker(observe, max_pending=1)
        self.addCleanup(worker.stop)
        self.addCleanup(release.set)
        self.assertTrue(worker.submit({"ts": 1}))
        self.assertTrue(entered.wait(1))
        self.assertTrue(worker.submit({"ts": 2}))
        with self.assertLogs("pawsight.observations", level="WARNING") as logs:
            self.assertFalse(worker.submit({"ts": 3}))
            self.assertFalse(worker.submit({"ts": 4}))
            self.assertEqual(len(logs.output), 1, "overload must not flood the log")
            release.set()
            self.assertTrue(second.wait(1))
            self.assertTrue(worker.submit({"ts": 5}))
        self.assertIn("frames lost: 2", logs.output[-1])
        self.assertEqual(seen[:2], [1, 2])

    def test_stopped_worker_rejects_new_observations(self):
        observe = Mock()
        worker = ObservationWorker(observe)
        worker.stop()
        self.assertFalse(worker.submit({"ts": 1}))
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
