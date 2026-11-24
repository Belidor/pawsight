"""Webcam source (MacBook built-in camera, USB camera).

There is no camera in CI, so what is checked is everything around it: the
go2rtc source we build for each OS, what we keep from go2rtc's device list
(and, above all, what we throw away: the "Capture screen" entries on macOS
are the whole screen), and that the setup page saves a webcam the same way as
an IP camera, with the frame test going through go2rtc and never writing
go2rtc.yaml on its own.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dotenv import dotenv_values

from pawsight import setup
from pawsight.config import Config
from pawsight.web import Dashboard
from pawsight.web_setup import WEBCAM_PROBE
from tests.test_setup import ZONES_FIXTURE

REPO = Path(__file__).resolve().parent.parent
JSON = {"content-type": "application/json", "X-Pawsight": "1"}
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 2048 + b"\xff\xd9"

# Answers of go2rtc v1.9.14 /api/ffmpeg/devices, trimmed from real machines.
MAC_DEVICES = {"sources": [
    {"name": "FaceTime HD Camera", "url": "ffmpeg:device?video=FaceTime HD Camera"},
    {"name": "Capture screen 0", "url": "ffmpeg:device?video=Capture screen 0"},
    {"name": "MacBook Pro Microphone", "url": "ffmpeg:device?audio=MacBook Pro Microphone"},
]}
LINUX_DEVICES = {"sources": [
    {"name": "YUYV 4:2:2", "info": "640x480 1280x720",
     "url": "ffmpeg:device?video=/dev/video0&input_format=yuyv422&video_size=640x480#video=h264#hardware"},
    {"name": "Motion-JPEG", "info": "640x480 1280x720 1920x1080",
     "url": "ffmpeg:device?video=/dev/video0&input_format=mjpeg&video_size=640x480"},
    {"name": "YUYV 4:2:2", "info": "640x480",
     "url": "ffmpeg:device?video=/dev/video2&input_format=yuyv422&video_size=640x480#video=h264#hardware"},
    {"name": "ALSA default", "info": " ",
     "url": "ffmpeg:device?audio=default&channels=1&sample_rate=16000&#audio=opus"},
]}


def query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(url[len(setup.WEBCAM_PREFIX):].split("#")[0]).items()}


class BuildUrl(unittest.TestCase):
    def test_mac_name_is_encoded_and_has_30_fps(self):
        url = setup.build_webcam_url("FaceTime HD Camera", "1280x720", system="Darwin")
        # go2rtc refuses a source with a space in it
        self.assertNotIn(" ", url)
        self.assertTrue(url.startswith("ffmpeg:device?"))
        self.assertTrue(url.endswith("#video=h264#hardware"))
        q = query(url)
        self.assertEqual(q["video"], "FaceTime HD Camera")
        self.assertEqual(q["video_size"], "1280x720")
        # avfoundation's default 29.97 is refused by the built-in Mac cameras
        self.assertEqual(q["framerate"], "30")

    def test_mac_name_cannot_break_out_of_the_query(self):
        url = setup.build_webcam_url("Cam&raw=-f null#x", system="Darwin")
        self.assertEqual(query(url)["video"], "Cam&raw=-f null#x")
        self.assertNotIn("raw", parse_qs(url.split("?", 1)[1].split("#")[0]))
        self.assertEqual(url.count("#"), 2)

    def test_mac_quote_is_refused(self):
        # go2rtc puts the name inside "…" on the ffmpeg command line
        with self.assertRaises(setup.SetupError):
            setup.build_webcam_url('Cam" -i "/etc/passwd', system="Darwin")

    def test_linux_device_and_format(self):
        url = setup.build_webcam_url("/dev/video0", "640x480", "mjpeg", system="Linux")
        q = query(url)
        self.assertEqual(q["video"], "/dev/video0")
        self.assertEqual(q["input_format"], "mjpeg")
        self.assertEqual(q["framerate"], "30")

    def test_linux_refuses_anything_but_a_video_device(self):
        for bad in ("/etc/passwd", "0", "/dev/video0 -f", "../dev/video0"):
            with self.subTest(bad=bad), self.assertRaises(setup.SetupError):
                setup.build_webcam_url(bad, system="Linux")
        with self.assertRaises(setup.SetupError):
            setup.build_webcam_url("/dev/video0", input_format="mjpeg -y", system="Linux")

    def test_size_must_be_one_of_ours(self):
        with self.assertRaises(setup.SetupError):
            setup.build_webcam_url("FaceTime HD Camera", "1280x720 -y", system="Darwin")
        with self.assertRaises(setup.SetupError):
            setup.build_webcam_url("", system="Darwin")

    def test_describe(self):
        url = setup.build_webcam_url("FaceTime HD Camera", system="Darwin")
        self.assertEqual(setup.describe_webcam(url), "webcam: FaceTime HD Camera, 1280x720")
        self.assertEqual(setup.describe_webcam(setup.build_webcam_url("0", system="Darwin")),
                         "webcam: camera #1, 1280x720")
        self.assertTrue(setup.is_webcam(url))
        self.assertFalse(setup.is_webcam("rtsp://1.2.3.4/s"))

    def test_saved_source_survives_the_yaml_round_trip(self):
        d = Path(tempfile.mkdtemp())
        try:
            f = d / "go2rtc.yaml"
            shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", f)
            url = setup.build_webcam_url("FaceTime HD Camera", system="Darwin")
            setup.write_go2rtc_stream(url, path=f)
            # `#` after a non-space is not a yaml comment, but it is worth proving
            self.assertEqual(setup.read_stream(path=f), url)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class DeviceList(unittest.TestCase):
    def test_mac_keeps_cameras_only(self):
        got = setup.webcam_devices(MAC_DEVICES, system="Darwin")
        # "Capture screen" is the whole screen: it must never be offered
        self.assertEqual([d["id"] for d in got], ["FaceTime HD Camera"])

    def test_linux_one_entry_per_device_mjpeg_preferred(self):
        got = setup.webcam_devices(LINUX_DEVICES, system="Linux")
        self.assertEqual([(d["id"], d["input_format"]) for d in got],
                         [("/dev/video0", "mjpeg"), ("/dev/video2", "yuyv422")])

    def test_garbage_is_an_empty_list(self):
        for junk in (None, [], {}, {"sources": "x"}, {"sources": [1, None, {"url": 5}]}):
            with self.subTest(junk=junk):
                self.assertEqual(setup.webcam_devices(junk, system="Linux"), [])


class FakeGo2rtc:
    """go2rtc with a webcam: device list, the frame endpoint and stream PUT."""

    def __init__(self, devices=MAC_DEVICES, frame: bytes | None = JPEG, error: str = ""):
        self.devices, self.frame, self.error = devices, frame, error
        self.frames: list[dict] = []
        self.puts: list[tuple[str, str]] = []

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes([
            web.get("/api/streams", self.streams),
            web.put("/api/streams", self.put),
            web.get("/api/ffmpeg/devices", self.list_devices),
            web.get("/api/frame.jpeg", self.frame_jpeg),
        ])
        return app

    async def streams(self, _req):
        return web.json_response({"dog_cam": {}, WEBCAM_PROBE: {}})

    async def list_devices(self, _req):
        return web.json_response(self.devices)

    async def put(self, req):
        self.puts.append((req.query.get("name", ""), req.query.get("src", "")))
        return web.Response(text="")

    async def frame_jpeg(self, req):
        self.frames.append(dict(req.query))
        if self.frame is None:
            return web.Response(status=500, text=self.error)
        return web.Response(body=self.frame, content_type="image/jpeg")


class WebcamRoutes(unittest.IsolatedAsyncioTestCase):
    go2rtc_kwargs: dict = {}

    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self._paths = (setup.ENV_FILE, setup.GO2RTC_FILE, setup.ZONES_FILE)
        setup.ENV_FILE = self.dir / ".env"
        setup.GO2RTC_FILE = self.dir / "go2rtc.yaml"
        setup.ZONES_FILE = self.dir / "zones.yaml"
        shutil.copy(REPO / "go2rtc" / "go2rtc.example.yaml", setup.GO2RTC_FILE)
        shutil.copy(ZONES_FIXTURE, setup.ZONES_FILE)
        self._system = setup._is_mac
        setup._is_mac = lambda system=None: (system or "Darwin") == "Darwin"

        self.go2rtc = FakeGo2rtc(**self.go2rtc_kwargs)
        self.g2r_server = TestServer(self.go2rtc.app())
        await self.g2r_server.start_server()
        self.dash = Dashboard(Config())
        self.dash.go2rtc = str(self.g2r_server.make_url("")).rstrip("/")
        self.client = TestClient(TestServer(self.dash.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.g2r_server.close()
        setup._is_mac = self._system
        setup.ENV_FILE, setup.GO2RTC_FILE, setup.ZONES_FILE = self._paths
        shutil.rmtree(self.dir, ignore_errors=True)

    async def test_device_list(self):
        r = await self.client.get("/api/setup/webcams")
        body = await r.json()
        self.assertTrue(body["ok"])
        self.assertEqual([d["id"] for d in body["devices"]], ["FaceTime HD Camera"])
        self.assertIn("1280x720", body["sizes"])

    async def test_probe_returns_a_frame_and_writes_nothing(self):
        before = setup.GO2RTC_FILE.read_text()
        r = await self.client.post("/api/setup/probe", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera", "size": "1280x720"})
        body = await r.json()
        self.assertTrue(body["ok"], body)
        self.assertTrue(body["frame"].startswith("data:image/jpeg;base64,"))
        # the test frame goes through a named in-memory stream, never the config
        self.assertEqual(self.go2rtc.frames[0]["name"], WEBCAM_PROBE)
        self.assertTrue(setup.is_webcam(self.go2rtc.frames[0]["src"]))
        self.assertEqual(self.go2rtc.puts, [])
        self.assertEqual(setup.GO2RTC_FILE.read_text(), before)

    async def test_bad_size_is_400_before_go2rtc(self):
        r = await self.client.post("/api/setup/probe", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera", "size": "99x99"})
        self.assertEqual(r.status, 400)
        self.assertEqual(self.go2rtc.frames, [])

    async def test_saved_like_a_plain_camera(self):
        r = await self.client.post("/api/setup/camera", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera", "size": "1280x720"})
        body = await r.json()
        self.assertEqual(r.status, 200, body)
        self.assertEqual(body["masked"], "webcam: FaceTime HD Camera, 1280x720")
        name, src = self.go2rtc.puts[0]
        self.assertEqual(name, "dog_cam")
        self.assertTrue(setup.is_webcam(src))
        self.assertEqual(setup.read_stream(), src)
        env = dotenv_values(setup.ENV_FILE)
        # one stream: the detector reads the main one
        self.assertEqual(env["PAWSIGHT_DETECT_RTSP"], "rtsp://127.0.0.1:8554/dog_cam")
        # no pan/tilt on a webcam
        self.assertEqual(env["PAWSIGHT_FOLLOW_DOG"], "0")
        self.assertEqual(env["PAWSIGHT_TILT_HOLD"], "0")
        self.assertEqual(env["PAWSIGHT_SEARCH_SEC"], "0")

        s = await (await self.client.get("/api/setup/state")).json()
        self.assertEqual(s["camera"]["kind"], "webcam")
        self.assertEqual(s["camera"]["masked"], "webcam: FaceTime HD Camera, 1280x720")
        # the probe's in-memory stream is not configuration
        self.assertNotIn(WEBCAM_PROBE, s["go2rtc"]["streams"])


class WebcamWithoutFrame(WebcamRoutes):
    go2rtc_kwargs = {"frame": None, "error": "exec/rtsp: EOF"}

    async def test_device_list(self):
        pass

    async def test_probe_returns_a_frame_and_writes_nothing(self):
        r = await self.client.post("/api/setup/probe", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera"})
        body = await r.json()
        self.assertFalse(body["ok"])
        self.assertIn("Privacy & Security", body["msg"])
        self.assertIn("EOF", body["msg"])

    async def test_saved_like_a_plain_camera(self):
        r = await self.client.post("/api/setup/camera", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera"})
        self.assertEqual(r.status, 400)
        self.assertFalse(setup.camera_configured())
        r = await self.client.post("/api/setup/camera", headers=JSON, json={
            "kind": "webcam", "device": "FaceTime HD Camera", "force": True})
        self.assertEqual(r.status, 200)
        self.assertTrue(setup.camera_configured())


class NoCameras(unittest.IsolatedAsyncioTestCase):
    async def test_empty_list_says_what_to_install(self):
        g = FakeGo2rtc(devices={"sources": []})
        srv = TestServer(g.app())
        await srv.start_server()
        dash = Dashboard(Config())
        dash.go2rtc = str(srv.make_url("")).rstrip("/")
        client = TestClient(TestServer(dash.app()))
        await client.start_server()
        try:
            body = await (await client.get("/api/setup/webcams")).json()
        finally:
            await client.close()
            await srv.close()
        self.assertFalse(body["ok"])
        self.assertIn("ffmpeg", body["msg"])


if __name__ == "__main__":
    unittest.main()
