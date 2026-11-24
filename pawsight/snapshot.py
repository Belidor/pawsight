"""The "right now" frame: where it comes from and how to ask for it.

The frame comes from the **detector**, not from a new connection to the
camera. The reason was measured and recorded in docs/architecture.md: the cs2 receive
buffer chokes on 2K over P2P, and every extra stream consumer means gaps in
the video for everyone else. The detector already has a continuous stream
open with a fresh frame ready in it: handing it out costs one `imencode`.

That also sets the snapshot resolution: it is whatever stream the detector
watches (`PAWSIGHT_DETECT_RTSP`). On Xiaomi cameras that is SD 640x360, on
purpose, see above.

Two ways to ask, depending on the run mode:

  * monolith (`pawsight live`): detector and brain share one process, so we
    call `Detector.snapshot_jpeg()` directly;
  * split mode (`pawsight detect` + `pawsight brain`): the brain lives in a
    container and cannot see the camera, but the bus between them already
    exists: `BusSnapshot` sends a `snap_now` request to `pawsight/commands`
    and waits for the reply on `pawsight/snapshot`. Replies of the other
    protocol (the labelling snapshot) are skipped: they have no `req` field,
    so no rendezvous matches them.

Both return the same thing: `(jpeg, note)`. No frame: the note says why.
A frame with a caveat (e.g. the stream had frozen): the note states the
caveat. Silently passing off an old frame as "now" is not allowed: that is
exactly what the person is asking about.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from .bus import TOPIC_CMD, TOPIC_SNAPSHOT, decode, encode

log = logging.getLogger("pawsight.snapshot")

# (jpeg or None, note or None)
Snapshot = tuple[bytes | None, str | None]


# The command is called snap_now, not snapshot: the `pawsight/commands` topic
# already carries the snapshot FOR LABELLING, which returns the frame ring with
# boxes and is addressed by the `id` field. One name for two different replies
# would mean the dashboard and the bot steal frames from each other.
CMD = "snap_now"


def request(req: str) -> bytes:
    """Ask the detector to send a frame."""
    return json.dumps({"cmd": CMD, "req": req}).encode()


def reply(req: str, jpeg: bytes | None, note: str | None) -> bytes:
    """The detector's reply. Request and reply formats live in one file so
    they cannot drift apart in different corners of the project."""
    payload: dict = {"req": req, "ts": time.time()}
    if note:
        payload["note"] = note
    return encode(payload, jpeg)


class BusSnapshot:
    """Asks the detector for a frame over MQTT and waits for the reply.

    The reply arrives on paho's network thread while the bot's thread waits
    for it, so the rendezvous uses an Event rather than sleep-and-poll.
    """

    def __init__(self, bus, timeout: float = 6.0) -> None:
        self.bus = bus
        self.timeout = timeout
        self._lock = threading.Lock()
        self._waiting: dict[str, tuple[threading.Event, list]] = {}

    def on_reply(self, raw: bytes) -> None:
        """TOPIC_SNAPSHOT subscriber."""
        try:
            payload, jpeg = decode(raw)
        except Exception:
            log.warning("could not parse the frame reply")
            return
        req = str(payload.get("req", ""))
        with self._lock:
            slot = self._waiting.get(req)
        if slot is None:
            # A late reply to a request that already timed out. Stay silent:
            # logging every such case is noise, not diagnostics.
            return
        ev, box = slot
        box[:] = [jpeg, payload.get("note")]
        ev.set()

    def grab(self) -> Snapshot:
        req = uuid.uuid4().hex[:8]
        ev, box = threading.Event(), [None, None]
        with self._lock:
            self._waiting[req] = (ev, box)
        try:
            self.bus.publish(TOPIC_CMD, request(req))
            if not ev.wait(self.timeout):
                return None, (f"the detector did not answer within {self.timeout:.0f} s; "
                              "is it running? (storage/detect.log)")
            return box[0], box[1]
        finally:
            with self._lock:
                self._waiting.pop(req, None)


def serve(bus, det, raw: bytes) -> None:
    """Detector side: parse the request and reply with a frame.

    Lives here, not in the detector: the detector must not know about the
    bus, which does not exist at all in the monolith.
    """
    try:
        req = str(json.loads(raw).get("req", ""))
    except Exception:
        log.warning("could not parse the frame request")
        return
    jpeg, note = det.snapshot_jpeg()
    if jpeg is None:
        log.info("requested frame not sent: %s", note)
    bus.publish(TOPIC_SNAPSHOT, reply(req, jpeg, note))
