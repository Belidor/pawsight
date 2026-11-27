"""appinit: the configuration half of install.sh for the desktop app."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from dotenv import dotenv_values

from pawsight import appinit, authsetup, setup

REPO = Path(__file__).resolve().parent.parent


class Configure(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copy(REPO / ".env.example", self.root / ".env.example")
        shutil.copy(REPO / "zones.example.yaml", self.root / "zones.example.yaml")
        (self.root / "go2rtc").mkdir()
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml",
                    self.root / "go2rtc" / "go2rtc.example.yaml")
        # authsetup/setup write next to their own ROOT by default; point them here
        self.patches = [
            patch.object(setup, "ENV_FILE", self.root / ".env"),
            patch.object(setup, "ENV_EXAMPLE", self.root / ".env.example"),
            patch.object(setup, "GO2RTC_FILE", self.root / "go2rtc" / "go2rtc.yaml"),
            patch.object(authsetup, "MOSQ_DIR", self.root / "mosquitto"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_new_install_gets_files_and_passwords(self):
        made = appinit.configure(self.root)
        self.assertTrue(made["new_install"])
        env = dotenv_values(self.root / ".env")
        self.assertEqual(env["PAWSIGHT_STACK"], "native")
        self.assertTrue(env["PAWSIGHT_MQTT_USER"])
        self.assertTrue(env["PAWSIGHT_MQTT_PASS"])
        g = yaml.safe_load((self.root / "go2rtc" / "go2rtc.yaml").read_text())
        self.assertTrue(g["api"]["username"])
        self.assertEqual(g["api"]["password"], env["GO2RTC_PASS"])
        self.assertTrue((self.root / "zones.yaml").exists())
        self.assertTrue((self.root / "storage").is_dir())

    def test_second_run_changes_nothing(self):
        appinit.configure(self.root)
        before = {p: p.read_bytes() for p in (self.root / ".env",
                                              self.root / "go2rtc" / "go2rtc.yaml",
                                              self.root / "zones.yaml")}
        made = appinit.configure(self.root)
        self.assertFalse(made["new_install"])
        for p, data in before.items():
            self.assertEqual(p.read_bytes(), data, p.name)

    def test_no_camera_is_invented(self):
        appinit.configure(self.root)
        self.assertFalse(setup.camera_configured(self.root / "go2rtc" / "go2rtc.yaml"))


class Ollama(unittest.TestCase):
    def test_nothing_listening_is_missing(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self.assertEqual(appinit.ollama_state(f"http://127.0.0.1:{port}", "m"), "missing")


if __name__ == "__main__":
    unittest.main()
