"""The self-test exit code reflects every required dependency."""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pawsight.__main__ import selftest


class Selftest(unittest.TestCase):
    def check(self, *, ollama=True, zones=True, mps=True, rtsp=True):
        cfg = SimpleNamespace(
            device="mps", rtsp="test-source", ollama_url="unused", ollama_model="test",
            zones=[SimpleNamespace(name="bowls")] if zones else [],
            home_hold=False, search_sec=0, telegram_ready=False, db_path="unused")
        cap = Mock()
        cap.isOpened.return_value = rtsp
        cap.read.return_value = (rtsp, None)
        torch = SimpleNamespace(backends=SimpleNamespace(
            mps=SimpleNamespace(is_available=lambda: mps)),
            cuda=SimpleNamespace(is_available=lambda: False))
        cv2 = SimpleNamespace(VideoCapture=lambda _: cap)
        output = io.StringIO()
        with patch.dict("sys.modules", {"torch": torch, "cv2": cv2}), \
                patch("pawsight.__main__.VLM.available", return_value=ollama), \
                contextlib.redirect_stdout(output):
            result = selftest(cfg)
        cap.release.assert_called_once()
        return result, output.getvalue()

    def test_missing_ollama_is_a_blocker(self):
        code, text = self.check(ollama=False)
        self.assertEqual(code, 1)
        self.assertIn("blockers found", text)

    def test_no_zones_is_a_blocker(self):
        code, text = self.check(zones=False)
        self.assertEqual(code, 1)
        self.assertIn("blockers found", text)

    def test_telegram_remains_optional(self):
        code, text = self.check()
        self.assertEqual(code, 0)
        self.assertIn("NOT configured", text)

    def test_gpu_and_stream_failures_remain_blockers(self):
        for opts in ({"mps": False}, {"rtsp": False}):
            with self.subTest(opts=opts):
                self.assertEqual(self.check(**opts)[0], 1)


if __name__ == "__main__":
    unittest.main()
