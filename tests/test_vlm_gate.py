"""False dog: the VLM does not see it, yet the notification still went out.

Measured 2026-09-06: the three-class model took an office chair for a dog
(conf 0.35 and 0.51), while the VLM answered exactly "NO DOG" on the same
frames. The answer was thrown away: describe() turned it into None, and None
blocked nothing, so the event was written and the message went to the owner.

The main point here is not to suppress too much: "no dog" and "could not ask"
must be told apart. If notifications are suppressed on any VLM silence, a
crashed Ollama silently cancels every alert, and that gets noticed weeks later.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pawsight.vlm import VLM


class Canned(VLM):
    """A VLM with a canned model answer."""

    def __init__(self, raw: str | None) -> None:
        super().__init__("http://127.0.0.1:0", "no-model")
        self._raw = raw

    def _generate(self, prompt, frames_jpeg):
        return self._raw


FRAME = [b"jpeg"]


class ThreeWayAnswer(unittest.TestCase):
    def test_no_dog_is_recognised_explicitly(self):
        self.assertEqual(Canned("NO DOG").look(FRAME)[0], "no_dog")

    def test_description_means_dog(self):
        verdict, line = Canned("lying").look(FRAME)
        self.assertEqual(verdict, "dog")
        self.assertEqual(line, "lying")

    def test_vlm_silence_is_not_a_refusal(self):
        self.assertEqual(Canned(None).look(FRAME)[0], "unknown",
                         "a crashed Ollama must not look like \"no dog\"")

    def test_without_frames_there_is_nobody_to_ask(self):
        self.assertEqual(Canned("lying").look([])[0], "unknown")

    def test_describe_is_compatible_with_old_behaviour(self):
        self.assertIsNone(Canned("NO DOG").describe(FRAME))
        self.assertIsNone(Canned(None).describe(FRAME))
        self.assertEqual(Canned("lying").describe(FRAME), "lying")


class FakeStore:
    def __init__(self) -> None:
        self.added: list[tuple] = []

    def add(self, *a, **kw) -> None:
        self.added.append(a)


class FakeTg:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text, photo=None, key=None) -> None:
        self.sent.append(text)


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _brain(self, raw: str | None):
        from pawsight.brain import Brain
        from pawsight.config import Config

        # The database is TEMPORARY. This used to open the live
        # storage/events.db, i.e. the test did exactly what caused the
        # 2026-09-06 incident: a second connection to a database owned by the
        # brain in the container (tests/test_db_owner.py). The real Store is
        # replaced with FakeStore right away anyway: the live database was
        # never needed.
        cfg = Config()
        cfg.db_path = Path(self.tmp.name) / "events.db"
        b = Brain(cfg, async_reports=False)
        b.vlm = Canned(raw)
        b.store = FakeStore()
        b.tg = FakeTg()
        b.frames[7] = [b"jpeg"]
        return b

    def test_false_dog_does_not_reach_telegram(self):
        b = self._brain("NO DOG")
        b._emit(7, None, "lying", 1788711543.0)
        self.assertEqual(b.tg.sent, [], "the VLM sees no dog: nothing to report")
        self.assertEqual(len(b.store.added), 1, "but the event stays in the database")

    def test_real_dog_is_sent(self):
        b = self._brain("lying")
        b._emit(7, None, "lying", 1788711543.0)
        self.assertEqual(len(b.tg.sent), 1)
        self.assertIn("lying", b.tg.sent[0])

    def test_crashed_vlm_suppresses_nothing(self):
        b = self._brain(None)
        b._emit(7, None, "lying", 1788711543.0)
        self.assertEqual(len(b.tg.sent), 1,
                         "VLM silence is no reason to cancel a notification")


if __name__ == "__main__":
    unittest.main()
