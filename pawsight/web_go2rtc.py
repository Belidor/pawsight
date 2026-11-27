"""go2rtc proxy: the player, the stream WebSocket and API calls for setup.

Mixin for web.Dashboard. Moved out of web.py with no change in behaviour or URLs.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType, web

from . import setup

log = logging.getLogger("pawsight.web")

# go2rtc streams the dashboard may show at all.
STREAMS = frozenset({setup.STREAM_MAIN, setup.STREAM_SUB})


def is_webkit(user_agent: str) -> bool:
    """Safari and the desktop app's window (WKWebView), not Chromium.
    Chromium also says "AppleWebKit" in its User-Agent, hence the exclusions."""
    ua = user_agent or ""
    return "AppleWebKit" in ua and not any(t in ua for t in ("Chrome", "Chromium", "Edg/"))


def view_stream(src: str, user_agent: str, main_url: str) -> str:
    """Which go2rtc stream to play for a requested one.

    Xiaomi cameras over P2P switch the main stream between 2304x1296 and
    640x360 as the link allows (measured: both sizes within 15 s). Chromium's
    MSE handles that; WebKit's HEVC decoder keeps the first size and draws the
    smaller frames in a corner of a green picture. So WebKit gets the SD
    substream, which keeps one size, and Chromium keeps the full picture.
    """
    if src == setup.STREAM_MAIN and is_webkit(user_agent) and main_url.startswith("xiaomi://"):
        return setup.STREAM_SUB
    return src


class Go2rtcProxy:
    # ---- go2rtc proxying ----

    def _go2rtc_auth(self) -> dict:
        """go2rtc API login (setup.go2rtc_credentials). The file is re-read only
        when it changes: streams are opened often, and parsing YAML is not free."""
        try:
            key = setup.GO2RTC_FILE.stat().st_mtime_ns
        except OSError:
            key = None
        key = (str(setup.GO2RTC_FILE), key, os.getenv("GO2RTC_USER", ""))
        if getattr(self, "_auth_key", None) != key:
            creds = setup.go2rtc_credentials()
            # Build the header ourselves: aiohttp's auth= parameter is being deprecated.
            self._auth = ({"Authorization": "Basic " + base64.b64encode(
                f"{creds[0]}:{creds[1]}".encode()).decode()} if creds else {})
            self._auth_key = key
        return self._auth

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def player_js(self, req):
        """Serves the go2rtc player from our own origin.

        Module scripts are loaded with a CORS check, and go2rtc sends no CORS
        headers. So we serve them ourselves, and then the relative import inside
        video-stream.js also resolves to our origin.
        """
        name = req.match_info["name"]
        if name not in ("video-rtc.js", "video-stream.js"):
            raise web.HTTPNotFound()
        s = await self._session()
        try:
            async with s.get(f"{self.go2rtc}/{name}", headers=self._go2rtc_auth(),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                status, body = r.status, await r.read()
        except Exception as e:
            log.warning("could not fetch %s from go2rtc: %s", name, e)
            raise web.HTTPBadGateway(text="go2rtc unavailable")
        if status != 200:
            # 401 means go2rtc has a password and ours did not match: the error
            # page must not be handed to the browser as a script, let alone cached.
            log.warning("go2rtc answered %s for %s%s", status, name,
                        " — check api.username/password in go2rtc.yaml "
                        "(in docker: GO2RTC_USER/GO2RTC_PASS in .env)" if status == 401 else "")
            raise web.HTTPBadGateway(text=f"go2rtc answered {status}")
        return web.Response(body=body, content_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=300"})

    async def stream_ws(self, req):
        """Two-way WebSocket relay to go2rtc.

        The guard middleware shuts this route to foreign tabs: the same-origin
        policy does not apply to WebSocket, and without an Origin check any
        open page could watch the live video.
        """
        src = req.query.get("src", setup.STREAM_MAIN)
        # Our own streams only: src goes into the go2rtc URL, and without a check
        # anything could be appended to the query through it (`&` etc.).
        if src not in STREAMS:
            raise web.HTTPBadRequest(text="unknown stream")
        src = view_stream(src, req.headers.get("User-Agent", ""), setup.read_stream())
        client = web.WebSocketResponse(protocols=("binary",), max_msg_size=0)
        await client.prepare(req)

        s = await self._session()
        url = f"{self.go2rtc.replace('http', 'ws', 1)}/api/ws?src={quote(src, safe='')}"
        try:
            async with s.ws_connect(url, max_msg_size=0, headers=self._go2rtc_auth()) as up:
                async def pump(a, b):
                    async for m in a:
                        if m.type == WSMsgType.TEXT:
                            await b.send_str(m.data)
                        elif m.type == WSMsgType.BINARY:
                            await b.send_bytes(m.data)
                        else:
                            break

                await asyncio.wait(
                    [asyncio.create_task(pump(client, up)),
                     asyncio.create_task(pump(up, client))],
                    return_when=asyncio.FIRST_COMPLETED,
                )
        except Exception as e:
            log.warning("go2rtc stream dropped: %s", e)
        finally:
            if not client.closed:
                await client.close()
        return client

    async def _go2rtc_call(self, method: str, path: str, params: dict | None = None,
                           timeout: float = 10) -> tuple[int, str]:
        """Request to go2rtc. Returns (status, text) with secrets scrubbed.

        A src with a password travels in the query string, just as start.sh
        does it. That takes the password no further than go2rtc.yaml already
        does: go2rtc stores it in its file anyway. The go2rtc response, however,
        is scrubbed: it may quote the source in full.
        """
        s = await self._session()
        try:
            async with s.request(method, f"{self.go2rtc}{path}", params=params,
                                 headers=await self._exec(self._go2rtc_auth),
                                 timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                return r.status, setup.mask_secrets(await r.text())
        except Exception as e:
            return 0, f"go2rtc did not answer at {self.go2rtc} ({type(e).__name__})"
