#!/usr/bin/env python3
"""Fetches the DEVICE token (needed for PTZ) without entering a password.

Uses the passToken go2rtc already saved when you logged in:
  1. serviceLogin with the userId+passToken cookie -> ssecurity + location
  2. GET location -> serviceToken
  3. /home/device_list -> each device has its own token

No password is requested or entered anywhere. The token is NOT printed to the
screen: the script writes it straight to .env (which is in .gitignore).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from micloud import MiCloud

ROOT = Path(__file__).resolve().parent.parent
G2R = ROOT / "go2rtc" / "go2rtc.yaml"
ENV = ROOT / ".env"
load_dotenv(ENV)
# The camera's did. There is no way to know it in advance, so:
#   * pass it as an argument:  get_device_token.py <did>
#   * put it in .env:          CAMERA_DID=<did>
#   * or leave it unset: the script then lists the account's devices and the
#     did is taken from there (if the account has a single camera, automatically).
WANT_DID = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("CAMERA_DID", "")).strip()


def read_go2rtc_creds() -> tuple[str, str]:
    if not G2R.exists():
        sys.exit(f"{G2R} not found; log in via the go2rtc WebUI first")
    data = yaml.safe_load(G2R.read_text()) or {}
    xm = data.get("xiaomi") or {}
    if not xm:
        sys.exit("go2rtc.yaml has no xiaomi section; log in via the go2rtc WebUI")
    uid = str(next(iter(xm)))
    return uid, str(xm[uid])


def main() -> int:
    uid, pass_token = read_go2rtc_creds()
    print(f"  account: {uid} (passToken taken from go2rtc, no password needed)")

    # __init__ does not need a password; it only sets locale/timezone/defaults.
    # We replace the login: instead of the password steps, we supply the ready passToken.
    mc = MiCloud(username=None, password=None)
    mc.user_id = uid
    mc.pass_token = pass_token
    mc._init_session()

    mc.session.cookies.update({"userId": uid, "passToken": pass_token})
    r = mc.session.get("https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true")
    body = json.loads(r.text.replace("&&&START&&&", ""))

    if "ssecurity" not in body or not body.get("location"):
        sys.exit(
            "  passToken rejected (expired?). Log in again via the go2rtc WebUI "
            f"and retry. Response: result={body.get('result')} desc={body.get('desc')}"
        )
    mc.ssecurity = body["ssecurity"]
    mc.cuser_id = body.get("cUserId")
    print("  ssecurity received")

    mc._login_step3(body["location"])
    if not mc.service_token:
        sys.exit("  failed to obtain serviceToken")
    print("  serviceToken received")

    devices = mc.get_devices(country="cn")
    if not devices:
        sys.exit("  the device list is empty")
    print(f"  devices in the account: {len(devices)}")

    if WANT_DID:
        cam = next((d for d in devices if str(d.get("did")) == WANT_DID), None)
    else:
        # A device counts as a camera if its model contains the word camera.
        cams = [d for d in devices if "camera" in str(d.get("model", "")).lower()]
        cam = cams[0] if len(cams) == 1 else None
        if cam is not None:
            print(f"  the account has a single camera, using it: did={cam.get('did')}")

    if cam is None:
        print("  no device selected. Devices in the account:")
        for d in devices:
            print(f"    did={d.get('did')} model={d.get('model')} name={d.get('name')}")
        print("\n  Run again with the right did:")
        print("    .venv/bin/python scripts/get_device_token.py <did>")
        return 1

    token = cam.get("token") or ""
    if len(token) != 32:
        sys.exit(f"  unexpected token length ({len(token)}), not writing to .env")

    print(f"  camera found: model={cam.get('model')} ip={cam.get('localip')}")

    text = ENV.read_text() if ENV.exists() else ""
    for key, val in (("CAMERA_TOKEN", token), ("CAMERA_IP", cam.get("localip") or "")):
        if not val:
            continue
        if re.search(rf"^{key}=.*$", text, re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={val}", text, flags=re.M)
        else:
            text += f"\n{key}={val}\n"
    ENV.write_text(text)
    print("  ✓ CAMERA_TOKEN written to .env (not shown on screen)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
