"""First-run setup: camera address, Telegram, water bowl confirmation.

Why a separate module rather than handlers right in web.py: there is no
aiohttp here and no network beyond the RTSP probe, so all of it is covered by
plain tests without a running server, just like the fan-out in notify.py.

Secrets travel in ONE direction only. The user types the camera password and
the bot token; they go to go2rtc/go2rtc.yaml and .env (both in .gitignore,
both 600) and never come back: not in the page response, not in the log, not
in the camera activity feed (it is written to the DB and broadcast to every
open tab, web.py:61). That is why there is not a single log call here with a
URL or a token, and only flags and a masked address ever leave this module.

The address check uses its own RTSP client on bare sockets rather than
cv2.VideoCapture from selftest: the brain runs without torch and cv2
(Dockerfile), and `import cv2` here would take down the whole dashboard. As a
bonus the bare client answers not just "doesn't work" but what exactly is
wrong: port closed, wrong password, wrong path.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import socket
import ssl
import stat
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import yaml

log = logging.getLogger("pawsight.setup")

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
GO2RTC_FILE = ROOT / "go2rtc" / "go2rtc.yaml"
GO2RTC_EXAMPLE = ROOT / "go2rtc" / "go2rtc.example.yaml"
ZONES_FILE = ROOT / "zones.yaml"

STREAM_MAIN = "dog_cam"
STREAM_SUB = "dog_cam_sd"
UA = "pawsight-setup"

# Maximum camera address length. The limit is not about overflow but about
# pasting something unrelated into the yaml and into go2rtc's query string.
URL_MAX = 512


class SetupError(Exception):
    """A refusal that can be shown to the user verbatim. Contains no secrets."""


# ------------------------------------------------------------------- masking

def mask_url(url: str) -> str:
    """`rtsp://user:pass@1.2.3.4:554/s1` -> `rtsp://…@1.2.3.4:554/s1`.

    The login is hidden just like the password: on cameras it is almost always
    admin, but on some models it is a second secret, and we cannot tell which
    from here.

    netloc parsing cannot be trusted here, and this is not paranoia: for
    `ffmpeg:rtsp://…` netloc is empty, and a password containing `/` cuts
    netloc short at that point. In both cases urlsplit "does not see" the
    credentials and a naive mask would return them in full. So we rebuild the
    address from the parsed parts, and if parsing fails we hide everything.
    The query is always dropped: some cameras take the login and password
    right there (`?usr=&pwd=`).
    """
    url = (url or "").strip()
    scheme, sep, rest = url.partition(":")
    if not sep or not rest.startswith("//"):
        return "…"                       # ffmpeg:…, exec:… — nothing to parse
    try:
        u = urlsplit(url)
        host, port = u.hostname, u.port
    except ValueError:
        return f"{scheme}://…"
    if not host:
        return f"{scheme}://…"
    who = "…@" if "@" in u.netloc else ""
    if ":" in host:                      # return IPv6 literals in brackets
        host = f"[{host}]"
    tail = (u.path or "") + ("?…" if u.query else "")
    return f"{scheme}://{who}{host}{':' + str(port) if port else ''}{tail}"


# ------------------------------------------------------------ camera address

def build_rtsp_url(host: str, port: str | int | None, path: str,
                   user: str = "", password: str = "") -> str:
    """Builds an RTSP URL from its parts.

    The login and password must be percent-encoded: camera passwords regularly
    contain @ : / ?, and without encoding urlsplit sees a different host. The
    failure would look like "camera not responding" with a perfectly correct
    password.
    """
    host = (host or "").strip()
    if not host:
        raise SetupError("camera address is empty")
    if "://" in host:
        raise SetupError("the address field contains a full URL. Paste it into the \"full address\" field")
    try:
        port_n = int(str(port).strip() or 554)
    except ValueError:
        raise SetupError("port must be a number")
    if not 1 <= port_n <= 65535:
        raise SetupError("port is out of range 1-65535")

    path = (path or "").strip()
    if path and not path.startswith("/"):
        path = "/" + path
    userinfo = ""
    if user:
        userinfo = quote(user, safe="")
        if password:
            userinfo += ":" + quote(password, safe="")
        userinfo += "@"
    if host.count(":") == 1 and not host.startswith("["):
        # `192.168.1.50:554` in the address field is a common slip (the port
        # has its own field right next to it). Treating it as an IPv6 literal
        # would build `rtsp://…@[192.168.1.50:554]:554/…` and fail to parse.
        raise SetupError("put the port in its own field and leave only the "
                         "host or IP in the address")
    if host.count(":") > 1 and not host.startswith("["):      # IPv6 literal
        host = f"[{host}]"
    return f"rtsp://{userinfo}{host}:{port_n}{path}"


def validate_rtsp_url(url: str) -> str:
    """Validates a pasted full address. Returns it stripped."""
    url = (url or "").strip()
    if not url:
        raise SetupError("address is empty")
    if len(url) > URL_MAX:
        raise SetupError(f"address is longer than {URL_MAX} characters — that can't be it")
    if any(c in url for c in "\r\n\t"):
        raise SetupError("address contains a newline or tab — something extra got copied")
    if " " in url:
        # go2rtc rejects such a source itself (streams: source with spaces
        # may be insecure), so we refuse earlier and more clearly.
        raise SetupError("address contains a space — go2rtc will not accept such a source")
    try:
        u = urlsplit(url)
        u.port          # a property, not a field: the port is parsed here
    except ValueError:
        # This catches "port is not a number", "/ or : in the password" (then
        # netloc is cut short at the password) and a broken IPv6 literal. The
        # exception text contains part of the password, so it never leaves.
        raise SetupError(
            "address cannot be parsed: the port is not a number, or the "
            "password contains \"/\", \":\" or \"#\". Fill in the login and "
            "password as separate fields and I will encode them myself"
        )
    if u.scheme not in ("rtsp", "rtsps"):
        raise SetupError(f"scheme \"{u.scheme or '—'}\" is not supported: use rtsp:// or rtsps://")
    if not u.hostname:
        raise SetupError("cannot find the camera host in the address")
    if (u.netloc.count("@") > 1):
        raise SetupError(
            "address has more than one @ — probably the password contains @. "
            "Fill in the login and password as separate fields and I will encode them myself"
        )
    return url


# -------------------------------------------------------------- camera probe

_PARAMS = re.compile(r'(\w+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^,\s]+))')


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    """`Digest realm="Cam", nonce="ab,cd"` -> ('digest', {...}).

    Uses a regex rather than split(','): nonces may contain commas.
    """
    scheme, _, rest = header.partition(" ")
    params = {}
    for m in _PARAMS.finditer(rest):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return scheme.lower(), params


def _h(algo: str, *parts: str) -> str:
    data = ":".join(parts).encode()
    return (hashlib.sha256(data) if algo.startswith("sha-256") else hashlib.md5(data)).hexdigest()


def digest_header(user: str, password: str, method: str, uri: str,
                  params: dict[str, str], nc_int: int = 1,
                  cnonce: str | None = None) -> str:
    """Digest Authorization, in two flavours at once.

    Cameras split roughly evenly: some require qop=auth (RFC 7616), others
    answer in the old RFC 2069 style without qop. Both must be supported:
    implementing only one, the tool would say "wrong password" where the
    password is correct.
    """
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    algo = (params.get("algorithm") or "MD5").lower()

    ha1 = _h(algo, user, realm, password)
    if algo.endswith("-sess"):
        cnonce = cnonce or os.urandom(8).hex()
        ha1 = _h(algo, ha1, nonce, cnonce)
    ha2 = _h(algo, method, uri)

    fields = [f'username="{user}"', f'realm="{realm}"', f'nonce="{nonce}"', f'uri="{uri}"']
    if "auth" in [q.strip() for q in params.get("qop", "").split(",") if q.strip()]:
        cnonce = cnonce or os.urandom(8).hex()
        nc = f"{nc_int:08x}"
        response = _h(algo, ha1, nonce, nc, cnonce, "auth", ha2)
        fields += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    else:
        response = _h(algo, ha1, nonce, ha2)
    fields.append(f'response="{response}"')
    if params.get("opaque"):
        fields.append(f'opaque="{params["opaque"]}"')
    if params.get("algorithm"):
        fields.insert(0, f'algorithm={params["algorithm"]}')
    return "Digest " + ", ".join(fields)


class _NotRtsp(Exception):
    pass


async def _read_response(rd: asyncio.StreamReader) -> tuple[int, str, dict[str, list[str]], bytes]:
    head = await rd.readuntil(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    m = re.match(r"RTSP/\d\.\d\s+(\d{3})\s*(.*)", lines[0])
    if not m:
        raise _NotRtsp(lines[0][:60])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if line:
            k, _, v = line.partition(":")
            headers.setdefault(k.strip().lower(), []).append(v.strip())
    n = int((headers.get("content-length") or ["0"])[0] or 0)
    body = await rd.readexactly(n) if n else b""
    return int(m.group(1)), m.group(2), headers, body


async def probe_rtsp(url: str, timeout: float = 6.0) -> dict:
    """Knocks on the camera and names the reason for a refusal, not just the fact.

    OPTIONS, then DESCRIBE; on 401, exactly one retry with Authorization.
    The first 401 is normal (that is how the camera sends its challenge); only
    the second one means "wrong login or password".

    Returns {"ok", "code", "msg"}, with no URL and no password inside msg.
    """
    try:
        url = validate_rtsp_url(url)
    except SetupError as e:
        return {"ok": False, "code": "bad_url", "msg": str(e)}

    u = urlsplit(url)
    host, port = u.hostname or "", u.port or 554
    user, password = unquote(u.username or ""), unquote(u.password or "")
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    base = f"rtsp://{host}:{port}"
    req_uri = base + path
    tls = None
    if u.scheme == "rtsps":
        # Camera certificates are almost always self-signed: there is no one
        # and nothing to verify them against, so verification is off, and the
        # user is told so in the response rather than left unaware.
        tls = ssl._create_unverified_context()

    try:
        rd, wr = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=tls), timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "code": "timeout",
                "msg": f"{host}:{port} did not answer within {timeout:.0f} s — address, network or firewall"}
    except socket.gaierror:
        return {"ok": False, "code": "dns", "msg": f"name {host} does not resolve"}
    except ConnectionRefusedError:
        return {"ok": False, "code": "refused",
                "msg": f"port {port} on {host} is closed — RTSP is off or the port is different"}
    except ssl.SSLError as e:
        return {"ok": False, "code": "tls", "msg": f"TLS handshake failed: {e.reason or 'error'}"}
    except OSError as e:
        return {"ok": False, "code": "net", "msg": f"network: {e.strerror or type(e).__name__}"}

    cseq = 0
    auth_header: str | None = None
    scheme_used = ""

    async def send(method: str, uri: str, extra: dict[str, str] | None = None):
        nonlocal cseq
        cseq += 1
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {cseq}", f"User-Agent: {UA}"]
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        if auth_header:
            lines.append(f"Authorization: {auth_header}")
        wr.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await wr.drain()
        return await asyncio.wait_for(_read_response(rd), timeout)

    try:
        # OPTIONS is not sent for its answer: some cameras reply 401 to a
        # DESCRIBE that comes without a prior exchange and without a
        # User-Agent (a known issue; go2rtc carries the same workaround).
        await send("OPTIONS", base + "/")

        for attempt in (1, 2):
            code, reason, headers, body = await send(
                "DESCRIBE", req_uri, {"Accept": "application/sdp"})
            if code != 401:
                break
            if not user:
                return {"ok": False, "code": "auth_required",
                        "msg": "camera requires a login and password"}
            if attempt == 2:
                return {"ok": False, "code": "auth_failed",
                        "msg": "camera rejected the login or password (401)"}
            pick = None
            for h in headers.get("www-authenticate") or []:
                s, p = _parse_challenge(h)
                if s == "digest":
                    pick = (s, p)
                    break
                if s == "basic" and pick is None:
                    pick = (s, p)
            if pick is None:
                return {"ok": False, "code": "auth_unknown",
                        "msg": "camera asks for an unfamiliar kind of authentication"}
            scheme_used, params = pick
            if scheme_used == "digest":
                auth_header = digest_header(user, password, "DESCRIBE", req_uri, params)
            else:
                auth_header = "Basic " + base64.b64encode(
                    f"{user}:{password}".encode()).decode()

        if code == 200:
            sdp = body.decode("utf-8", "replace")
            video = [ln for ln in sdp.splitlines() if ln.startswith("m=video")]
            note = " (certificate not verified)" if tls else ""
            if not video:
                return {"ok": False, "code": "no_video",
                        "msg": "camera answered, but the stream has no video track"}
            return {"ok": True, "code": "ok", "msg": "stream found" + note,
                    "auth": scheme_used or "no password"}
        if code == 404:
            return {"ok": False, "code": "not_found",
                    "msg": f"path {u.path or '/'} not found on the camera (404) — check the stream path"}
        if code == 403:
            return {"ok": False, "code": "forbidden", "msg": "camera answered 403 — access denied"}
        if code in (455, 501):
            return {"ok": False, "code": "method",
                    "msg": f"camera does not accept DESCRIBE ({code}) — not an RTSP service?"}
        return {"ok": False, "code": "rtsp_error", "msg": f"camera answered {code} {reason}"}
    except asyncio.TimeoutError:
        return {"ok": False, "code": "mute",
                "msg": "port is open but the camera is silent — is this RTSP?"}
    except _NotRtsp as e:
        return {"ok": False, "code": "not_rtsp", "msg": f"not RTSP on this port: \"{e}\""}
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionResetError):
        return {"ok": False, "code": "reset", "msg": "camera dropped the connection"}
    except OSError as e:
        return {"ok": False, "code": "net", "msg": f"network: {e.strerror or type(e).__name__}"}
    finally:
        wr.close()
        try:
            await wr.wait_closed()
        except Exception:
            pass


# --------------------------------------------------------------- file writes

def make_private(path: Path) -> int:
    """Brings an existing file to mode 600 and returns the previous mode.

    Needed because go2rtc.yaml is not written only by us: install.sh copies
    the template (644 by umask), and a running go2rtc edits the file itself.
    """
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return mode


def _write_private(path: Path, text: str) -> None:
    """Atomic write with mode 600.

    Via a temp file NEXT TO the target (os.replace on the same filesystem);
    otherwise a crash mid-write leaves a truncated .env or go2rtc.yaml. The
    temp file is created with mode 600 from the start: there is no reason to
    put the camera password into a world-readable tmp even for an instant. The
    `.part` suffix is covered by .gitignore, so if the process is killed
    exactly between the write and the swap, the leftover with the password
    will not get committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".part")
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


_SAFE_ENV = re.compile(r"^[A-Za-z0-9_.,:/@=+-]*$")


def _env_value(value: str) -> str:
    """Prepares a value for .env with an eye to how it will be read back.

    python-dotenv cuts everything after ' #' in an UNquoted value and eats
    trailing spaces, while double quotes turn \\n into a newline. Single
    quotes do neither, so anything that is not a plain word goes into them.
    """
    if "${" in value:
        # dotenv would substitute an environment variable here (empty), in any
        # quotes and with no way to escape it. The password would silently change.
        raise SetupError("the value contains ${ — dotenv would substitute an "
                         "environment variable for it. Such a value cannot be saved")
    if _SAFE_ENV.match(value):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def set_env(updates: dict[str, str], path: Path | None = None,
            example: Path | None = None) -> list[str]:
    """Edits .env line by line, preserving comments and key order.

    Not sed as in install.sh: that one is macOS-only and breaks on a value
    containing `|`. An empty value here means "leave as is": otherwise
    reopening the wizard with an empty form would wipe an already configured
    token.

    Returns the list of changed keys.
    """
    path = path or ENV_FILE
    example = example or ENV_EXAMPLE
    updates = {k: v for k, v in updates.items() if v != ""}
    if not updates:
        return []
    if path.exists():
        text = path.read_text(encoding="utf-8")
    elif example.exists():
        text = example.read_text(encoding="utf-8")
    else:
        text = ""

    lines = text.splitlines()
    changed: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if not m or m.group(2) not in updates:
            continue
        key = m.group(2)
        seen.add(key)
        new = f"{key}={_env_value(updates[key])}"
        if lines[i] != new:
            lines[i] = new
            changed.append(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={_env_value(value)}")
            changed.append(key)

    _write_private(path, "\n".join(lines) + "\n")
    return changed


def env_flags(path: Path | None = None) -> dict:
    """What is filled in .env — ONLY flags and counters, no values."""
    from dotenv import dotenv_values

    from .notify import parse_chats

    path = path or ENV_FILE
    if not path.exists():
        return {"exists": False, "telegram_token": False, "telegram_chats": 0,
                "camera_ip": False, "camera_token": False}
    try:
        vals = dotenv_values(path)
    except Exception:
        return {"exists": True, "unreadable": True, "telegram_token": False,
                "telegram_chats": 0, "camera_ip": False, "camera_token": False}
    return {
        "exists": True,
        "telegram_token": bool(vals.get("TELEGRAM_BOT_TOKEN")),
        "telegram_chats": len(parse_chats(vals.get("TELEGRAM_CHAT_ID") or "")),
        "camera_ip": bool(vals.get("CAMERA_IP")),
        "camera_token": bool(vals.get("CAMERA_TOKEN")),
    }


# ------------------------------------------------------------------- go2rtc

def _stream_value(raw) -> str:
    """streams.dog_cam can be a string, a list or None — normalise to a string."""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    return (raw or "").strip() if isinstance(raw, str) else ""


def read_stream(name: str = STREAM_MAIN, path: Path | None = None) -> str:
    """What go2rtc.yaml holds for the stream. Empty means not configured."""
    path = path or GO2RTC_FILE
    if not path.exists():
        return ""
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(cfg, dict):
        return ""
    streams = cfg.get("streams")
    if not isinstance(streams, dict):
        # `streams: dog_cam` as a string means the file is broken, but that is
        # no reason to crash the dashboard: "not configured" is more honest
        # here than an exception.
        return ""
    return _stream_value(streams.get(name))


def go2rtc_credentials(path: Path | None = None) -> tuple[str, str] | None:
    """go2rtc API login and password: from go2rtc.yaml if it is readable,
    otherwise from the environment (GO2RTC_USER/GO2RTC_PASS — docker mode,
    where the file is not mounted into the container). None means no auth
    (older installs)."""
    path = path or GO2RTC_FILE
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = None
    if isinstance(cfg, dict):
        api = cfg.get("api")
        if isinstance(api, dict) and str(api.get("username") or ""):
            return str(api["username"]), str(api.get("password") or "")
        # The file is readable but has no login, so go2rtc runs without a
        # password. We do not mix in the environment here: it may be left over
        # from a previous install and would only break every request.
        return None
    user = os.getenv("GO2RTC_USER", "")
    return (user, os.getenv("GO2RTC_PASS", "")) if user else None


def camera_configured(path: Path | None = None) -> bool:
    """First run is detected here, not from .env.

    .env does not store the camera address at all: PAWSIGHT_RTSP points at
    go2rtc (rtsp://127.0.0.1:8554/dog_cam) and is always set, even when there
    is no camera. The only sign that "a camera is configured" is a non-empty
    streams.dog_cam, and the go2rtc.example.yaml template ships it as an
    empty string.
    """
    return bool(read_stream(STREAM_MAIN, path or GO2RTC_FILE))


def xiaomi_logged_in(path: Path | None = None) -> bool:
    """Whether go2rtc.yaml has a xiaomi section. The value (token) is not read."""
    path = path or GO2RTC_FILE
    if not path.exists():
        return False
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return False
    return bool(isinstance(cfg, dict) and cfg.get("xiaomi"))


def write_go2rtc_stream(url: str, name: str = STREAM_MAIN, path: Path | None = None,
                        example: Path | None = None) -> None:
    """Writes the stream into go2rtc.yaml. The camera password lives only here.

    safe_load + safe_dump, the same path install.sh already takes: it loses
    the template's comments but keeps every key, including the `xiaomi`
    section with the Mi account token. go2rtc maintains this file itself too,
    so writing it by hand only makes sense when go2rtc is not responding (see
    web.py).
    """
    path = path or GO2RTC_FILE
    example = example or GO2RTC_EXAMPLE
    if path.exists():
        raw = path.read_text(encoding="utf-8")
    elif example.exists():
        raw = example.read_text(encoding="utf-8")
    else:
        raw = ""
    try:
        cfg = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        raise SetupError("go2rtc.yaml does not parse as yaml — fix it by hand")
    if not isinstance(cfg, dict):
        raise SetupError("go2rtc.yaml has an unexpected structure — fix it by hand")
    cfg.setdefault("streams", {})
    if not isinstance(cfg["streams"], dict):
        raise SetupError("the streams section in go2rtc.yaml is not a mapping — fix it by hand")
    cfg["streams"][name] = url
    _write_private(path, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))


# --------------------------------------------------------------------- zones

_SELECT_RE = re.compile(r"^(?P<head>\s*select:\s*)(?P<val>left|right)(?P<tail>[^\n]*)$", re.M)
# A zone shape is a single-line vertex list: that way it can be swapped as text.
_SHAPE_RE = re.compile(r"^(?P<head>[ \t]*shape:[ \t]*)(?P<val>\[[^#\n]*\])(?P<tail>[^\n]*)$", re.M)
_HEADER_RE = re.compile(r"^# WATER BOWL = .*$\n(^#.*$\n)*", re.M)
_CONFIRM_RE = re.compile(r"^water_bowl_confirmed:.*$", re.M)


def _zone_block(text: str, name: str) -> tuple[int, int]:
    """Bounds of a top-level zone block inside `zones:`."""
    m = re.search(rf"^  {name}:[ \t]*$", text, re.M)
    if not m:
        raise SetupError(f"zones.yaml has no zone {name} — the file was edited by hand; "
                         "swap select: left/right yourself")
    nxt = re.search(r"^  [a-z_]+:[ \t]*$", text[m.end():], re.M)
    return m.end(), m.end() + (nxt.start() if nxt else len(text) - m.end())


def water_bowl(path: Path | None = None) -> dict:
    """Which bowl is recorded as the water bowl, and whether the user confirmed it here."""
    path = path or ZONES_FILE
    if not path.exists():
        return {"side": None, "confirmed": None}
    text = path.read_text(encoding="utf-8")
    try:
        s, e = _zone_block(text, "water")
    except SetupError:
        return {"side": None, "confirmed": None}
    m = _SELECT_RE.search(text[s:e])
    c = _CONFIRM_RE.search(text)
    return {
        "side": m.group("val") if m else None,
        "confirmed": c.group(0).split(":", 1)[1].strip().strip('"') if c else None,
    }


def set_water_bowl(side: str, path: Path | None = None, today: str | None = None) -> dict:
    """Swaps select between the water and food zones — only on the user's choice.

    The TEXT is edited, not the parsed yaml: safe_dump would wipe the
    explanations this file exists for (why zones are tied to anchors, why the
    geometry here is only a second opinion). A file without its reasons looks
    just as correct and lies silently, exactly what section 6 of AGENTS.md is
    written against.

    The bowls zone is left alone: the visit trigger itself depends on it.
    """
    path = path or ZONES_FILE
    if side not in ("left", "right"):
        raise SetupError("side can only be left or right")
    if not path.exists():
        raise SetupError("zones.yaml not found")
    today = today or time.strftime("%Y-%m-%d")
    text = path.read_text(encoding="utf-8")

    other = "right" if side == "left" else "left"
    out = text
    s, e = _zone_block(text, "water")
    m = _SELECT_RE.search(text[s:e])
    was = m.group("val") if m else None
    for zone, value, comment in (
        ("water", side, f"# water — {side} (confirmed by owner)"),
        ("food", other, f"# food — {other}"),
    ):
        s, e = _zone_block(out, zone)
        block = out[s:e]
        if len(_SELECT_RE.findall(block)) != 1:
            raise SetupError(
                f"zone {zone} has more than one select line — the file was edited by hand. "
                "Swap select: left and select: right yourself"
            )
        # Keep the comment column at character 27, as in the file now.
        block = _SELECT_RE.sub(lambda m: f"{m.group('head')}{value}".ljust(27) + comment, block)
        out = out[:s] + block + out[e:]

    # The floor shape belongs to the bowl's PLACE, not its contents: if the
    # water bowl is now the right one, its floor strip is the one that was
    # drawn for the right bowl. A shape on only one zone without its pair is a
    # half-done manual edit; swapping select and leaving the shape in place
    # would make the geometry lie, so we refuse.
    shapes: dict[str, str | None] = {}
    for zone in ("water", "food"):
        s, e = _zone_block(out, zone)
        found = _SHAPE_RE.findall(out[s:e])
        if len(found) > 1:
            raise SetupError(f"zone {zone} has more than one shape line — the file was edited by hand")
        shapes[zone] = found[0][1] if found else None
    if (shapes["water"] is None) != (shapes["food"] is None):
        raise SetupError("shape is set on only one of the water/food zones — the file "
                         "was edited by hand. Set it on both or remove it from both, "
                         "otherwise swapping the bowls would make the geometry lie")
    if shapes["water"] is not None and was != side:
        for zone, value in (("water", shapes["food"]), ("food", shapes["water"])):
            s, e = _zone_block(out, zone)
            block = _SHAPE_RE.sub(lambda m, v=value: f"{m.group('head')}{v}{m.group('tail')}",
                                  out[s:e], count=1)
            out = out[:s] + block + out[e:]

    _check_swap_by_yaml(text, out, side_changed=was != side)

    header = (f"# WATER BOWL = {side.upper()}. Confirmed by the owner on the setup page\n"
              f"# {today}.\n")
    if _HEADER_RE.search(out):
        # The lines about the author's reference frame describe SOMEONE ELSE'S
        # room: leaving them next to the new confirmation would be a lie.
        out = _HEADER_RE.sub(header, out, count=1)
    stamp = f'water_bowl_confirmed: "{today}"'
    if _CONFIRM_RE.search(out):
        out = _CONFIRM_RE.sub(stamp, out, count=1)
    else:
        out = re.sub(r"^zones:[ \t]*$",
                     "# Date the owner confirmed the water bowl choice on the\n"
                     "# setup page. Without this stamp the wizard cannot tell a\n"
                     "# deliberate choice from a value left over from the author.\n"
                     f"{stamp}\n\nzones:", out, count=1, flags=re.M)

    if out == text:
        return {"changed": False, "side": side}
    _write_atomic_public(path, out)
    return {"changed": True, "side": side}


def _check_swap_by_yaml(before: str, after: str, *, side_changed: bool) -> None:
    """The text edit is checked against the parsed YAML before writing to disk.

    The regex only sees a single-line `shape: [...]`. A shape written as a
    block list or wrapped across lines is read by Config just fine, but here
    it is either not found (select swapped, shapes left with the other bowls)
    or found halfway (two chimeras made of strip halves). Both substitutions
    look like a confirmed file. So the expectation is taken from parsing the
    file BEFORE the edit (are there shapes at all), and the result is checked:
    after the edit water and food have exactly the shapes they should, swapped
    on a side change and unchanged otherwise. If not, we refuse and the file
    is left untouched.
    """
    try:
        b = (yaml.safe_load(before) or {}).get("zones") or {}
        a = (yaml.safe_load(after) or {}).get("zones") or {}
    except yaml.YAMLError as e:
        raise SetupError(f"zones.yaml no longer parses after the swap: {e}") from e
    shape = lambda z, n: ((z.get(n) or {}).get("anchor") or {}).get("shape")
    bw, bf = shape(b, "water"), shape(b, "food")
    if (bw is None) != (bf is None):
        raise SetupError("shape is set on only one of the water/food zones — the file "
                         "was edited by hand. Set it on both or remove it from both, "
                         "otherwise swapping the bowls would make the geometry lie")
    want = {"water": bf if side_changed else bw, "food": bw if side_changed else bf}
    got = {n: shape(a, n) for n in want}
    if got != want:
        raise SetupError("could not swap the shapes as text — the file was edited by hand. "
                         "Write shape for the water and food zones on one line, like "
                         "shape: [[dx, dy], ...], and try again")


def _write_atomic_public(path: Path, text: str) -> None:
    """zones.yaml holds no secrets and lives in git — permissions are not tightened."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# -------------------------------------------------------------------- status

def writable() -> dict:
    """Whether THIS process can edit the configuration.

    In the docker stack it cannot: only storage and zones.yaml (the latter
    read-only) are mounted into the container, and .env arrives via env_file
    when the container is created. Silently writing /app/.env inside the
    container and reporting "saved" is the worst thing we could do here.
    """
    def ok(p: Path) -> bool:
        target = p if p.exists() else p.parent
        return os.access(target, os.W_OK)

    return {
        "env": ok(ENV_FILE),
        "go2rtc": ok(GO2RTC_FILE),
        "zones": ok(ZONES_FILE),
        "in_container": Path("/.dockerenv").exists(),
    }


_CREDS_IN_URL = re.compile(r"(\w+://)[^\s/@]*:[^\s/@]*@")


def mask_secrets(text: str) -> str:
    """Strips `scheme://login:password@` from foreign text.

    Applied to everything that comes from go2rtc and goes to the page: we
    control our own messages, but another program's answer about our own
    source may well contain it in full.
    """
    return _CREDS_IN_URL.sub(r"\1…@", text or "")
