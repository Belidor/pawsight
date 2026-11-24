"""Camera control over the local miIO protocol.

Two protocols, do not mix them up:
  * video goes over P2P (`miss`/cs2); go2rtc handles it and needs the
    ACCOUNT token;
  * control goes over local miIO on UDP/54321 and needs the DEVICE token
    (32 hex). Obtain it with scripts/get_device_token.py.

Pan is missing from this model's MIoT spec (9 services, not a single
pan/tilt), so the legacy `set_motor` command is used.

Auto-tracking is the opposite: it lives ONLY in MIoT (siid=2, piid=8).
The legacy `track` property from `get_prop` has nothing to do with it:
verified, it always returns "off" regardless of the actual tracking state.
The camera rejects set_track / set_motion_track / set_follow.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from .miio_lite import MiioDevice, MiioError

log = logging.getLogger("pawsight.ptz")

DIRECTIONS = {"left": 1, "right": 2, "up": 3, "down": 4}

# Motion Tracking in the MIoT spec of chuangmi.camera.029a02
TRACK_SIID, TRACK_PIID = 2, 8

__all__ = ["CameraPTZ", "PTZError", "DIRECTIONS"]

PTZError = MiioError


class CameraPTZ:
    def __init__(self, ip: str | None = None, token: str | None = None,
                 timeout: float = 5.0) -> None:
        ip = ip or os.getenv("CAMERA_IP", "")
        token = token or os.getenv("CAMERA_TOKEN", "")
        if not ip:
            raise MiioError("CAMERA_IP is not set in .env")
        if not token:
            raise MiioError(
                "CAMERA_TOKEN is not set in .env: this is the DEVICE token (32 hex), "
                "not the one go2rtc uses. Get it with: python scripts/get_device_token.py"
            )
        self.dev = MiioDevice(ip, token, timeout=timeout)
        self.ip = ip
        # One object is called from several threads (the web server's executor):
        # the time counter and message ids in MiioDevice are not thread-safe.
        self._lock = threading.Lock()

    # ---- pan ----

    def rotate(self, direction: str) -> str:
        d = direction.lower()
        if d not in DIRECTIONS:
            raise MiioError(f"direction '{direction}'; allowed: {', '.join(DIRECTIONS)}")
        # No retries: set_motor is not idempotent. A timeout does not mean "did
        # not arrive": the motor is busy and does not answer on UDP, and a retry
        # after it turned the camera twice. Reads (get_prop etc.) do retry.
        with self._lock:
            res = self.dev.send("set_motor", {"operation": DIRECTIONS[d]}, retries=0)
        log.info("pan %s -> %s", d, res)
        return str(res)

    # ---- auto-tracking ----

    def _did(self) -> str:
        if self.dev._did is None:
            self.dev.handshake()
        return str(self.dev._did)

    def get_tracking(self) -> bool:
        # Reading the state has no retries: it is a poll, not an action.
        # Retries with a 5 s timeout meant waiting up to 15 s.
        with self._lock:
            r = self.dev.send("get_properties",
                              [{"did": self._did(), "siid": TRACK_SIID, "piid": TRACK_PIID}],
                              retries=0)
        return bool(r[0].get("value"))

    def set_tracking(self, on: bool, verify: bool = True) -> bool:
        """The camera's built-in motion tracking. Returns the state after setting it.

        The camera confirms the write immediately (code=0), but the new value
        does not become readable instantly: without a pause, reading back
        returns the old one. Verified: 3 seconds is enough in both directions.
        """
        with self._lock:
            self.dev.send("set_properties",
                          [{"did": self._did(), "siid": TRACK_SIID,
                            "piid": TRACK_PIID, "value": bool(on)}])
        if not verify:
            return bool(on)
        time.sleep(3)
        state = self.get_tracking()
        log.info("auto-tracking -> %s", "on" if state else "off")
        return state

    # ---- status ----

    def status(self) -> dict:
        names = ["power", "light", "full_color", "flip", "wdr",
                 "watermark", "sdcard_status", "night_mode"]
        with self._lock:
            vals = self.dev.send("get_prop", names)
        out = dict(zip(names, vals))
        out["motion_tracking"] = self.get_tracking()
        return out
