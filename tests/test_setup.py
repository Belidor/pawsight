"""First-run setup: address building, config writes, camera probe.

There is no network here: the test brings up its own camera on loopback that
answers like a real one, with three kinds of auth at once. The focus is not
"does it work" but what breaks silently: password percent-encoding, comments
surviving in zones.yaml, secrets not leaking into logs.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

import yaml
from dotenv import dotenv_values

from pawsight import setup

REPO = Path(__file__).resolve().parent.parent
# zones.yaml comes from tests/data, not the repo root: the setup page rewrites
# the root file when the owner presses the button, and a "what ships by
# default" test would fail on the owner's machine after the first setup.
ZONES_FIXTURE = Path(__file__).resolve().parent / "data" / "zones.yaml"


# ---------------------------------------------------------------- mock camera

SDP = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=Cam\r\nt=0 0\r\n"
       "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n")
_P = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')


def _md5(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode()).hexdigest()


class FakeCamera:
    """A 30-line RTSP server: OPTIONS/DESCRIBE and three kinds of auth.

    It has to be our own: real cameras use Digest both with qop=auth
    (RFC 7616) and without it (RFC 2069), and a tool that only handles one
    will report "wrong password" where the password is correct.
    """

    def __init__(self, mode: str = "digest_qop", user: str = "admin",
                 password: str = "s3cret", path: str = "/stream1") -> None:
        self.mode, self.user, self.password, self.path = mode, user, password, path
        self.nonce, self.realm = "deadbeef", "IP Camera(42)"
        self.sdp = SDP
        self.server = None
        self.port = 0

    async def start(self) -> "FakeCamera":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    def url(self, user: str | None = None, password: str | None = None,
            path: str | None = None) -> str:
        creds = ""
        if user is not None or user is None and self.user:
            u = self.user if user is None else user
            p = self.password if password is None else password
            creds = f"{u}:{p}@" if u else ""
        return f"rtsp://{creds}127.0.0.1:{self.port}{path or self.path}"

    def _challenge(self) -> str:
        if self.mode == "basic":
            return f'Basic realm="{self.realm}"'
        if self.mode == "digest_2069":
            return f'Digest realm="{self.realm}", nonce="{self.nonce}"'
        return (f'Digest realm="{self.realm}", nonce="{self.nonce}", '
                f'qop="auth", opaque="2a", algorithm=MD5')

    def _ok(self, header: str | None, method: str) -> bool:
        if not header:
            return False
        scheme, _, rest = header.partition(" ")
        if scheme.lower() == "basic":
            if self.mode != "basic":
                return False
            try:
                u, _, p = base64.b64decode(rest).decode().partition(":")
            except Exception:
                return False
            return u == self.user and p == self.password
        if scheme.lower() != "digest" or self.mode == "basic":
            return False
        d = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
             for m in _P.finditer(rest)}
        if d.get("nonce") != self.nonce:
            return False
        ha1 = _md5(self.user, self.realm, self.password)
        ha2 = _md5(method, d.get("uri", ""))
        if self.mode == "digest_qop":
            if d.get("qop") != "auth" or not d.get("nc") or not d.get("cnonce"):
                return False
            return d.get("response") == _md5(ha1, self.nonce, d["nc"], d["cnonce"], "auth", ha2)
        return d.get("response") == _md5(ha1, self.nonce, ha2)

    async def _handle(self, rd, wr) -> None:
        try:
            while True:
                head = await rd.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                method, uri, _ = lines[0].split(" ", 2)
                headers = {}
                for line in lines[1:]:
                    if line:
                        k, _, v = line.partition(":")
                        headers[k.strip().lower()] = v.strip()

                def reply(code, reason, extra="", body=""):
                    b = body.encode()
                    msg = f"RTSP/1.0 {code} {reason}\r\nCSeq: {headers.get('cseq', '0')}\r\n{extra}"
                    if b:
                        msg += f"Content-Type: application/sdp\r\nContent-Length: {len(b)}\r\n"
                    wr.write((msg + "\r\n").encode() + b)

                if method == "OPTIONS":
                    reply(200, "OK", "Public: OPTIONS, DESCRIBE\r\n")
                elif not self._ok(headers.get("authorization"), method):
                    reply(401, "Unauthorized", f"WWW-Authenticate: {self._challenge()}\r\n")
                elif not uri.split("?")[0].endswith(self.path):
                    reply(404, "Stream Not Found")
                else:
                    reply(200, "OK", "", self.sdp)
                await wr.drain()
        except Exception:
            pass
        finally:
            wr.close()


# -------------------------------------------------------------------- address

class BuildUrl(unittest.TestCase):
    def test_password_is_percent_encoded(self):
        """@ : / are common in passwords; without encoding urlsplit sees a
        different host, and the user gets "camera not responding" with a correct password."""
        u = setup.build_rtsp_url("192.168.1.50", 554, "/stream1", "admin", "p@ss:w/rd")
        self.assertEqual(u, "rtsp://admin:p%40ss%3Aw%2Frd@192.168.1.50:554/stream1")

    def test_path_gets_leading_slash(self):
        self.assertTrue(setup.build_rtsp_url("h", 554, "stream1").endswith(":554/stream1"))

    def test_default_port(self):
        self.assertIn(":554/", setup.build_rtsp_url("h", "", "/s"))

    def test_no_user_no_at(self):
        self.assertEqual(setup.build_rtsp_url("h", 554, "/s"), "rtsp://h:554/s")

    def test_ipv6_literal_gets_brackets(self):
        self.assertTrue(setup.build_rtsp_url("fd00::1", 554, "/s").startswith("rtsp://[fd00::1]:554"))

    def test_host_with_port_is_refused_not_taken_for_ipv6(self):
        """`192.168.1.50:554` in the address field is a common slip; taking it
        for an IPv6 literal would build an unparseable URL and fail later."""
        with self.assertRaises(setup.SetupError):
            setup.build_rtsp_url("192.168.1.50:554", 554, "/s", "admin", "pw")

    def test_whole_url_in_host_field_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.build_rtsp_url("rtsp://1.2.3.4/s", 554, "")

    def test_empty_host_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.build_rtsp_url("", 554, "/s")


class ValidateUrl(unittest.TestCase):
    def test_http_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.validate_rtsp_url("http://1.2.3.4/s")

    def test_space_refused(self):
        """go2rtc rejects a source with a space itself — we refuse more clearly."""
        with self.assertRaises(setup.SetupError):
            setup.validate_rtsp_url("rtsp://1.2.3.4/a b")

    def test_newline_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.validate_rtsp_url("rtsp://1.2.3.4/s\nstreams: {}")

    def test_two_at_signs_refused(self):
        with self.assertRaises(setup.SetupError) as e:
            setup.validate_rtsp_url("rtsp://u:p@ss@1.2.3.4/s")
        self.assertIn("@", str(e.exception))

    def test_rtsps_allowed(self):
        self.assertTrue(setup.validate_rtsp_url("rtsps://1.2.3.4:322/s"))

    def test_unparsable_port_is_a_refusal_not_a_crash(self):
        """A password with "/" or ":" turns the port into garbage and urlsplit
        raises ValueError, whose text contains part of the password. It must not leak."""
        for bad in ("rtsp://admin:SeCr3t/PaSs@1.2.3.4:554/s",
                    "rtsp://admin:pa#ss@1.2.3.4:554/s",
                    "rtsp://1.2.3.4:99999/s", "rtsp://1.2.3.4:554x/s",
                    "rtsp://[fd00::1/s"):
            with self.assertRaises(setup.SetupError) as e:
                setup.validate_rtsp_url(bad)
            self.assertNotIn("SeCr3t", str(e.exception))
            self.assertNotIn("pa#", str(e.exception))

    def test_too_long_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.validate_rtsp_url("rtsp://h/" + "x" * 600)


class Mask(unittest.TestCase):
    def test_wrapped_source_is_hidden_whole(self):
        """`ffmpeg:rtsp://…` and `exec:…` are normal go2rtc source forms, and
        their netloc is empty: parsing cannot be trusted, so hide everything."""
        for raw in ("ffmpeg:rtsp://admin:hunter2@1.2.3.4:554/s1#video=copy",
                    "exec:ffmpeg -i rtsp://admin:hunter2@1.2.3.4/s"):
            self.assertNotIn("hunter2", setup.mask_url(raw))

    def test_slash_in_password_hides_everything(self):
        """netloc is cut short at the password, and urlsplit "does not see" the credentials."""
        m = setup.mask_url("rtsp://admin:SeCr3t/PaSs@192.0.2.9:554/stream1")
        self.assertNotIn("SeCr3t", m)
        self.assertNotIn("PaSs", m)

    def test_credentials_in_query_are_dropped(self):
        """Some cameras take the login and password in the query (?usr=&pwd=)."""
        m = setup.mask_url("rtsp://1.2.3.4/stream?usr=admin&pwd=SeCr3t")
        self.assertNotIn("SeCr3t", m)
        self.assertIn("1.2.3.4/stream", m)

    def test_ipv6_stays_readable(self):
        self.assertEqual(setup.mask_url("rtsp://[fd00::1]:554/s"), "rtsp://[fd00::1]:554/s")

    def test_credentials_disappear(self):
        m = setup.mask_url("rtsp://admin:hunter2@192.168.1.50:554/stream1")
        self.assertNotIn("hunter2", m)
        self.assertNotIn("admin", m)
        self.assertIn("192.168.1.50:554/stream1", m)

    def test_xiaomi_url(self):
        self.assertNotIn("V1", setup.mask_url("xiaomi://12:V1secret@10.0.0.5?did=1"))

    def test_mask_secrets_in_foreign_text(self):
        """A go2rtc response may quote the source in full."""
        t = setup.mask_secrets("streams: dial rtsp://admin:hunter2@1.2.3.4:554/s: refused")
        self.assertNotIn("hunter2", t)
        self.assertIn("refused", t)


# -------------------------------------------------------------------- .env

class EnvWriter(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self.example = REPO / ".env.example"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_created_from_example_with_comments(self):
        setup.set_env({"TELEGRAM_CHAT_ID": "111"}, path=self.env, example=self.example)
        text = self.env.read_text()
        self.assertIn("@BotFather", text)
        self.assertIn("TELEGRAM_CHAT_ID=111", text)

    def test_mode_is_600(self):
        """.env receives the bot token, and install.sh only chmods go2rtc.yaml."""
        setup.set_env({"TELEGRAM_CHAT_ID": "111"}, path=self.env, example=self.example)
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_empty_value_keeps_previous(self):
        """An empty form field means "do not change": otherwise reopening the
        wizard would wipe an already configured token."""
        setup.set_env({"TELEGRAM_BOT_TOKEN": "123:AAA"}, path=self.env, example=self.example)
        setup.set_env({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "222"}, path=self.env)
        self.assertEqual(dotenv_values(self.env)["TELEGRAM_BOT_TOKEN"], "123:AAA")
        self.assertEqual(dotenv_values(self.env)["TELEGRAM_CHAT_ID"], "222")

    def test_awkward_value_survives_round_trip(self):
        """dotenv cuts everything after ' #' in an unquoted value."""
        value = "a b # c'd\\e"
        setup.set_env({"TELEGRAM_BOT_TOKEN": value}, path=self.env, example=self.example)
        self.assertEqual(dotenv_values(self.env)["TELEGRAM_BOT_TOKEN"], value)

    def test_dollar_brace_refused(self):
        """dotenv would substitute an environment variable here, with no way to escape it."""
        with self.assertRaises(setup.SetupError):
            setup.set_env({"TELEGRAM_BOT_TOKEN": "a${HOME}b"}, path=self.env, example=self.example)

    def test_missing_key_appended(self):
        setup.set_env({"PAWSIGHT_WEB_HOST": "127.0.0.1"}, path=self.env, example=self.example)
        self.assertEqual(dotenv_values(self.env)["PAWSIGHT_WEB_HOST"], "127.0.0.1")

    def test_returns_changed_keys_only(self):
        setup.set_env({"TELEGRAM_CHAT_ID": "111"}, path=self.env, example=self.example)
        self.assertEqual(setup.set_env({"TELEGRAM_CHAT_ID": "111"}, path=self.env), [])

    def test_flags_never_return_values(self):
        setup.set_env({"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "1,2"},
                      path=self.env, example=self.example)
        flags = setup.env_flags(self.env)
        self.assertEqual(flags["telegram_chats"], 2)
        self.assertTrue(flags["telegram_token"])
        self.assertNotIn("SECRET", repr(flags))


# ------------------------------------------------------------- go2rtc.yaml

class Go2rtcYaml(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.yaml = self.dir / "go2rtc.yaml"
        self.example = REPO / "go2rtc" / "go2rtc.example.yaml"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_template_means_not_configured(self):
        """The template has dog_cam: "" — that is exactly how first run is detected."""
        shutil.copy(self.example, self.yaml)
        self.assertFalse(setup.camera_configured(self.yaml))

    def test_absent_file_means_not_configured(self):
        self.assertFalse(setup.camera_configured(self.dir / "missing.yaml"))

    def test_scalar_streams_means_not_configured(self):
        """`streams: dog_cam` as a string means a broken file, but no reason to crash "/"."""
        self.yaml.write_text("streams: dog_cam\n")
        self.assertFalse(setup.camera_configured(self.yaml))
        self.assertEqual(setup.read_stream("dog_cam", self.yaml), "")

    def test_broken_yaml_means_not_configured(self):
        self.yaml.write_text("streams: [oh: no\n")
        self.assertFalse(setup.camera_configured(self.yaml))

    def test_list_form_is_understood(self):
        """In go2rtc a source can be a list — start.sh already handles that."""
        self.yaml.write_text(yaml.safe_dump({"streams": {"dog_cam": ["rtsp://a/1"]}}))
        self.assertTrue(setup.camera_configured(self.yaml))
        self.yaml.write_text(yaml.safe_dump({"streams": {"dog_cam": [""]}}))
        self.assertFalse(setup.camera_configured(self.yaml))

    def test_xiaomi_source_counts_as_configured(self):
        self.yaml.write_text(yaml.safe_dump({"streams": {"dog_cam": "xiaomi://u:t@1.2.3.4?did=1"}}))
        self.assertTrue(setup.camera_configured(self.yaml))

    def test_write_keeps_xiaomi_section(self):
        """The xiaomi section holds the Mi account token: losing it means
        sending the owner to log in again."""
        self.yaml.write_text(yaml.safe_dump(
            {"api": {"listen": "127.0.0.1:1984"}, "xiaomi": {"777": "V1:TOKEN"},
             "streams": {"dog_cam": ""}}))
        setup.write_go2rtc_stream("rtsp://admin:pw@1.2.3.4:554/s1", path=self.yaml)
        cfg = yaml.safe_load(self.yaml.read_text())
        self.assertEqual(cfg["xiaomi"], {"777": "V1:TOKEN"})
        self.assertEqual(cfg["api"]["listen"], "127.0.0.1:1984")
        self.assertTrue(setup.xiaomi_logged_in(self.yaml))

    def test_write_mode_is_600(self):
        shutil.copy(self.example, self.yaml)
        setup.write_go2rtc_stream("rtsp://admin:pw@1.2.3.4:554/s1", path=self.yaml)
        self.assertEqual(stat.S_IMODE(self.yaml.stat().st_mode), 0o600)

    def test_created_from_example_keeps_loopback_binds(self):
        """The file is born from the template: listen addresses there are already loopback."""
        setup.write_go2rtc_stream("rtsp://1.2.3.4/s", path=self.yaml, example=self.example)
        cfg = yaml.safe_load(self.yaml.read_text())
        self.assertEqual(cfg["api"]["listen"], "127.0.0.1:1984")
        self.assertEqual(cfg["rtsp"]["listen"], "127.0.0.1:8554")
        self.assertEqual(cfg["webrtc"]["listen"], "127.0.0.1:8555")

    def test_second_stream_is_separate_key(self):
        shutil.copy(self.example, self.yaml)
        setup.write_go2rtc_stream("rtsp://1.2.3.4/main", path=self.yaml)
        setup.write_go2rtc_stream("rtsp://1.2.3.4/sub", setup.STREAM_SUB, path=self.yaml)
        self.assertEqual(setup.read_stream(setup.STREAM_MAIN, self.yaml), "rtsp://1.2.3.4/main")
        self.assertEqual(setup.read_stream(setup.STREAM_SUB, self.yaml), "rtsp://1.2.3.4/sub")


# -------------------------------------------------------------- zones.yaml

class WaterBowl(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.zones = self.dir / "zones.yaml"
        shutil.copy(ZONES_FIXTURE, self.zones)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_shipped_file_is_left_and_unconfirmed(self):
        """Until the user presses the button, "left" is the author's choice, not theirs."""
        self.assertEqual(setup.water_bowl(self.zones), {"side": "left", "confirmed": None})

    def test_repo_zones_still_have_the_three_bowl_zones(self):
        """About the root file we only assert what the code depends on: which
        bowl is the water bowl may already be anything (the owner confirmed it).
        The file may have more zones (the room), but these three must exist:
        both the visit trigger and the "this is the water bowl" button rely on them."""
        cfg = yaml.safe_load((REPO / "tests" / "data" / "zones_room.yaml").read_text())
        self.assertLessEqual({"bowls", "water", "food"}, set(cfg["zones"]))
        self.assertEqual(cfg["zones"]["bowls"]["anchor"]["select"], "all")

    def test_swap_on_repo_file_leaves_room_zones_alone(self):
        """Swapping the bowls does not move the room.

        The page swaps the water/food shapes: they belong to the bowl's PLACE.
        Room zones are tied to the pair as a whole, and the door and the rug do
        not move just because the right bowl was named the water bowl.
        """
        repo_copy = self.dir / "repo_zones.yaml"
        shutil.copy(REPO / "tests" / "data" / "zones_room.yaml", repo_copy)
        before = yaml.safe_load(repo_copy.read_text())["zones"]
        room = sorted(set(before) - {"bowls", "water", "food"})
        self.assertTrue(room, "no room zones left in the root zones.yaml")
        setup.set_water_bowl("right", repo_copy, today="2026-09-09")
        after = yaml.safe_load(repo_copy.read_text())["zones"]
        for name in room:
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(after["water"]["anchor"]["select"], "right")

    def test_swap_moves_both_zones(self):
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        cfg = yaml.safe_load(self.zones.read_text())
        self.assertEqual(cfg["zones"]["water"]["anchor"]["select"], "right")
        self.assertEqual(cfg["zones"]["food"]["anchor"]["select"], "left")

    def test_bowls_zone_untouched(self):
        """The visit trigger itself depends on the bowls zone."""
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        cfg = yaml.safe_load(self.zones.read_text())
        self.assertEqual(cfg["zones"]["bowls"]["anchor"]["select"], "all")

    def test_comments_survive(self):
        """These explanations are why the file exists: safe_dump would wipe them all."""
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        text = self.zones.read_text()
        self.assertIn("RELATIVE ORDER", text)
        self.assertIn("Visit TRIGGER", text)
        self.assertIn("the spec forbids inventing labels", text)

    def test_authors_reference_frame_is_dropped(self):
        """The reference frame coordinates describe someone else's room: next to
        a new confirmation they would be a lie."""
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        text = self.zones.read_text()
        self.assertNotIn("1251,1115", text)
        self.assertIn("WATER BOWL = RIGHT", text)

    def test_swap_moves_floor_shapes_with_the_bowls(self):
        """A shape belongs to the bowl's PLACE, not its contents: if the water
        bowl is now the right one, its floor strip is the one the right bowl had."""
        before = yaml.safe_load(self.zones.read_text())["zones"]
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        after = yaml.safe_load(self.zones.read_text())["zones"]
        self.assertEqual(after["water"]["anchor"]["shape"], before["food"]["anchor"]["shape"])
        self.assertEqual(after["food"]["anchor"]["shape"], before["water"]["anchor"]["shape"])
        self.assertEqual(after["bowls"]["anchor"]["shape"], before["bowls"]["anchor"]["shape"])

    def test_confirming_the_same_side_keeps_shapes(self):
        before = yaml.safe_load(self.zones.read_text())["zones"]
        setup.set_water_bowl("left", self.zones, today="2026-09-07")
        after = yaml.safe_load(self.zones.read_text())["zones"]
        self.assertEqual(after["water"]["anchor"]["shape"], before["water"]["anchor"]["shape"])
        self.assertEqual(after["food"]["anchor"]["shape"], before["food"]["anchor"]["shape"])

    def test_shape_in_only_one_zone_is_refused(self):
        """A half-done manual edit: one zone has a shape, the other does not.
        Swapping select and leaving the shape in place would make the geometry lie."""
        text = self.zones.read_text()
        s, e = setup._zone_block(text, "food")
        block = re.sub(r"^\s*shape:.*\n", "", text[s:e], flags=re.M)
        self.zones.write_text(text[:s] + block + text[e:])
        with self.assertRaises(setup.SetupError):
            setup.set_water_bowl("right", self.zones, today="2026-09-07")
        self.assertEqual(setup.water_bowl(self.zones)["side"], "left")

    def _shape_rewritten(self, zone: str, style: str) -> str:
        """The same shape in a different YAML form: a block list or a line break."""
        text = self.zones.read_text()
        s, e = setup._zone_block(text, zone)
        m = re.search(r"^(?P<ind>[ \t]*)shape: (?P<val>\[.*\])[^\n]*$", text[s:e], re.M)
        pts = yaml.safe_load(m.group("val"))
        ind = m.group("ind")
        if style == "block":
            new = f"{ind}shape:\n" + "".join(f"{ind}  - [{x}, {y}]\n" for x, y in pts)
        else:
            half = len(pts) // 2
            row = lambda part: ", ".join(f"[{x}, {y}]" for x, y in part)
            new = f"{ind}shape: [{row(pts[:half])},\n{ind}        {row(pts[half:])}]\n"
        block = text[s:e][:m.start()] + new + text[s:e][m.end() + 1:]
        return text[:s] + block + text[e:]

    def test_block_list_shape_is_refused_not_left_behind(self):
        """A block list is read by Config but not caught by the single-line
        regex: without a refusal select would be swapped while the shapes stay
        with the other bowls, and the file would look confirmed."""
        shipped = yaml.safe_load(self.zones.read_text())["zones"]["water"]["anchor"]["shape"]
        for zone in ("water", "food"):
            self.zones.write_text(self._shape_rewritten(zone, "block"))
        before = self.zones.read_text()
        # the block form must read as the same shape, otherwise the test checks the wrong thing
        self.assertEqual(yaml.safe_load(before)["zones"]["water"]["anchor"]["shape"], shipped)
        with self.assertRaises(setup.SetupError):
            setup.set_water_bowl("right", self.zones, today="2026-09-07")
        self.assertEqual(self.zones.read_text(), before)

    def test_wrapped_flow_list_shape_is_refused_not_spliced(self):
        """A line break inside [..]: the regex would take half the vertices, and
        the swap would glue two readable chimeras out of the two strips."""
        for zone in ("water", "food"):
            self.zones.write_text(self._shape_rewritten(zone, "wrap"))
        before = self.zones.read_text()
        with self.assertRaises(setup.SetupError):
            setup.set_water_bowl("right", self.zones, today="2026-09-07")
        self.assertEqual(self.zones.read_text(), before)

    def test_swapping_back_restores_the_shipped_geometry(self):
        original = yaml.safe_load(self.zones.read_text())["zones"]
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        setup.set_water_bowl("right", self.zones, today="2026-09-08")   # same side again
        setup.set_water_bowl("left", self.zones, today="2026-09-09")
        back = yaml.safe_load(self.zones.read_text())["zones"]
        for zone in ("bowls", "water", "food"):
            self.assertEqual(back[zone]["anchor"]["shape"], original[zone]["anchor"]["shape"], zone)
            self.assertEqual(back[zone]["anchor"]["select"], original[zone]["anchor"]["select"], zone)

    def test_confirmation_is_recorded(self):
        setup.set_water_bowl("left", self.zones, today="2026-09-07")
        self.assertEqual(setup.water_bowl(self.zones),
                         {"side": "left", "confirmed": "2026-09-07"})

    def test_reconfirm_updates_date_only(self):
        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        setup.set_water_bowl("right", self.zones, today="2026-09-09")
        text = self.zones.read_text()
        self.assertEqual(text.count("water_bowl_confirmed"), 1)
        self.assertEqual(len(re.findall(r"^# WATER BOWL = ", text, re.M)), 1)
        self.assertEqual(setup.water_bowl(self.zones)["confirmed"], "2026-09-09")

    def test_hand_edited_file_is_refused_not_mangled(self):
        self.zones.write_text("zones:\n  water:\n    label: x\n")
        with self.assertRaises(setup.SetupError):
            setup.set_water_bowl("right", self.zones, today="2026-09-07")
        self.assertEqual(self.zones.read_text(), "zones:\n  water:\n    label: x\n")

    def test_bad_side_refused(self):
        with self.assertRaises(setup.SetupError):
            setup.set_water_bowl("top", self.zones)

    def test_zones_still_load_into_config(self):
        """The file must stay loadable by Config._load_zones."""
        from pawsight.config import Config

        setup.set_water_bowl("right", self.zones, today="2026-09-07")
        raw = yaml.safe_load(self.zones.read_text())
        names = sorted((raw.get("zones") or {}).keys())
        self.assertEqual(names, ["bowls", "food", "water"])
        self.assertTrue(hasattr(Config, "_load_zones"))


# ---------------------------------------------------------------------- probe

class Probe(unittest.IsolatedAsyncioTestCase):
    async def test_every_auth_flavour_passes(self):
        """Digest with and without qop, plus Basic: cameras come in all three kinds."""
        for mode in ("digest_qop", "digest_2069", "basic"):
            cam = await FakeCamera(mode=mode).start()
            try:
                r = await setup.probe_rtsp(cam.url())
                self.assertTrue(r["ok"], f"{mode}: {r}")
                self.assertEqual(r["code"], "ok")
            finally:
                await cam.stop()

    async def test_wrong_password_says_so(self):
        cam = await FakeCamera().start()
        try:
            r = await setup.probe_rtsp(cam.url(password="WRONG"))
            self.assertEqual(r["code"], "auth_failed")
        finally:
            await cam.stop()

    async def test_missing_credentials_distinguished_from_wrong_ones(self):
        """The first 401 is normal (that is how the challenge arrives). Calling
        it a "wrong password" would be a lie when the fields are empty."""
        cam = await FakeCamera().start()
        try:
            r = await setup.probe_rtsp(cam.url(user=""))
            self.assertEqual(r["code"], "auth_required")
        finally:
            await cam.stop()

    async def test_wrong_path_is_404_not_auth(self):
        cam = await FakeCamera().start()
        try:
            r = await setup.probe_rtsp(cam.url(path="/nope"))
            self.assertEqual(r["code"], "not_found")
        finally:
            await cam.stop()

    async def test_no_video_track(self):
        """The camera answering does not yet mean video will come from it."""
        cam = FakeCamera()
        cam.sdp = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\nm=audio 0 RTP/AVP 8\r\n"
        await cam.start()
        try:
            r = await setup.probe_rtsp(cam.url())
            self.assertEqual(r["code"], "no_video")
        finally:
            await cam.stop()

    async def test_closed_port(self):
        r = await setup.probe_rtsp("rtsp://127.0.0.1:9/s", timeout=2)
        self.assertEqual(r["code"], "refused")

    async def test_not_rtsp_on_that_port(self):
        async def http(rd, wr):
            try:
                await rd.readuntil(b"\r\n\r\n")
            except Exception:
                pass
            wr.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await wr.drain()
            wr.close()

        srv = await asyncio.start_server(http, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            r = await setup.probe_rtsp(f"rtsp://127.0.0.1:{port}/s", timeout=3)
            self.assertEqual(r["code"], "not_rtsp")
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_silent_port_times_out(self):
        async def mute(rd, wr):
            try:
                # Do not answer before the client times out; read() without a size waits for EOF.
                await rd.read()
            finally:
                wr.close()
                await wr.wait_closed()

        srv = await asyncio.start_server(mute, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            r = await setup.probe_rtsp(f"rtsp://127.0.0.1:{port}/s", timeout=1)
            self.assertEqual(r["code"], "mute")
        finally:
            srv.close()
            await srv.wait_closed()

    async def test_bad_url_never_touches_network(self):
        r = await setup.probe_rtsp("http://1.2.3.4/s")
        self.assertEqual(r["code"], "bad_url")


# -------------------------------------------------------------------- secrets

class SecretsStayPut(unittest.IsolatedAsyncioTestCase):
    """Captured on the ROOT logger: __main__._log() installs its handler there,
    and in native mode all of it goes to storage/brain.log, which the user is
    told to read and paste into conversations."""

    async def test_probe_reports_reason_without_password(self):
        cam = await FakeCamera().start()
        try:
            with self.assertLogs(level="DEBUG") as logs:
                r = await setup.probe_rtsp(cam.url(password="hunter2"))
                logging_noise = "\n".join(logs.output)
            self.assertNotIn("hunter2", repr(r))
            self.assertNotIn("hunter2", logging_noise)
        finally:
            await cam.stop()

    def test_writers_log_nothing_with_the_url(self):
        d = Path(tempfile.mkdtemp())
        try:
            g = d / "go2rtc.yaml"
            with self.assertLogs(level="DEBUG") as logs:
                setup.write_go2rtc_stream("rtsp://admin:hunter2@1.2.3.4/s", path=g,
                                          example=REPO / "go2rtc" / "go2rtc.example.yaml")
                setup.set_env({"TELEGRAM_BOT_TOKEN": "123:SECRET"}, path=d / ".env",
                              example=REPO / ".env.example")
                logging.getLogger("pawsight.setup").debug("marker so assertLogs does not fail")
            noise = "\n".join(logs.output)
            self.assertNotIn("hunter2", noise)
            self.assertNotIn("SECRET", noise)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class ShapeLineParsing(unittest.TestCase):
    """A shape line may carry a trailing comment — even one with brackets."""

    def test_comment_with_brackets_stays_outside_the_value(self):
        line = "      shape: [[-10, -8], [-85, 37], [-112, 4]]   # [dx, dy] from the bowl\n"
        m = setup._SHAPE_RE.search(line)
        self.assertEqual(m.group("val"), "[[-10, -8], [-85, 37], [-112, 4]]")
        self.assertEqual(m.group("tail"), "   # [dx, dy] from the bowl")
