"""First-run setup routes: guards and writing the full camera address.

A real aiohttp server is started here, but all on loopback and all our own:
go2rtc is a ten-line mock, the camera is the mock from test_setup. What is
checked is what a pure function cannot check: that a POST from a foreign tab
is rejected, that the password stays in go2rtc.yaml and never shows up in
.env, and that before setup "/" shows the wizard, not an empty dashboard.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

from pawsight import setup
from pawsight.config import Config
from pawsight.web import Dashboard
from tests.test_setup import ZONES_FIXTURE, FakeCamera

REPO = Path(__file__).resolve().parent.parent
# The same header the guard middleware requires on every state-changing
# request: a foreign page cannot send it without a preflight.
JSON = {"content-type": "application/json", "X-Pawsight": "1"}

# Every setup route: the guard must be on each of them, not just on the one
# somebody remembered to check.
SETUP_ROUTES = [
    ("GET", "/setup", None),
    ("GET", "/api/setup/state", None),
    ("POST", "/api/setup/probe", {"url": "rtsp://127.0.0.1:9/s"}),
    ("POST", "/api/setup/camera", {"url": "rtsp://127.0.0.1:9/s"}),
    ("POST", "/api/setup/telegram", {"token": "", "chats": ""}),
    ("POST", "/api/setup/bowls", {"side": "right"}),
    ("POST", "/api/setup/device_token", {}),
]


class FakeGo2rtc:
    """Answers like go2rtc v1.9.14: PUT /api/streams takes src from the query.

    It also does what sets the real go2rtc apart from a stub: write the
    source into its own config (then our writer must stay quiet and not wipe
    someone else's comments) and refuse while quoting the source in full.
    """

    def __init__(self, writes_config: bool = False, fail_with: str = "") -> None:
        self.puts: list[tuple[str, str]] = []
        self.streams = {"dog_cam": {}}
        self.writes_config = writes_config
        self.fail_with = fail_with

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/api/streams", self.list_),
            web.put("/api/streams", self.put),
        ])
        return app

    async def list_(self, _req):
        return web.json_response(self.streams)

    async def put(self, req):
        name, src = req.query.get("name", ""), req.query.get("src", "")
        self.puts.append((name, src))
        if self.fail_with:
            return web.Response(status=500, text=self.fail_with.replace("SRC", src))
        if self.writes_config:
            # go2rtc edits the yaml by inserting into the text, comments stay intact.
            text = setup.GO2RTC_FILE.read_text()
            setup.GO2RTC_FILE.write_text(text.replace(f'  {name}: ""', f"  {name}: {src}"))
        return web.Response(text="")


class SetupRoutes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._paths = (setup.ENV_FILE, setup.GO2RTC_FILE, setup.ZONES_FILE)
        setup.ENV_FILE = self.dir / ".env"
        setup.GO2RTC_FILE = self.dir / "go2rtc.yaml"
        setup.ZONES_FILE = self.dir / "zones.yaml"
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", setup.GO2RTC_FILE)
        shutil.copy(ZONES_FIXTURE, setup.ZONES_FILE)

        self.go2rtc = FakeGo2rtc()
        self.g2r_server = TestServer(self.go2rtc.app())
        await self.g2r_server.start_server()

        self.dash = Dashboard(Config())
        self.dash.go2rtc = str(self.g2r_server.make_url("")).rstrip("/")
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def save_camera(self, cam, **extra):
        body = {"host": "127.0.0.1", "port": cam.port, "path": cam.path,
                "user": cam.user, "password": cam.password}
        body.update(extra)
        r = await self.client.post("/api/setup/camera", headers=JSON, json=body)
        return r, await r.json()

    async def asyncTearDown(self):
        await self.client.close()
        await self.g2r_server.close()
        setup.ENV_FILE, setup.GO2RTC_FILE, setup.ZONES_FILE = self._paths
        shutil.rmtree(self.dir, ignore_errors=True)

    # ------------------------------------------------------------------ guards

    async def test_state_is_readable(self):
        r = await self.client.get("/api/setup/state")
        self.assertEqual(r.status, 200)
        s = await r.json()
        self.assertFalse(s["camera"]["configured"])
        self.assertTrue(s["go2rtc"]["ok"])

    async def test_foreign_origin_is_refused_on_every_writing_route(self):
        """127.0.0.1 is reachable from any open site. We check ALL state-changing
        routes: otherwise nobody would notice a guard missing on one of them.

        GET is excluded on purpose: the browser will send a foreign request,
        but will not let the page READ the response (CORS), and responses hold
        no secrets anyway. The exception is WebSocket, CORS does not apply to
        it; it is checked separately."""
        for method, path, body in SETUP_ROUTES:
            if method != "POST":
                continue
            with self.subTest(route=path):
                r = await self.client.request(
                    method, path, json=body,
                    headers={**JSON, "Origin": "http://evil.example"})
                self.assertEqual(r.status, 403)
        self.assertEqual(setup.water_bowl(setup.ZONES_FILE)["side"], "left")
        self.assertFalse(setup.camera_configured(setup.GO2RTC_FILE))

    async def test_foreign_host_header_is_refused_on_every_route(self):
        """DNS rebinding: a foreign name, our address."""
        for method, path, body in SETUP_ROUTES:
            with self.subTest(route=path):
                r = await self.client.request(method, path, json=body,
                                              headers={**JSON, "Host": "cam.evil.example"})
                self.assertEqual(r.status, 403)

    async def test_post_without_the_guard_header_is_refused(self):
        """A simple request (no non-standard header) leaves a foreign site
        without a preflight OPTIONS, and aiohttp does not check the content type."""
        for method, path, body in SETUP_ROUTES:
            if method != "POST":
                continue
            with self.subTest(route=path):
                r = await self.client.post(
                    path, data="{}", headers={"content-type": "text/plain"})
                self.assertEqual(r.status, 403)

    async def test_cross_site_fetch_marker_is_refused(self):
        """The browser sets Sec-Fetch-Site itself, and it cannot be forged."""
        r = await self.client.post("/api/setup/bowls", json={"side": "right"},
                                   headers={**JSON, "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 403)

    async def test_live_websockets_are_guarded_too(self):
        """The same-origin policy does not apply to WebSocket: without a guard
        any open tab could watch the camera video."""
        for path in ("/api/live", "/api/stream?src=dog_cam"):
            with self.subTest(path=path):
                r = await self.client.get(path, headers={
                    "Origin": "http://evil.example", "Connection": "Upgrade",
                    "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
                self.assertEqual(r.status, 403)

    async def test_malformed_json_is_400_not_500(self):
        r = await self.client.post("/api/setup/bowls", data="[1, 2]", headers=JSON)
        self.assertEqual(r.status, 400)

    # ------------------------------------------------------------------ camera

    async def test_camera_saved_end_to_end(self):
        cam = await FakeCamera().start()
        try:
            r = await self.client.post("/api/setup/camera", headers=JSON, json={
                "host": "127.0.0.1", "port": cam.port, "path": cam.path,
                "user": cam.user, "password": cam.password})
            body = await r.json()
        finally:
            await cam.stop()
        self.assertEqual(r.status, 200, body)
        self.assertTrue(body["ok"])
        self.assertNotIn(cam.password, str(body))

        # go2rtc got the stream on the fly, with the same request start.sh makes
        self.assertEqual(self.go2rtc.puts[0][0], "dog_cam")
        self.assertIn("s3cret", self.go2rtc.puts[0][1])

        # the password lives in exactly one file
        self.assertIn(cam.password, setup.GO2RTC_FILE.read_text())
        self.assertNotIn(cam.password, setup.ENV_FILE.read_text())
        env = dotenv_values(setup.ENV_FILE)
        self.assertEqual(env["PAWSIGHT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam")
        self.assertEqual(env["PAWSIGHT_DETECT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam")
        # a plain camera has no pan/tilt: otherwise the detector would wait for a PTZ that does not exist
        self.assertEqual(env["PAWSIGHT_FOLLOW_DOG"], "0")
        self.assertEqual(env["PAWSIGHT_TILT_HOLD"], "0")
        self.assertEqual(env["PAWSIGHT_SEARCH_SEC"], "0")

    async def test_state_never_hands_back_the_password(self):
        """The page re-reads the state right after saving, and that is the
        main place the password could find its way back to the browser."""
        cam = await FakeCamera().start()
        try:
            await self.save_camera(cam)
            r = await self.client.get("/api/setup/state")
            text = await r.text()
        finally:
            await cam.stop()
        self.assertNotIn(cam.password, text)
        self.assertNotIn(cam.user, text)
        self.assertIn("…@", (await r.json())["camera"]["masked"])

    async def test_go2rtc_error_quoting_the_source_is_masked(self):
        """go2rtc quotes the source in full in its response, password included."""
        self.go2rtc.fail_with = "streams: dial SRC: connection refused"
        cam = await FakeCamera().start()
        try:
            _, body = await self.save_camera(cam)
        finally:
            await cam.stop()
        self.assertNotIn(cam.password, str(body))
        self.assertIn("…@", str(body))

    async def test_file_is_written_when_go2rtc_is_down(self):
        """This branch is what it is all for: go2rtc is not up, and the address
        must not get lost, or the user "saved" into nowhere."""
        await self.g2r_server.close()
        cam = await FakeCamera().start()
        try:
            r, body = await self.save_camera(cam)
        finally:
            await cam.stop()
        self.assertEqual(r.status, 200, body)
        self.assertTrue(setup.camera_configured(setup.GO2RTC_FILE))
        self.assertTrue(any("go2rtc did not answer" in s["text"] for s in body["steps"]), body)

    async def test_running_go2rtc_keeps_its_own_file(self):
        """A live go2rtc edits the yaml itself, by inserting into the text, with comments.
        Overwriting it with our safe_dump would wipe the template's explanations."""
        self.go2rtc.writes_config = True
        cam = await FakeCamera().start()
        try:
            r, body = await self.save_camera(cam)
        finally:
            await cam.stop()
        self.assertEqual(r.status, 200, body)
        text = setup.GO2RTC_FILE.read_text()
        self.assertIn("The exact path depends on the manufacturer", text)
        self.assertEqual(setup.read_stream("dog_cam", setup.GO2RTC_FILE).split("@")[-1],
                         f"127.0.0.1:{cam.port}{cam.path}")

    async def test_config_file_becomes_private_even_if_go2rtc_wrote_it(self):
        """install.sh copies the template with 644 and go2rtc does not change
        permissions, while the file ends up holding the camera password."""
        self.go2rtc.writes_config = True
        setup.GO2RTC_FILE.chmod(0o644)
        cam = await FakeCamera().start()
        try:
            await self.save_camera(cam)
        finally:
            await cam.stop()
        import stat as st
        self.assertEqual(st.S_IMODE(setup.GO2RTC_FILE.stat().st_mode), 0o600)

    async def test_dead_second_stream_is_refused(self):
        """go2rtc's PUT does not dial the camera: an unchecked substream would
        give a green tick and a detector reading an address with no frames."""
        cam = await FakeCamera().start()
        try:
            r, body = await self.save_camera(cam, sub_url="rtsp://127.0.0.1:9/stream2")
        finally:
            await cam.stop()
        self.assertEqual(r.status, 400)
        self.assertIn("second stream", body["msg"])
        self.assertFalse(setup.camera_configured(setup.GO2RTC_FILE))

    async def test_forced_dead_second_stream_leaves_detector_on_main(self):
        cam = await FakeCamera().start()
        try:
            r, body = await self.save_camera(cam, sub_url="rtsp://127.0.0.1:9/stream2",
                                             force=True)
        finally:
            await cam.stop()
        self.assertEqual(r.status, 200, body)
        env = dotenv_values(setup.ENV_FILE)
        self.assertEqual(env["PAWSIGHT_DETECT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam")

    async def test_second_stream_switches_detector_only(self):
        cam = await FakeCamera().start()
        sub = await FakeCamera(path="/stream2").start()
        try:
            r = await self.client.post("/api/setup/camera", headers=JSON, json={
                "host": "127.0.0.1", "port": cam.port, "path": cam.path,
                "user": cam.user, "password": cam.password,
                "sub_url": sub.url()})
            self.assertEqual(r.status, 200, await r.json())
        finally:
            await cam.stop()
            await sub.stop()
        env = dotenv_values(setup.ENV_FILE)
        self.assertEqual(env["PAWSIGHT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam")
        self.assertEqual(env["PAWSIGHT_DETECT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam_sd")
        self.assertTrue(setup.read_stream("dog_cam_sd", setup.GO2RTC_FILE))

    async def test_second_stream_from_parts(self):
        """The brand presets send the substream as parts (same login, other path)."""
        cam = await FakeCamera().start()
        sub = await FakeCamera(path="/stream2").start()
        try:
            r = await self.client.post("/api/setup/camera", headers=JSON, json={
                "host": "127.0.0.1", "port": cam.port, "path": cam.path,
                "user": cam.user, "password": cam.password,
                "sub_host": "127.0.0.1", "sub_port": sub.port, "sub_path": sub.path,
                "sub_user": sub.user, "sub_password": sub.password})
            self.assertEqual(r.status, 200, await r.json())
        finally:
            await cam.stop()
            await sub.stop()
        self.assertTrue(setup.read_stream("dog_cam_sd", setup.GO2RTC_FILE).endswith(
            f"127.0.0.1:{sub.port}/stream2"))

    async def test_unreachable_camera_is_not_written(self):
        """Writing it "just in case" means showing a green tick and leaving the
        user to find out the truth a week later from empty statistics."""
        r = await self.client.post("/api/setup/camera", headers=JSON, json={
            "host": "127.0.0.1", "port": 9, "path": "/s"})
        self.assertEqual(r.status, 400)
        self.assertFalse(setup.camera_configured(setup.GO2RTC_FILE))
        self.assertEqual(self.go2rtc.puts, [])

    async def test_force_writes_despite_silence(self):
        r = await self.client.post("/api/setup/camera", headers=JSON, json={
            "host": "127.0.0.1", "port": 9, "path": "/s", "force": True})
        self.assertEqual(r.status, 200)
        self.assertTrue(setup.camera_configured(setup.GO2RTC_FILE))
        steps = await r.json()
        self.assertFalse(steps["steps"][0]["ok"])       # the camera still did not answer

    async def test_bad_url_is_refused_before_the_network(self):
        r = await self.client.post("/api/setup/camera", headers=JSON,
                                   json={"url": "http://1.2.3.4/s"})
        self.assertEqual(r.status, 400)
        self.assertIn("rtsp", (await r.json())["msg"])

    # ---------------------------------------------------------- telegram

    async def test_telegram_saves_but_never_echoes_the_token(self):
        with patch("pawsight.notify.check", return_value={
                "token_ok": True, "bot": "@pawsight_bot", "error": None,
                "recipients": [{"n": 1, "ok": True, "reason": "delivered"}]}):
            r = await self.client.post("/api/setup/telegram", headers=JSON,
                                       json={"token": "123:SECRET", "chats": "111"})
            body = await r.json()
        self.assertEqual(r.status, 200)
        self.assertNotIn("SECRET", str(body))
        self.assertEqual(sorted(body["saved"]),
                         ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        self.assertEqual(dotenv_values(setup.ENV_FILE)["TELEGRAM_BOT_TOKEN"], "123:SECRET")

    async def test_empty_token_reuses_the_stored_one(self):
        """Otherwise re-checking the recipients would wipe the configured token."""
        setup.set_env({"TELEGRAM_BOT_TOKEN": "123:STORED", "TELEGRAM_CHAT_ID": "111"},
                      path=setup.ENV_FILE, example=REPO / ".env.example")
        with patch("pawsight.notify.check", return_value={
                "token_ok": True, "bot": None, "error": None, "recipients": []}) as check:
            await self.client.post("/api/setup/telegram", headers=JSON,
                                   json={"token": "", "chats": "222"})
        self.assertEqual(check.call_args.args[0], "123:STORED")
        env = dotenv_values(setup.ENV_FILE)
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "123:STORED")
        self.assertEqual(env["TELEGRAM_CHAT_ID"], "222")

    async def test_bad_token_is_not_written(self):
        with patch("pawsight.notify.check", return_value={
                "token_ok": False, "bot": None, "recipients": [],
                "error": "Telegram rejected the token (401)"}):
            r = await self.client.post("/api/setup/telegram", headers=JSON,
                                       json={"token": "123:WRONG", "chats": "111"})
        self.assertEqual((await r.json())["saved"], [])
        self.assertNotIn("WRONG", setup.ENV_FILE.read_text() if setup.ENV_FILE.exists() else "")

    async def test_only_bowls_are_sent_to_the_bowl_picker(self):
        """The observation also carries furniture anchors. The page labels the
        outermost boxes "LEFT" and "RIGHT": with a table left of the bowls, the
        user would confirm the table. Exactly what section 6 of AGENTS.md
        forbids, done by someone else's hands."""
        self.dash.latest = {
            "ts": 1e9, "size": [1280, 720],
            "anchors": [{"cls": "dining table", "box": [10, 100, 240, 860]},
                        {"cls": "bowl", "box": [600, 500, 660, 540]},
                        {"cls": "refrigerator", "box": [900, 90, 1100, 700]},
                        {"cls": "bowl", "box": [670, 505, 730, 545]}],
            "zones": {"water": {"label": "water bowl", "poly": [[1, 2]]},
                      "food": {"label": "food bowl", "poly": [[3, 4]]}},
        }
        s = await (await self.client.get("/api/setup/state")).json()
        self.assertEqual(s["bowls"]["anchors"],
                         [[600, 500, 660, 540], [670, 505, 730, 545]])
        self.assertTrue(s["bowls"]["visible"])

    # ------------------------------------------------------------------ bowls

    async def test_bowls_endpoint_writes_and_records_who_said_so(self):
        r = await self.client.post("/api/setup/bowls", headers=JSON, json={"side": "right"})
        self.assertEqual(r.status, 200)
        self.assertEqual(setup.water_bowl(setup.ZONES_FILE)["side"], "right")
        self.assertIsNotNone(setup.water_bowl(setup.ZONES_FILE)["confirmed"])

    async def test_bowls_endpoint_refuses_nonsense(self):
        r = await self.client.post("/api/setup/bowls", headers=JSON, json={"side": "top"})
        self.assertEqual(r.status, 400)

    # -------------------------------------------------------------------- "/"

    async def test_root_shows_wizard_until_camera_is_set(self):
        page = await (await self.client.get("/")).text()
        self.assertIn("pawsight setup", page)

    async def test_root_shows_dashboard_once_camera_is_set(self):
        setup.write_go2rtc_stream("rtsp://1.2.3.4/s", path=setup.GO2RTC_FILE)
        page = await (await self.client.get("/")).text()
        self.assertIn("live view", page)

    async def test_dashboard_reachable_even_before_setup(self):
        page = await (await self.client.get("/?dashboard=1")).text()
        self.assertIn("live view", page)


if __name__ == "__main__":
    unittest.main()
