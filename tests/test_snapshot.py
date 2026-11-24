"""On-demand frame over the bus: request -> detector -> reply.

There is no broker here: the bus is replaced by an object that hands each
publish straight back. This checks the contract between the two halves
(`pawsight detect` and `pawsight brain`), because they live in different
processes and an edit in one can break it.
"""
from __future__ import annotations

import json
import unittest

from pawsight.bus import TOPIC_CMD, TOPIC_SNAPSHOT, decode
from pawsight.snapshot import CMD, BusSnapshot, serve

JPEG = b"\xff\xd8\x00\x01\x02kadr\xff\xd9"


class FakeDetector:
    def __init__(self, jpeg=JPEG, note=None) -> None:
        self.result = (jpeg, note)
        self.calls = 0

    def snapshot_jpeg(self):
        self.calls += 1
        return self.result


class FakeBus:
    """A bus that calls the detector right away: simulates an instant reply."""

    def __init__(self, det: FakeDetector | None = None) -> None:
        self.det = det
        self.snap: BusSnapshot | None = None
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes) -> None:
        self.published.append((topic, payload))
        if topic == TOPIC_CMD and self.det is not None:
            serve(self, self.det, payload)
        elif topic == TOPIC_SNAPSHOT and self.snap is not None:
            self.snap.on_reply(payload)


def wired(det: FakeDetector) -> tuple[FakeBus, BusSnapshot]:
    bus = FakeBus(det)
    bus.snap = BusSnapshot(bus, timeout=0.5)
    return bus, bus.snap


class RoundTrip(unittest.TestCase):
    def test_frame_comes_back_byte_for_byte(self):
        """base64 both ways: a corrupted JPEG in Telegram looks like "the bot
        is broken", while the cause would be in the transport."""
        det = FakeDetector()
        _, snap = wired(det)
        self.assertEqual(snap.grab(), (JPEG, None))
        self.assertEqual(det.calls, 1)

    def test_request_goes_to_the_commands_topic(self):
        """The detector listens on exactly this topic; nobody answers on another one."""
        bus, snap = wired(FakeDetector())
        snap.grab()
        topic, payload = bus.published[0]
        self.assertEqual(topic, TOPIC_CMD)
        self.assertEqual(json.loads(payload)["cmd"], CMD)

    def test_reply_carries_the_request_id(self):
        bus, snap = wired(FakeDetector())
        snap.grab()
        asked = json.loads(bus.published[0][1])["req"]
        answered, _ = decode(bus.published[1][1])
        self.assertEqual(answered["req"], asked)

    def test_note_survives_the_trip(self):
        _, snap = wired(FakeDetector(JPEG, "frame is 9 s old"))
        self.assertEqual(snap.grab(), (JPEG, "frame is 9 s old"))

    def test_reason_survives_when_there_is_no_frame(self):
        _, snap = wired(FakeDetector(None, "the stream is open but has no frames yet"))
        jpeg, note = snap.grab()
        self.assertIsNone(jpeg)
        self.assertIn("frames", note)


class NobodyAnswers(unittest.TestCase):
    def test_timeout_says_what_to_check(self):
        """The brain runs without a detector (it may have failed to start) and
        must tell the person so instead of staying silent forever."""
        snap = BusSnapshot(FakeBus(), timeout=0.05)
        jpeg, note = snap.grab()
        self.assertIsNone(jpeg)
        self.assertIn("detector", note)

    def test_answer_to_a_foreign_request_is_ignored(self):
        """A reply to someone else's (or an expired) request must not wake ours."""
        bus = FakeBus()
        snap = BusSnapshot(bus, timeout=0.05)
        snap.on_reply(json.dumps({"req": "foreign"}).encode())   # not our req
        self.assertIsNone(snap.grab()[0])

    def test_garbage_reply_does_not_crash_the_mqtt_thread(self):
        """on_reply is called from paho's network thread: an exception there
        brings down message handling entirely."""
        snap = BusSnapshot(FakeBus(), timeout=0.05)
        with self.assertLogs("pawsight.snapshot", level="WARNING"):
            snap.on_reply(b"not json at all")

    def test_waiters_do_not_pile_up(self):
        """Every unanswered request would otherwise leave an Event and bytes behind."""
        snap = BusSnapshot(FakeBus(), timeout=0.01)
        for _ in range(3):
            snap.grab()
        self.assertEqual(snap._waiting, {})


class DetectorSide(unittest.TestCase):
    def test_garbage_request_answers_nothing(self):
        bus = FakeBus()
        with self.assertLogs("pawsight.snapshot", level="WARNING"):
            serve(bus, FakeDetector(), b"not json at all")
        self.assertEqual(bus.published, [])

    def test_missing_frame_is_reported_not_hidden(self):
        bus = FakeBus()
        with self.assertLogs("pawsight.snapshot", level="INFO"):
            serve(bus, FakeDetector(None, "the detector has not opened the stream yet"), b'{"req": "x"}')
        payload, jpeg = decode(bus.published[0][1])
        self.assertIsNone(jpeg)
        self.assertIn("not opened the stream", payload["note"])


if __name__ == "__main__":
    unittest.main()
