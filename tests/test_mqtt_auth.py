"""Password-protected broker: the bus can log in, a new install gets passwd and ACL.

An anonymous broker let any process on this machine send the detector
commands (pan the camera, delete frames, train) and read camera frames. Old
installs without a password must keep working as before.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from pawsight import authsetup
from pawsight.bus import Bus

REPO = Path(__file__).resolve().parent.parent


class BusWithLogin(unittest.TestCase):
    def _connect(self, bus):
        fake = MagicMock()
        with patch("paho.mqtt.client.Client", return_value=fake):
            bus.connect()
        return fake

    def test_login_is_sent_to_broker(self):
        c = self._connect(Bus("127.0.0.1", username="pawsight", password="pw"))
        c.username_pw_set.assert_called_once_with("pawsight", "pw")

    def test_no_login_means_anonymous(self):
        with patch.dict(os.environ, {"PAWSIGHT_MQTT_USER": "", "PAWSIGHT_MQTT_PASS": ""}):
            c = self._connect(Bus("127.0.0.1"))
        c.username_pw_set.assert_not_called()

    def test_login_from_environment(self):
        with patch.dict(os.environ, {"PAWSIGHT_MQTT_USER": "u", "PAWSIGHT_MQTT_PASS": "p"}):
            c = self._connect(Bus("127.0.0.1"))
        c.username_pw_set.assert_called_once_with("u", "p")

    def test_main_passes_login_from_config(self):
        src = (REPO / "pawsight" / "__main__.py").read_text()
        self.assertEqual(src.count("username=cfg.mqtt_user, password=cfg.mqtt_pass"),
                         src.count("Bus(cfg.mqtt_host"))


class MosquittoHash(unittest.TestCase):
    def test_format_7(self):
        salt = b"0123456789ab"
        h = authsetup.mosquitto_hash("pw", salt=salt)
        tag, it, s64, d64 = h.split("$")[1:]
        self.assertEqual((tag, it), ("7", "101"))
        self.assertEqual(base64.b64decode(s64), salt)
        self.assertEqual(base64.b64decode(d64),
                         hashlib.pbkdf2_hmac("sha512", b"pw", salt, 101, 64))

    def test_salt_is_new_each_time(self):
        self.assertNotEqual(authsetup.mosquitto_hash("pw"), authsetup.mosquitto_hash("pw"))


class PasswordEnabling(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.mdir = self.d / "mosquitto"
        self.mdir.mkdir()
        shutil.copy(REPO / "mosquitto" / "acl", self.mdir / "acl")
        self.env = self.d / ".env"
        self.env.write_text("PAWSIGHT_STACK=native\n")

    def test_writes_passwd_conf_and_env(self):
        self.assertTrue(authsetup.enable_mqtt_auth(self.mdir, self.env, password="pw"))
        line = (self.mdir / "passwd").read_text().strip()
        self.assertTrue(line.startswith("pawsight:$7$101$"))
        self.assertNotIn("pw$", line)
        self.assertEqual((self.mdir / "passwd").stat().st_mode & 0o777, 0o600)
        conf = (self.mdir / "native.conf").read_text()
        self.assertIn("listener 1883 127.0.0.1", conf)
        self.assertIn("allow_anonymous false", conf)
        self.assertIn(f"acl_file {self.mdir / 'acl'}", conf)
        env = self.env.read_text()
        self.assertIn("PAWSIGHT_MQTT_USER=pawsight", env)
        self.assertIn("PAWSIGHT_MQTT_PASS=pw", env)
        self.assertIn("PAWSIGHT_STACK=native", env)

    def test_rerun_changes_nothing(self):
        authsetup.enable_mqtt_auth(self.mdir, self.env, password="first")
        before = (self.mdir / "passwd").read_text()
        self.assertFalse(authsetup.enable_mqtt_auth(self.mdir, self.env, password="second"))
        self.assertEqual((self.mdir / "passwd").read_text(), before)

    def test_login_in_acl_matches(self):
        acl = (REPO / "mosquitto" / "acl").read_text()
        self.assertIn(f"user {authsetup.MQTT_USER}\n", acl)
        self.assertIn("topic readwrite pawsight/#", acl)


class BrokerConfigs(unittest.TestCase):
    def test_compose_chooses_by_passwd(self):
        svc = yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]["mosquitto"]
        cmd = " ".join(svc["command"])
        self.assertIn("-f /mosquitto/auth/passwd", cmd)
        self.assertIn("/mosquitto/auth/auth.conf", cmd)
        self.assertIn("/mosquitto/config/mosquitto.conf", cmd, "old installs keep the previous one")
        self.assertIn("./mosquitto:/mosquitto/auth:ro", svc["volumes"])
        self.assertEqual(svc["ports"], ["127.0.0.1:1883:1883"])

    def test_auth_conf(self):
        conf = (REPO / "mosquitto" / "auth.conf").read_text()
        self.assertIn("allow_anonymous false", conf)
        self.assertIn("password_file /mosquitto/auth/passwd", conf)
        self.assertIn("acl_file /mosquitto/auth/acl", conf)

    def test_secrets_in_gitignore(self):
        gi = (REPO / ".gitignore").read_text().splitlines()
        for f in ("mosquitto/passwd", "mosquitto/native.conf"):
            self.assertIn(f, gi)

    def test_scripts(self):
        inst = (REPO / "install.sh").read_text()
        self.assertIn("-m pawsight.authsetup --mqtt", inst)
        self.assertIn('[ -f .env ] || NEW_INSTALL=1', inst)
        self.assertIn("mosquitto/native.conf -d", (REPO / "start.sh").read_text())
        self.assertIn("mosquitto/mosquitto.pid", (REPO / "stop.sh").read_text())


if __name__ == "__main__":
    unittest.main()
