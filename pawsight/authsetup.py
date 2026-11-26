"""Passwords for local services: the go2rtc API and the MQTT broker.

Why, if everything listens on 127.0.0.1? Loopback is reachable from ANY page
open in the owner's browser, and via DNS rebinding a foreign site could read
go2rtc's /api/config (the camera password and the Mi account token) and change
streams. And an anonymous broker lets any process on this machine send
commands to the detector (pan the camera, delete frames, start training) and
read camera frames.

Runs twice in the life of an install:
  * by install.sh — for a new install, right after copying the templates;
  * by the owner — once, to migrate an older install:
        .venv/bin/python -m pawsight.authsetup --go2rtc --mqtt

Secret values are NOT printed: neither on screen nor to the log. Running it
again changes nothing if a password is already set.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

import yaml

from . import setup

G2R_USER = "pawsight"
MQTT_USER = "pawsight"          # same as in mosquitto/acl
MOSQ_DIR = setup.ROOT / "mosquitto"


def _new_password() -> str:
    # token_urlsafe: [A-Za-z0-9_-] — safe both quoted in YAML and in .env.
    return secrets.token_urlsafe(18)


def enable_go2rtc_auth(path: Path | None = None, env: Path | None = None,
                       password: str | None = None) -> bool:
    """Write api.username/password/local_auth into go2rtc.yaml and the same
    login and password into .env (GO2RTC_USER/GO2RTC_PASS — for the brain in a container).

    The edit is textual, not safe_dump: the file holds the owner's comments
    and the xiaomi section that go2rtc writes itself. The result is checked
    against the original parse: everything except the api section must stay the same.

    Returns True if anything changed.
    """
    path = path or setup.GO2RTC_FILE
    text = path.read_text(encoding="utf-8")
    before = yaml.safe_load(text) or {}
    if not isinstance(before, dict):
        raise ValueError(f"{path}: not a YAML mapping")
    api = before.get("api") if isinstance(before.get("api"), dict) else {}
    if str(api.get("username") or ""):
        return False

    pw = password or _new_password()
    auth = [f'  username: "{G2R_USER}"', f'  password: "{pw}"', "  local_auth: true"]
    lines = text.splitlines()
    head = next((i for i, ln in enumerate(lines) if re.match(r"^api:\s*(#.*)?$", ln)), None)
    if head is None:
        # Without an api section go2rtc would listen on 0.0.0.0:1984 — set listen explicitly.
        lines = ['api:', '  listen: "127.0.0.1:1984"', *auth, ""] + lines
    else:
        end = head + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            end += 1
        block = [ln for ln in lines[head + 1:end]
                 if not re.match(r"^\s+(username|password|local_auth)\s*:", ln)]
        # the block's trailing empty lines go after our keys
        tail = []
        while block and not block[-1].strip():
            tail.insert(0, block.pop())
        lines = lines[:head + 1] + block + auth + tail + lines[end:]
    new = "\n".join(lines) + ("\n" if text.endswith("\n") or head is None else "")

    after = yaml.safe_load(new) or {}
    got = after.get("api") or {}
    if (got.get("username"), got.get("password"), got.get("local_auth")) != (G2R_USER, pw, True):
        raise RuntimeError("go2rtc.yaml: the api section did not parse after the edit — file left untouched")
    if {k: v for k, v in after.items() if k != "api"} != \
            {k: v for k, v in before.items() if k != "api"}:
        raise RuntimeError("go2rtc.yaml: the edit touched more than api — file left untouched")
    if head is not None and got.get("listen") != api.get("listen"):
        raise RuntimeError("go2rtc.yaml: the edit moved api.listen — file left untouched")

    setup._write_private(path, new)
    setup.set_env({"GO2RTC_USER": G2R_USER, "GO2RTC_PASS": pw}, env)
    return True


def mosquitto_hash(password: str, salt: bytes | None = None, iterations: int = 101) -> str:
    """Hash in mosquitto_passwd 2.x format: $7$ — PBKDF2-SHA512.

    Done ourselves rather than `mosquitto_passwd -b`: that one takes the
    password as an argument, visible in ps, and in docker mode the host may
    not have mosquitto_passwd at all. Format verified against eclipse-mosquitto:2."""
    salt = salt if salt is not None else os.urandom(12)
    dk = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, 64)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def _owner(root: Path) -> str:
    try:
        import pwd
    except ImportError:             # Windows: no mosquitto user switching there anyway
        import getpass
        return getpass.getuser()
    try:
        return pwd.getpwuid(root.stat().st_uid).pw_name
    except (KeyError, OSError):
        return pwd.getpwuid(os.getuid()).pw_name


def native_conf(mdir: Path, root: Path) -> str:
    """Config for the native broker with a password: absolute paths, loopback only."""
    return (
        "# Generated by pawsight.authsetup for this machine; not tracked in git.\n"
        "# start.sh runs its own mosquitto with it instead of a system service.\n"
        "listener 1883 127.0.0.1\n"
        # Started as root (a server, a container), mosquitto switches to the
        # "mosquitto" user, which cannot write the pid file and the log here.
        # Stay the user who owns the install; ignored when not started as root.
        f"user {_owner(root)}\n"
        "allow_anonymous false\n"
        f"password_file {mdir / 'passwd'}\n"
        f"acl_file {mdir / 'acl'}\n"
        "persistence false\n"
        f"pid_file {mdir / 'mosquitto.pid'}\n"
        f"log_dest file {root / 'storage' / 'mosquitto.log'}\n"
        "log_type warning\n"
    )


def enable_mqtt_auth(mdir: Path | None = None, env: Path | None = None,
                     password: str | None = None) -> bool:
    """mosquitto/passwd (hash), mosquitto/native.conf and PAWSIGHT_MQTT_USER/PASS in .env.

    Already set (passwd exists and the login is in .env): nothing changes.
    Otherwise the password is new and written to both places at once: half
    of the old password would leave the system without a bus.
    """
    from dotenv import dotenv_values

    mdir = mdir or MOSQ_DIR
    env = env or setup.ENV_FILE
    passwd = mdir / "passwd"
    have = dotenv_values(env) if env.exists() else {}
    if passwd.exists() and have.get("PAWSIGHT_MQTT_USER"):
        return False
    pw = password or _new_password()
    mdir.mkdir(parents=True, exist_ok=True)
    setup._write_private(passwd, f"{MQTT_USER}:{mosquitto_hash(pw)}\n")
    setup._write_private(mdir / "native.conf", native_conf(mdir, mdir.parent))
    setup.set_env({"PAWSIGHT_MQTT_USER": MQTT_USER, "PAWSIGHT_MQTT_PASS": pw}, env)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pawsight.authsetup",
                                 description="Passwords for the go2rtc API and the MQTT broker "
                                             "(values are not printed).")
    ap.add_argument("--go2rtc", action="store_true", help="password for the go2rtc API and WebUI")
    ap.add_argument("--mqtt", action="store_true", help="password and ACL for the MQTT broker")
    a = ap.parse_args(argv)
    if not (a.go2rtc or a.mqtt):
        ap.print_help()
        return 2
    rc = 0
    if a.mqtt:
        try:
            if enable_mqtt_auth():
                print("  mqtt: password written to mosquitto/passwd (hash) and to .env "
                      "(PAWSIGHT_MQTT_USER/PAWSIGHT_MQTT_PASS). Restart the system: "
                      "./stop.sh && ./start.sh")
            else:
                print("  mqtt: password already set — nothing changed")
        except OSError as e:
            print(f"  mqtt: not done — {e}", file=sys.stderr)
            rc = 1
    if not a.go2rtc:
        return rc
    try:
        if enable_go2rtc_auth():
            print("  go2rtc: login and password written to go2rtc/go2rtc.yaml (api section) and to .env "
                  "(GO2RTC_USER/GO2RTC_PASS). Restart the system: ./stop.sh && ./start.sh")
        else:
            print("  go2rtc: password already set — nothing changed")
    except (OSError, ValueError, RuntimeError) as e:
        print(f"  go2rtc: not done — {e}", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
