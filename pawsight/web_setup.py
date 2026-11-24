"""First-run setup routes (/setup, /api/setup/*).

Mixin for web.Dashboard. Moved out of web.py with no change in behaviour or URLs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from aiohttp import web

from . import config as cfgmod
from . import setup

log = logging.getLogger("pawsight.web")
STATIC = Path(__file__).parent / "static"


class SetupRoutes:
    # ---- first-run setup ----

    async def setup_page(self, req):
        return web.FileResponse(STATIC / "setup.html")

    async def _setup_state(self) -> dict:
        """Measured facts and flags only: no secret values here."""
        main = await self._exec(setup.read_stream)
        sub = await self._exec(setup.read_stream, setup.STREAM_SUB)
        code, body = await self._go2rtc_call("GET", "/api/streams")
        live = []
        if code == 200:
            try:
                live = sorted(json.loads(body).keys())
            except Exception:
                live = []
        obs = self.latest or {}
        zones = obs.get("zones") or {}
        age = time.time() - obs["ts"] if obs.get("ts") else None
        return {
            "camera": {
                "configured": bool(main),
                "kind": "xiaomi" if main.startswith("xiaomi://") else ("rtsp" if main else ""),
                "masked": setup.mask_url(main) if main else "",
                "sub": setup.mask_url(sub) if sub else "",
            },
            "xiaomi": await self._exec(setup.xiaomi_logged_in),
            "telegram": await self._exec(setup.env_flags),
            "bowls": {
                **await self._exec(setup.water_bowl),
                "visible": bool(zones.get("water") and zones.get("food")),
                # Bowls ONLY: the observation also carries furniture anchors
                # (table, fridge, sofa — detector.py), and the page labels the
                # outermost boxes "LEFT" and "RIGHT". With a table left of the
                # bowls, the user would end up confirming the table.
                "anchors": [a["box"] for a in (obs.get("anchors") or [])
                            if a.get("cls") == "bowl"][:4],
                "size": obs.get("size") or [1280, 720],
            },
            "go2rtc": {"ok": code == 200, "url": self.go2rtc, "streams": live,
                       "error": "" if code == 200 else body},
            "detector": {"ok": age is not None and age < 15,
                         "age": round(age, 1) if age is not None else None},
            "writable": await self._exec(setup.writable),
                # One stack-mode resolution for the whole system. This used to
                # have its own os.getenv("PAWSIGHT_STACK", "native") with the
                # OPPOSITE default, and on an install without that key the setup
                # page said "brain in a container (PAWSIGHT_STACK=native)" —
                # a contradiction in a single sentence.
            "stack": cfgmod.STACK,
        }

    async def api_setup_state(self, req):
        return web.json_response(await self._setup_state())

    async def api_setup_probe(self, req):
        """Address check without writing anything: press it as often as you like."""
        try:
            url = self._url_from(await self._body(req))
        except setup.SetupError as e:
            return web.json_response({"ok": False, "msg": str(e)}, status=400)
        return web.json_response(await setup.probe_rtsp(url))

    @staticmethod
    def _url_from(body: dict, prefix: str = "") -> str:
        """Address from the form: either pasted whole or built from parts."""
        whole = (body.get(prefix + "url") or "").strip()
        if whole:
            return setup.validate_rtsp_url(whole)
        return setup.validate_rtsp_url(setup.build_rtsp_url(
            body.get(prefix + "host", ""), body.get(prefix + "port", "") or 554,
            body.get(prefix + "path", ""), body.get(prefix + "user", ""),
            body.get(prefix + "password", "")))

    async def _apply_stream(self, name: str, url: str) -> list[dict]:
        """Writes the stream into go2rtc: first to the live process, then to the file.

        The order matters. go2rtc's PUT edits go2rtc.yaml itself, and more
        carefully than we do: it inserts into the text and keeps comments. Our
        safe_dump loses comments, so it is only the fallback for when go2rtc
        is not running. Writing the file under a running go2rtc is doubly
        wrong: it will overwrite it on its next own edit.
        """
        steps = []
        code, body = await self._go2rtc_call(
            "PUT", "/api/streams", {"name": name, "src": url})
        if code == 200:
            steps.append({"ok": True, "text": f"go2rtc accepted stream {name} on the fly"})
        elif code == 0:
            steps.append({"ok": False, "text": body + " — writing to the file only"})
        else:
            steps.append({"ok": False, "text": f"go2rtc refused ({code}): {body.strip()}"})

        written = await self._exec(setup.read_stream, name)
        if written == url:
            steps.append({"ok": True, "text": f"stream {name} address is saved in go2rtc.yaml"})
        else:
            try:
                await self._exec(setup.write_go2rtc_stream, url, name)
                steps.append({"ok": True, "text": f"stream {name} address written to go2rtc.yaml"})
            except (setup.SetupError, OSError) as e:
                steps.append({"ok": False,
                              "text": f"go2rtc.yaml not written: {setup.mask_secrets(str(e))}"})
                return steps
        # Permissions are set AFTER the write and in both cases: install.sh may
        # have created the file as a copy of the template (644 by umask), and
        # with go2rtc running it is go2rtc that rewrites it — os.WriteFile
        # does not change permissions, but does not fix them either.
        try:
            was = await self._exec(setup.make_private, setup.GO2RTC_FILE)
            if was & 0o077:
                steps.append({"ok": True, "text": (
                    f"go2rtc.yaml permissions set to 600 (were {was:o}: it holds the camera password)")})
        except OSError as e:
            steps.append({"ok": False, "text": f"go2rtc.yaml permissions not set: {e.strerror}"})
        return steps

    async def api_setup_camera(self, req):
        body = await self._body(req)
        w = await self._exec(setup.writable)
        if not w["go2rtc"]:
            return web.json_response({"ok": False, "msg": (
                "this process cannot edit go2rtc.yaml — the brain runs in a "
                "container and the configuration lives on the host. Run the wizard there: "
                ".venv/bin/python -m pawsight setup")}, status=409)
        try:
            url = self._url_from(body)
            sub = self._url_from(body, "sub_") if (
                body.get("sub_url") or body.get("sub_path")) else ""
        except setup.SetupError as e:
            return web.json_response({"ok": False, "msg": str(e)}, status=400)

        was = await self._exec(setup.camera_configured)
        probe = await setup.probe_rtsp(url)
        # The second stream is probed SEPARATELY and always: go2rtc's PUT does
        # not dial the camera, so an unchecked substream would give a green
        # tick and a detector reading an address no frame will ever come from.
        # It would only show up a week later as empty statistics.
        sub_probe = await setup.probe_rtsp(sub) if sub else None
        if not body.get("force"):
            for what, r in (("camera", probe), ("second stream", sub_probe)):
                if r and not r["ok"]:
                    return web.json_response(
                        {"ok": False, "probe": r, "msg": f"{what}: {r['msg']}"}, status=400)

        steps = [{"ok": probe["ok"], "text": "camera: " + probe["msg"]}]
        steps += await self._apply_stream(setup.STREAM_MAIN, url)
        if sub:
            steps.append({"ok": sub_probe["ok"], "text": "second stream: " + sub_probe["msg"]})
            steps += await self._apply_stream(setup.STREAM_SUB, sub)

        # The detector is switched to the substream only if it actually
        # answered: pointing it at a dead address is exactly the silent
        # breakage the probe was written for.
        detect = setup.STREAM_SUB if (sub and sub_probe["ok"]) else setup.STREAM_MAIN
        if sub and not sub_probe["ok"]:
            steps.append({"ok": False, "text": (
                "detector left on the main stream: the second one did not answer")})
        # The camera address NEVER goes into .env: PAWSIGHT_* point at go2rtc.
        env = {"PAWSIGHT_RTSP": f"rtsp://127.0.0.1:8554/{setup.STREAM_MAIN}",
               "PAWSIGHT_DETECT_RTSP": f"rtsp://127.0.0.1:8554/{detect}"}
        if not url.startswith("xiaomi://"):
            # A plain camera has no pan/tilt: otherwise the detector would wait
            # in vain for a PTZ that does not exist (install.sh --rtsp does the same).
            env |= {"PAWSIGHT_TILT_HOLD": "0", "PAWSIGHT_FOLLOW_DOG": "0", "PAWSIGHT_SEARCH_SEC": "0"}
        try:
            changed = await self._exec(setup.set_env, env)
            if changed:
                steps.append({"ok": True, "text": ".env updated: " + ", ".join(changed)})
        except (setup.SetupError, OSError) as e:
            steps.append({"ok": False, "text": f".env not written: {e}"})

        log.info("camera address saved via the setup page (the address itself is not logged)")
        after = [{"ok": True, "text": (
            "restart the system to apply the values from .env: "
            "./stop.sh && ./start.sh")}]
        if was:
            after.append({"ok": True, "text": (
                "the camera was configured before: a restart is mandatory here — "
                "the detector and current viewers hold on to the old stream")})
        return web.json_response({"ok": True, "masked": setup.mask_url(url),
                                  "steps": steps + after})

    async def api_setup_telegram(self, req):
        from dotenv import dotenv_values

        from .notify import check

        body = await self._body(req)
        token = (body.get("token") or "").strip()
        chats = (body.get("chats") or "").strip()
        stored = await self._exec(lambda: dotenv_values(setup.ENV_FILE)
                                  if setup.ENV_FILE.exists() else {})
        # An empty field means "keep as is", not "erase": otherwise checking
        # the recipients would wipe an already configured token.
        res = await self._exec(check, token or (stored.get("TELEGRAM_BOT_TOKEN") or ""),
                               chats or (stored.get("TELEGRAM_CHAT_ID") or ""))
        saved = []
        if res["token_ok"]:
            w = await self._exec(setup.writable)
            if w["env"]:
                try:
                    saved = await self._exec(
                        setup.set_env, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chats})
                except (setup.SetupError, OSError) as e:
                    res["error"] = f".env not written: {e}"
            elif token or chats:
                res["error"] = ("the brain runs in a container and cannot see .env — "
                                "enter the values on the host")
        res["saved"] = saved
        return web.json_response(res)

    async def api_setup_bowls(self, req):
        side = (await self._body(req)).get("side")
        try:
            r = await self._exec(setup.set_water_bowl, side)
        except (setup.SetupError, OSError) as e:
            return web.json_response({"ok": False, "msg": str(e)}, status=400)
        log.info("water bowl confirmed by the owner: %s", side)
        return web.json_response({"ok": True, **r})

    async def api_setup_device_token(self, req):
        """DEVICE token for panning the Xiaomi camera.

        The script does not ask for a password: it takes the passToken go2rtc
        already saved when you logged in, and never prints the token itself.
        So it is fine to run it from here, but this page has no field for the
        Mi account password and never will — that login belongs to the go2rtc WebUI.
        """
        if not await self._exec(setup.xiaomi_logged_in):
            return web.json_response({"ok": False, "msg": (
                "go2rtc.yaml has no xiaomi section yet — first log in to the "
                "Mi account in the go2rtc WebUI")}, status=409)
        script = setup.ROOT / "scripts" / "get_device_token.py"
        if not script.exists() or not (await self._exec(setup.writable))["env"]:
            return web.json_response({"ok": False, "msg": (
                "this step is done on the host: .venv/bin/python "
                "scripts/get_device_token.py")}, status=409)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), cwd=str(setup.ROOT),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            return web.json_response({"ok": False, "msg": "the script did not answer within 90 s"},
                                     status=504)
        text = setup.mask_secrets(out.decode("utf-8", "replace"))
        return web.json_response({"ok": proc.returncode == 0, "log": text})

