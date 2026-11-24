"""Old DOGCAM_* settings keep working after the rename to pawsight."""
import tempfile
import unittest
from pathlib import Path

from pawsight._legacy_env import adopt_legacy_env


class LegacyEnv(unittest.TestCase):
    def test_old_key_is_adopted(self):
        env = {"DOGCAM_MQTT_PASS": "secret"}
        self.assertEqual(adopt_legacy_env(env), ["PAWSIGHT_MQTT_PASS"])
        self.assertEqual(env["PAWSIGHT_MQTT_PASS"], "secret")

    def test_new_key_wins(self):
        env = {"DOGCAM_STACK": "docker", "PAWSIGHT_STACK": "native"}
        self.assertEqual(adopt_legacy_env(env), [])
        self.assertEqual(env["PAWSIGHT_STACK"], "native")

    def test_unrelated_keys_are_untouched(self):
        env = {"TELEGRAM_BOT_TOKEN": "t", "CAMERA_IP": "x"}
        adopt_legacy_env(env)
        self.assertEqual(env, {"TELEGRAM_BOT_TOKEN": "t", "CAMERA_IP": "x"})

    def test_env_file_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / ".env"
            f.write_text("DOGCAM_WEB_PORT=8100\n")
            env = {}
            adopt_legacy_env(env, env_file=f)
            self.assertEqual(env["PAWSIGHT_WEB_PORT"], "8100")


if __name__ == "__main__":
    unittest.main()
