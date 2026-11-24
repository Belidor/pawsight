"""Bus between the native detector and the containerised brain.

It carries OBSERVATIONS, not video: a few hundred bytes of JSON per frame
plus a small JPEG, and only when the dog is in the frame. This is exactly the
line along which the project splits into "must be native" and "can be in a
container".
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Callable

log = logging.getLogger("pawsight.bus")

TOPIC_OBS = "pawsight/observations"
TOPIC_STATUS = "pawsight/status"        # model/dataset/training state, retained
TOPIC_CAMLOG = "pawsight/camera_log"
TOPIC_CMD = "pawsight/commands"         # brain -> detector
TOPIC_REPLIES = "pawsight/replies"      # detector -> brain, echoes the command id
TOPIC_LOGS = "pawsight/logs"            # log lines read on the HOST
TOPIC_SNAPSHOT = "pawsight/snapshot"    # freeze-frame for labelling


# Activity feed kinds. The list is closed: the dashboard colours tags by it,
# and the server rejects anything not listed here.
KINDS = ("search", "follow", "edge", "tilt", "stream", "ptz",
         "dataset", "train", "brain", "manual", "error")


class MqttLogHandler(logging.Handler):
    """Forwards log records to MQTT for the dashboard activity feed.

    This used to be an ALLOW LIST: three loggers, six kinds, everything else
    silently vanished. In 20 minutes of running, 223 pawsight.ptz lines
    ("pan right -> ['OK']"), every error and all of the brain's work bypassed
    the feed.

    Now it is the other way round: delivery is guaranteed. A WARNING or higher
    record always reaches the feed; if its kind is not recognised, it is
    "error". Only known noise is dropped: the five-minute diagnostics and the
    startup banner, which belong in the raw logs tab.
    """

    # pawsight.web is excluded: otherwise the feed would comment on itself.
    WATCH = ("pawsight.",)
    SKIP = ("pawsight.web", "pawsight.logstream")

    BY_LOGGER = {
        "pawsight.follow": "follow", "pawsight.tilt": "tilt",
        "pawsight.ptz": "ptz", "pawsight.ptzworker": "ptz",
        "pawsight.dataset": "dataset", "pawsight.train": "train",
        "pawsight.detector": "stream",
        "pawsight.brain": "brain", "pawsight.notify": "brain", "pawsight.vlm": "brain",
        "pawsight.bus": "brain", "pawsight.motion": "stream", "pawsight.anchors": "brain",
    }

    # A repeat of the same (kind, text) pair within this window is dropped.
    # Feed volume grew roughly tenfold (follow 16/min + ptz 11/min), and
    # merging repeats here is cheaper than sorting them out downstream.
    DEDUP_SEC = 5.0

    def __init__(self, bus: "Bus") -> None:
        super().__init__(level=logging.INFO)
        self.bus = bus
        self._last: tuple[str, str] | None = None
        self._last_at = 0.0

    @classmethod
    def kind(cls, name: str, msg: str, levelno: int = logging.INFO) -> str | None:
        """The kind is determined BY CONTENT, and only then do we look at the level.

        Not the other way round: "stream froze for 2.3 s" is a warning, but it
        is the most frequent warning in the system, with its own tag and
        colour, and flattening it into "error" would lose the one signal that
        shows stream gaps at a glance.
        """
        m = msg.lower()
        if "edge" in m or "overflow" in m:
            k = "edge"
        elif name.endswith("follow"):
            k = "search" if m.startswith("search") else "follow"
        elif name.endswith("tilt") or m.startswith("tilt"):
            k = "tilt"
        elif "stream" in m:
            k = "stream"
        else:
            k = cls.BY_LOGGER.get(name)

        if levelno >= logging.WARNING:
            # Warnings and errors ALWAYS reach the feed: an unrecognised kind
            # is "error", not a reason to drop the record.
            return k or "error"
        if ("diag" in m or "detection classes" in m
                or m.startswith("detector:") or m.startswith("alive:")):
            return None
        return k

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self.WATCH) or record.name.startswith(self.SKIP):
            return
        try:
            msg = record.getMessage()
            k = self.kind(record.name, msg, record.levelno)
            if k is None:
                return
            key = (k, msg)
            if self._last == key and record.created - self._last_at < self.DEDUP_SEC:
                return
            self._last, self._last_at = key, record.created
            from .logstream import redact
            self.bus.publish(TOPIC_CAMLOG, json.dumps({
                "ts": record.created, "kind": k,
                "level": record.levelname.lower(), "text": redact(msg)[:220],
            }).encode())
        except Exception:
            pass


def encode(obs: dict, jpeg: bytes | None) -> bytes:
    payload = dict(obs)
    if jpeg:
        payload["jpeg_b64"] = base64.b64encode(jpeg).decode()
    return json.dumps(payload).encode()


def decode(raw: bytes) -> tuple[dict, bytes | None]:
    d = json.loads(raw)
    b = d.pop("jpeg_b64", None)
    return d, base64.b64decode(b) if b else None


def mqtt_topic_matches(pattern: str, topic: str) -> bool:
    """Topic matching with MQTT wildcards + and #."""
    if pattern == topic:
        return True
    ps, ts = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(ps):
        if seg == "#":
            return True
        if i >= len(ts):
            return False
        if seg != "+" and seg != ts[i]:
            return False
    return len(ps) == len(ts)


class Bus:
    def __init__(self, host: str, port: int = 1883, client_id: str = "pawsight",
                 username: str | None = None, password: str | None = None) -> None:
        # The pid is appended to the name. The broker disconnects the previous
        # owner of an identical client_id, so two processes with the same name
        # knock each other off in a loop, silently: subscriptions are
        # re-confirmed, messages are lost, and the log shows only "subscription
        # confirmed" over and over. Persistence is off in mosquitto and no
        # sessions are needed, so the name can be unique at no cost.
        self.host, self.port = host, port
        # Broker login: explicit or from the environment (PAWSIGHT_MQTT_USER/PASS).
        # Empty means an anonymous connection, as in installs that predate passwords.
        self.username = os.getenv("PAWSIGHT_MQTT_USER", "") if username is None else username
        self.password = os.getenv("PAWSIGHT_MQTT_PASS", "") if password is None else password
        self.client_id = f"{client_id}-{os.getpid()}"
        self._c = None
        self._subs: list[tuple[str, Callable[[bytes], None]]] = []

    def connect(self, background: bool = True):
        """background=True: a background thread (publisher);
        False: the caller runs loop_forever() (subscriber)."""
        import paho.mqtt.client as mqtt

        self._c = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id
        )
        # Subscriptions are made in on_connect, not right after connect():
        # a SUBSCRIBE sent before CONNACK is processed gets lost, and the same
        # would happen after every reconnect.
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        if self.username:
            self._c.username_pw_set(self.username, self.password or None)
        self._c.connect(self.host, self.port, keepalive=60)
        if background:
            self._c.loop_start()
        log.info("bus: connected to %s:%s", self.host, self.port)
        return self

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        """retain is needed for the status: a dashboard opened between detector
        ticks would otherwise show an empty tab until the next publish."""
        if self._c:
            self._c.publish(topic, payload, qos=0, retain=retain)

    def _on_connect(self, client, _u, _flags, reason_code, _props=None) -> None:
        if reason_code != 0:
            hint = ("; check PAWSIGHT_MQTT_USER/PAWSIGHT_MQTT_PASS in .env and "
                    "mosquitto/passwd" if "author" in str(reason_code).lower()
                    or "password" in str(reason_code).lower() else "")
            log.warning("bus: broker refused, code %s%s", reason_code, hint)
            return
        for topic, _ in self._subs:
            client.subscribe(topic, qos=0)
            log.info("bus: subscription to %s confirmed", topic)

    def _on_message(self, _c, _u, msg) -> None:
        for topic, handler in self._subs:
            if mqtt_topic_matches(topic, msg.topic):
                try:
                    handler(msg.payload)
                except Exception:
                    log.exception("message handler crashed")

    def subscribe(self, topic: str, handler: Callable[[bytes], None]) -> None:
        self._subs.append((topic, handler))
        if self._c and self._c.is_connected():
            self._c.subscribe(topic, qos=0)
            log.info("bus: subscribing to %s", topic)

    def loop_forever(self) -> None:
        """Blocking subscriber loop with auto-reconnect."""
        self._c.loop_forever(retry_first_connection=True)

    def close(self) -> None:
        if self._c:
            self._c.loop_stop()
            self._c.disconnect()
