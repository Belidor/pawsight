"""WebKit (Safari, the desktop app) plays the SD substream of a Xiaomi camera.

The main P2P stream changes size on the fly; WebKit's HEVC decoder then draws
a small picture in a green frame.
"""
import unittest

from pawsight import setup
from pawsight.web_go2rtc import is_webkit, view_stream

SAFARI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/26.0 Safari/605.1.15")
APP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)"
CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
XIAOMI = "xiaomi://1:cn@192.168.1.2?did=1&model=x"


class ViewStream(unittest.TestCase):
    def test_webkit_detection(self):
        self.assertTrue(is_webkit(SAFARI))
        self.assertTrue(is_webkit(APP))
        self.assertFalse(is_webkit(CHROME))
        self.assertFalse(is_webkit(""))

    def test_webkit_with_xiaomi_gets_sd(self):
        for ua in (SAFARI, APP):
            self.assertEqual(view_stream(setup.STREAM_MAIN, ua, XIAOMI), setup.STREAM_SUB)

    def test_chromium_keeps_main(self):
        self.assertEqual(view_stream(setup.STREAM_MAIN, CHROME, XIAOMI), setup.STREAM_MAIN)

    def test_plain_rtsp_keeps_main(self):
        # An ordinary RTSP camera may have no SD substream at all.
        self.assertEqual(view_stream(setup.STREAM_MAIN, APP, "rtsp://cam/1"), setup.STREAM_MAIN)

    def test_explicit_sd_unchanged(self):
        self.assertEqual(view_stream(setup.STREAM_SUB, CHROME, XIAOMI), setup.STREAM_SUB)
