"""Dashboard guards: Host, Origin, the X-Pawsight header and input parsing.

Moved out of web.py with no change in behaviour (code review, 2026-11-21).
"""
from __future__ import annotations

from aiohttp import web

# Header our own dashboard must send on every state-changing request.
# WHY. The server listens on 127.0.0.1, but loopback is reachable from ANY page
# open in the owner's browser — the very reasoning for proxying the go2rtc API
# instead of exposing it (see the module header). Yet our own POST routes were
# wide open: aiohttp does not check content-type in .json(), so a fetch with
# text/plain goes out as a "simple" request without a preflight OPTIONS. A
# non-standard header triggers a preflight, and the server will refuse to allow
# it for a foreign page.
GUARD_HEADER = "X-Pawsight"
SAFE_ORIGINS = {"http://127.0.0.1", "http://localhost", "http://[::1]"}
SAFE_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _host_ok(host: str) -> bool:
    """Host allowlist — protection against DNS rebinding: otherwise a foreign
    domain resolving to 127.0.0.1 could read logs and status."""
    return (host or "").rsplit(":", 1)[0].strip("[]") in {h.strip("[]") for h in SAFE_HOSTS}


def _same_origin(req: web.Request) -> None:
    """The request came from a page of this same server, not a foreign tab."""
    origin = req.headers.get("Origin")
    if origin and origin.rsplit(":", 1)[0] not in SAFE_ORIGINS and origin not in SAFE_ORIGINS:
        raise web.HTTPForbidden(text="foreign Origin")
    site = req.headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        raise web.HTTPForbidden(text="request is not from the same page")


@web.middleware
async def guard(req: web.Request, handler):
    if req.method in ("GET", "HEAD", "OPTIONS"):
        if not _host_ok(req.headers.get("Host", "")):
            raise web.HTTPForbidden(text="foreign Host")
        # WebSocket is not subject to the same-origin policy AT ALL: a foreign
        # page opens ws://127.0.0.1:8099/api/stream and watches the live video,
        # and no CORS stops it. The X-Pawsight header cannot be set there
        # (WebSocket allows no custom headers), so Origin is what remains: the
        # browser always sends it and a page cannot forge it.
        if req.headers.get("Upgrade", "").lower() == "websocket":
            _same_origin(req)
        return await handler(req)
    if not _host_ok(req.headers.get("Host", "")):
        raise web.HTTPForbidden(text="foreign Host")
    if req.headers.get(GUARD_HEADER) != "1":
        raise web.HTTPForbidden(text=f"header {GUARD_HEADER}: 1 is required")
    _same_origin(req)
    return await handler(req)


def _int_arg(raw, default: int, name: str) -> int:
    """A number from the request: a malformed value is a 400 with a message, not a 500.

    An empty/missing value gives the default. bool is rejected: True from JSON
    would silently become 1."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise web.HTTPBadRequest(text=f"{name}: integer expected")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text=f"{name}: integer expected")


async def read_body(req) -> dict:
    """Request body. Malformed JSON is a 400 with a message, not a 500 with a traceback."""
    try:
        body = await req.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON object expected")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON object expected")
    return body
