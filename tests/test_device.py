"""Device choice: Metal on a Mac, CUDA on Linux with an NVIDIA GPU, else CPU."""
from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from pawsight import config
from pawsight.config import Config


def fake_torch(mps: bool, cuda: bool):
    t = types.ModuleType("torch")
    t.backends = types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps))
    t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    return t


class DevicePick(unittest.TestCase):
    def test_order(self):
        for mps, cuda, want in ((True, False, "mps"), (False, True, "cuda:0"),
                                (False, False, "cpu"), (True, True, "mps")):
            with self.subTest(mps=mps, cuda=cuda), \
                    mock.patch.dict(sys.modules, {"torch": fake_torch(mps, cuda)}):
                self.assertEqual(config.detect_device(), want)

    def test_no_torch_is_cpu(self):
        """The brain image has no torch and must still build its Config."""
        with mock.patch.dict(sys.modules, {"torch": None}):
            self.assertEqual(config.detect_device(), "cpu")

    def test_auto_resolved_once_explicit_kept(self):
        with mock.patch.dict(sys.modules, {"torch": fake_torch(False, True)}):
            c = Config(device="auto")
            self.assertEqual(c.device, "auto", "not resolved until a model process asks")
            self.assertEqual(c.resolve_device(), "cuda:0")
            self.assertEqual(Config(device="cpu").resolve_device(), "cpu")


if __name__ == "__main__":
    unittest.main()
