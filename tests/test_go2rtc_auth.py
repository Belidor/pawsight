"""The go2rtc API is password-protected, and the dashboard and start.sh send that password.

127.0.0.1:1984 is reachable from any page in the owner's browser; via DNS
rebinding a foreign site could read /api/config (camera password, Mi token)
and change streams. Older installs without a password must keep working as before.
"""
from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from pawsight import setup
from pawsight.config import Config
from pawsight.web import Dashboard

REPO = Path(__file__).resolve().parent.parent


def _yaml(d: Path, api: dict) -> Path:
    f = d / "go2rtc.yaml"
    f.write_text(yaml.safe_dump({"api": {"listen": "127.0.0.1:1984", **api},
                                 "streams": {"dog_cam": ""}}))
    return f


class Credentials(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_from_file(self):
        f = _yaml(self.d, {"username": "pawsight", "password": "p1"})
        self.assertEqual(setup.go2rtc_credentials(f), ("pawsight", "p1"))

    def test_file_without_login_means_no_password_even_with_env(self):
        f = _yaml(self.d, {})
        with patch.dict(os.environ, {"GO2RTC_USER": "u", "GO2RTC_PASS": "p"}):
            self.assertIsNone(setup.go2rtc_credentials(f))

    def test_no_file_uses_env(self):
        with patch.dict(os.environ, {"GO2RTC_USER": "u", "GO2RTC_PASS": "p"}):
            self.assertEqual(setup.go2rtc_credentials(self.d / "missing.yaml"), ("u", "p"))

    def test_nothing_at_all(self):
        with patch.dict(os.environ, {"GO2RTC_USER": "", "GO2RTC_PASS": ""}):
            self.assertIsNone(setup.go2rtc_credentials(self.d / "missing.yaml"))

    def test_template(self):
        api = yaml.safe_load((REPO / "go2rtc" / "go2rtc.example.yaml").read_text())["api"]
        self.assertEqual(api["username"], "", "the template holds no secrets")
        self.assertIs(api["local_auth"], True,
                      "without local_auth go2rtc lets 127.0.0.1 in without a password")
        self.assertEqual(api["listen"], "127.0.0.1:1984")


class Scripts(unittest.TestCase):
    def test_start_sh_sends_authorization(self):
        src = (REPO / "start.sh").read_text()
        self.assertIn('req.add_header("Authorization","Basic "+tok)', src)

    def test_install_sh_sets_password_only_on_new_install(self):
        src = (REPO / "install.sh").read_text()
        block = src[src.index('if [ "$NEW_G2R" = "1" ]; then'):]
        self.assertIn("-m pawsight.authsetup --go2rtc", block.split("\nfi\n", 1)[0])


class EnablePassword(unittest.TestCase):
    """pawsight.authsetup: for a new install from install.sh, for an older one by the owner."""

    def setUp(self):
        from pawsight import authsetup
        self.a = authsetup
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.f = self.d / "go2rtc.yaml"
        self.env = self.d / ".env"
        self.env.write_text("# my .env\nTELEGRAM_CHAT_ID=1\n")

    def _api(self):
        return yaml.safe_load(self.f.read_text())["api"]

    def test_template(self):
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", self.f)
        before = self.f.read_text()
        self.assertTrue(self.a.enable_go2rtc_auth(self.f, self.env, password="s3cr-et_"))
        api = self._api()
        self.assertEqual((api["username"], api["password"], api["local_auth"], api["listen"]),
                         ("pawsight", "s3cr-et_", True, "127.0.0.1:1984"))
        self.assertIn("DNS rebinding", self.f.read_text(), "template comments intact")
        self.assertEqual(self.f.stat().st_mode & 0o777, 0o600)
        env = self.env.read_text()
        self.assertIn("GO2RTC_USER=pawsight", env)
        self.assertIn("GO2RTC_PASS=s3cr-et_", env)
        self.assertIn("# my .env", env)
        self.assertEqual(len(before.splitlines()), len(self.f.read_text().splitlines()),
                         "the template already has the keys — no lines added")

    def test_old_install_like_the_owners(self):
        """Format of the live file: api with listen only, a xiaomi section, streams."""
        self.f.write_text(
            "# my notes\napi:\n  listen: \"127.0.0.1:1984\"\n\n"
            "rtsp:\n  listen: \"127.0.0.1:8554\"\n\n"
            "xiaomi:\n  \"123\": V1:abc\n"
            "streams:\n  dog_cam: xiaomi://u:p@1.2.3.4?did=1\n  dog_cam_sd: \"\"\n")
        before = yaml.safe_load(self.f.read_text())
        self.assertTrue(self.a.enable_go2rtc_auth(self.f, self.env, password="pw"))
        after = yaml.safe_load(self.f.read_text())
        self.assertEqual({k: v for k, v in after.items() if k != "api"},
                         {k: v for k, v in before.items() if k != "api"})
        self.assertEqual(after["api"], {"listen": "127.0.0.1:1984", "username": "pawsight",
                                        "password": "pw", "local_auth": True})
        self.assertTrue(self.f.read_text().startswith("# my notes"))

    def test_repeat_changes_nothing(self):
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", self.f)
        self.a.enable_go2rtc_auth(self.f, self.env, password="first")
        self.assertFalse(self.a.enable_go2rtc_auth(self.f, self.env, password="second"))
        self.assertEqual(self._api()["password"], "first")

    def test_without_api_section_listens_on_loopback(self):
        self.f.write_text("streams:\n  dog_cam: \"\"\n")
        self.a.enable_go2rtc_auth(self.f, self.env, password="pw")
        self.assertEqual(self._api()["listen"], "127.0.0.1:1984")

    def test_password_is_not_printed(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", self.f)
        out = io.StringIO()
        with patch.object(setup, "GO2RTC_FILE", self.f), \
                patch.object(setup, "ENV_FILE", self.env), \
                patch.object(self.a, "_new_password", return_value="VERY-SECRET"), \
                redirect_stdout(out), redirect_stderr(out):
            self.assertEqual(self.a.main(["--go2rtc"]), 0)
        self.assertNotIn("VERY-SECRET", out.getvalue())
        self.assertEqual(self._api()["password"], "VERY-SECRET")


class FakeGo2rtc:
    """go2rtc with basic auth: 401 without the right login."""

    def __init__(self, creds):
        self.creds = creds
        self.seen = []

    def _ok(self, req) -> bool:
        h = req.headers.get("Authorization", "")
        self.seen.append(h)
        if self.creds is None:
            return True
        want = "Basic " + base64.b64encode(":".join(self.creds).encode()).decode()
        return h == want

    def app(self):
        async def streams(req):
            if not self._ok(req):
                return web.Response(status=401)
            return web.json_response({"dog_cam": {}})

        async def js(req):
            if not self._ok(req):
                return web.Response(status=401, text="401 Unauthorized")
            return web.Response(text="export{}", content_type="text/javascript")

        async def ws(req):
            if not self._ok(req):
                return web.Response(status=401)
            w = web.WebSocketResponse()
            await w.prepare(req)
            await w.send_str("hello")
            await w.close()
            return w

        app = web.Application()
        app.add_routes([web.get("/api/streams", streams), web.get("/video-rtc.js", js),
                        web.get("/api/ws", ws)])
        return app


class DashboardSendsPassword(unittest.IsolatedAsyncioTestCase):
    async def _up(self, file_api: dict | None, server_creds):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        old = setup.GO2RTC_FILE
        setup.GO2RTC_FILE = _yaml(self.d, file_api) if file_api is not None \
            else self.d / "missing.yaml"
        self.addCleanup(setattr, setup, "GO2RTC_FILE", old)
        self.g = FakeGo2rtc(server_creds)
        srv = TestServer(self.g.app())
        await srv.start_server()
        self.addAsyncCleanup(srv.close)
        self.dash = Dashboard(Config(db_path=self.d / "events.db", snap_dir=self.d / "s"))
        self.dash.go2rtc = str(srv.make_url("")).rstrip("/")
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_with_password_from_file(self):
        await self._up({"username": "pawsight", "password": "pw"}, ("pawsight", "pw"))
        code, _ = await self.dash._go2rtc_call("GET", "/api/streams")
        self.assertEqual(code, 200)
        r = await self.client.get("/player/video-rtc.js")
        self.assertEqual(r.status, 200)
        async with self.client.ws_connect("/api/stream?src=dog_cam") as w:
            m = await w.receive(timeout=2)
        self.assertEqual(m.data, "hello")

    async def test_password_from_env_in_docker(self):
        with patch.dict(os.environ, {"GO2RTC_USER": "u", "GO2RTC_PASS": "p"}):
            await self._up(None, ("u", "p"))
            code, _ = await self.dash._go2rtc_call("GET", "/api/streams")
        self.assertEqual(code, 200)

    async def test_old_install_without_password(self):
        with patch.dict(os.environ, {"GO2RTC_USER": "", "GO2RTC_PASS": ""}):
            await self._up({}, None)
            code, _ = await self.dash._go2rtc_call("GET", "/api/streams")
            r = await self.client.get("/player/video-rtc.js")
        self.assertEqual(code, 200)
        self.assertEqual(r.status, 200)
        self.assertTrue(all(h == "" for h in self.g.seen), "no header without a password")

    async def test_wrong_password_is_not_served_as_script(self):
        await self._up({"username": "pawsight", "password": "old"}, ("pawsight", "new"))
        with self.assertLogs("pawsight.web", "WARNING"):
            r = await self.client.get("/player/video-rtc.js")
        self.assertEqual(r.status, 502)


if __name__ == "__main__":
    unittest.main()
