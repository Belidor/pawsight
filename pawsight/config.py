"""Configuration: .env + zones.yaml. Secrets come only from the environment, never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .notify import parse_chats

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- who owns storage/events.db ----------------------------------------------
#
# Incident 2026-09-06. The brain ran in a container, storage/ reached it via a
# bind mount, and the host command `pawsight today` opened the same file with
# its own connection. SQLite locks do not cross the Docker Desktop VM boundary,
# so both sides believed they were alone: on exit the host process recreated
# events.db-wal and -shm, while the brain's connection stayed on the deleted
# inodes. The dashboard started answering "disk I/O error" to every request,
# and everything the brain wrote afterwards went into an orphaned WAL and was
# lost. Silently, for three hours.
#
# Hence the rule: the right to open the SHARED database is checked, not assumed.
# Measurements and analysis: docs/architecture.md, 2026-09-06.

IN_CONTAINER = Path("/.dockerenv").exists() or ROOT == Path("/app")


def _stack(raw: str | None) -> str:
    """Where the brain lives: `native` or `docker`.

    The value is read leniently as to form (spaces, quotes, a comment on the
    same line) and strictly as to meaning: anything but an exact `native` is
    docker. start.sh picks the default the same way: a missing key also means
    docker. Fail-closed on purpose: a typo in .env must not silently give the
    host access to the container's database.
    """
    value = (raw or "").split("#", 1)[0].strip().strip("'\"").lower()
    return "native" if value == "native" else "docker"


STACK = _stack(os.getenv("PAWSIGHT_STACK"))


def _main_root() -> Path:
    """Root of the MAIN working tree, not the current one.

    The container gets `./storage` mounted from the directory holding
    docker-compose.yml, i.e. from the main checkout. In a git worktree ROOT
    points at the worktree itself, and without this correction the gate would
    lock its `storage/events.db`, a database nobody mounted and nobody owns.
    Worktree marker: `.git` there is a file with a link, not a directory.
    """
    dotgit = ROOT / ".git"
    if dotgit.is_file():
        try:
            line = dotgit.read_text().strip()
        except OSError:
            return ROOT
        if line.startswith("gitdir:"):
            # <main>/.git/worktrees/<name> -> go up three levels
            main = Path(line.split(":", 1)[1].strip()).parent.parent.parent
            if (main / "pawsight").is_dir():
                return main
    return ROOT


SHARED_DB = _main_root() / "storage" / "events.db"


def db_is_foreign(path) -> bool:
    """True: this database is owned by the brain in the container and we are on the host.

    Exactly the file passed through to the container is forbidden, not any
    SQLite: temporary test databases, `pawsight batch` with its own path and
    work in a git worktree (its own `storage/`, not given to the container)
    stay legitimate. The ban must be precise, or people will route around it,
    and a ban that is too broad simply stops being thought about.
    """
    if IN_CONTAINER or STACK == "native":
        return False
    try:
        return Path(path).resolve() == SHARED_DB.resolve()
    except OSError:
        return False


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _parse_shape(zone: str, raw) -> tuple[tuple[int, int], ...] | None:
    """shape from zones.yaml: at least three vertices, each a pair of numbers.

    A malformed shape fails loading right away instead of turning into "no
    zone": a zone that silently failed to build is indistinguishable from
    "bowls not visible", and bowl visits would just vanish from the statistics
    without a single error.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(f"zones.yaml: zone {zone} shape must be a list of vertices")
    pts: list[tuple[int, int]] = []
    for p in raw:
        ok = (isinstance(p, (list, tuple)) and len(p) == 2
              and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p))
        if not ok:
            raise ValueError(f"zones.yaml: zone {zone} shape vertex must be "
                             f"a pair of numbers [dx, dy], not {p!r}")
        pts.append((int(round(p[0])), int(round(p[1]))))
    if len(pts) < 3:
        raise ValueError(f"zones.yaml: zone {zone} shape has fewer than three vertices ({len(pts)})")
    return tuple(pts)


@dataclass(frozen=True)
class Zone:
    """A zone: either a fixed polygon or tied to an anchor in the frame."""

    name: str
    label: str
    polygon: list[tuple[int, int]] | None = None
    anchor_class: str | None = None
    anchor_min_conf: float = 0.20
    anchor_expand: tuple[int, int, int, int] = (60, 40, 60, 120)
    anchor_memory_sec: float = 20.0
    anchor_select: str = "all"
    # A shape ON THE FLOOR: vertices as offsets from the anchor's floor contact
    # point ((x1+x2)/2, y2) in detector-frame pixels. When set, the expand
    # rectangle is not built. Drawn by the owner (zones.yaml, AGENTS.md section 6).
    anchor_shape: tuple[tuple[int, int], ...] | None = None
    # Send Telegram notifications for events in this zone. Missing in older
    # zones.yaml files, so the default keeps the old behaviour: notify.
    notify: bool = True

    @property
    def is_anchored(self) -> bool:
        return self.anchor_class is not None


def detect_device() -> str:
    """mps on Apple Silicon, cuda:0 with an NVIDIA GPU, otherwise cpu."""
    try:
        import torch
    except ImportError:              # the brain image has no torch, and needs none
        return "cpu"
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


@dataclass
class Config:
    rtsp: str = os.getenv("PAWSIGHT_RTSP", "rtsp://127.0.0.1:8554/dog_cam")
    # The detector takes the 640x360 SD substream, not the 2K HD one. The reason
    # was measured: go2rtc's cs2 receive buffer chokes on 2K P2P (551 overflows/min,
    # gaps every ~8 s), while SD has 0 gaps, 0 overflows. HD stays for the dashboard.
    detect_rtsp: str = os.getenv("PAWSIGHT_DETECT_RTSP", "rtsp://127.0.0.1:8554/dog_cam_sd")
    model: str = os.getenv("PAWSIGHT_MODEL", "yolo26s.pt")
    # auto: Apple Metal (mps), then an NVIDIA GPU (cuda:0), then the CPU.
    # Resolved by resolve_device() only in the processes that run the model:
    # the brain must not import torch just to read its config.
    device: str = os.getenv("PAWSIGHT_DEVICE", "auto")
    tracker: str = os.getenv("PAWSIGHT_TRACKER", "botsort.yaml")
    detect_fps: float = _f("PAWSIGHT_DETECT_FPS", 5)
    imgsz: int = _i("PAWSIGHT_IMGSZ", 640)
    # The frame is downscaled to this size before inference. 1280x720 was
    # measured as the best trade-off: -25% time at the same detection confidence.
    # For the SD source: we work at its native resolution, no upscaling.
    infer_w: int = _i("PAWSIGHT_INFER_W", 640)
    infer_h: int = _i("PAWSIGHT_INFER_H", 360)
    # The threshold handed to the tracker is LOW on purpose: BoT-SORT has two
    # stages (track_high 0.25 / track_low 0.1), and the second one rescues an
    # already tracked dog when its confidence drops (held in arms, wearing a
    # cone, against the light). A 0.35 pre-filter defeated that stage: a dog in
    # arms "vanished". Caveat (checked against ultralytics 8.4.141 code,
    # 2026-09-07): the second stage only works while the track is continuously
    # maintained. One frame without a match makes the track Lost, only boxes
    # >= 0.25 can revive it, and a new track starts from 0.25 and is reported
    # from the second consecutive frame; with fuse_score, confirmation needs
    # IoU*conf >= 0.3: a dog with conf < 0.30 will not appear in `dogs` at all
    # after losing its track. The profile without fuse_score is
    # trackers/botsort-dog.yaml (PAWSIGHT_TRACKER), not yet proven in production.
    conf: float = _f("PAWSIGHT_CONF", 0.15)

    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen3-vl:8b-instruct")

    # repr=False: a dataclass prints ALL fields, and a single line like
    # log.info("config: %s", cfg) would publish the whole bot token.
    tg_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""),
                          repr=False)
    tg_chat: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""),
                         repr=False)

    rate_limit_sec: int = _i("PAWSIGHT_RATE_LIMIT_SEC", 300)

    mqtt_host: str = os.getenv("PAWSIGHT_MQTT_HOST", "127.0.0.1")
    mqtt_port: int = _i("PAWSIGHT_MQTT_PORT", 1883)
    # Broker login. Empty means anonymous, as on installs from before passwords.
    mqtt_user: str = field(default_factory=lambda: os.getenv("PAWSIGHT_MQTT_USER", ""))
    mqtt_pass: str = field(default_factory=lambda: os.getenv("PAWSIGHT_MQTT_PASS", ""),
                           repr=False)
    web_port: int = _i("PAWSIGHT_WEB_PORT", 8099)
    # The dashboard listens on loopback ONLY: the setup page is where the camera
    # password and the bot token are entered, and they have no business on the
    # home network. In the container the address is overridden to 0.0.0.0
    # (docker-compose.yml): nothing gets out from there anyway, since it is
    # published as 127.0.0.1:8099.
    web_host: str = os.getenv("PAWSIGHT_WEB_HOST", "127.0.0.1")

    # camera tilt hold against the reference frame
    tilt_hold: bool = os.getenv("PAWSIGHT_TILT_HOLD", "1") == "1"
    tilt_interval: float = _f("PAWSIGHT_TILT_INTERVAL", 60)
    tilt_act: bool = os.getenv("PAWSIGHT_TILT_ACT", "1") == "1"      # 0 = measure only
    tilt_ref: Path = ROOT / "storage" / "tilt_reference.jpg"
    # our own dog following (pan only); the camera's built-in tracking is off
    follow_dog: bool = os.getenv("PAWSIGHT_FOLLOW_DOG", "1") == "1"
    # dog search: one pan step every N seconds while the dog is out of frame (0 = off).
    # Works only when patrol is off (PAWSIGHT_HOME_HOLD=0).
    search_sec: float = _f("PAWSIGHT_SEARCH_SEC", 10)

    # PATROL: by default the camera looks at the bowls and searches for the dog
    # in sectors. The sector unit is a motor PULSE, not a degree: the camera's
    # field of view has not been measured, and the step is asymmetric (right 0.096
    # of the frame width, left 0.162). Defaults are chosen so both sides sweep
    # about 1.2 frame widths.
    home_hold: bool = os.getenv("PAWSIGHT_HOME_HOLD", "1") == "1"
    search_idle_sec: float = _f("PAWSIGHT_SEARCH_IDLE_SEC", 180)
    scan_step_right: int = _i("PAWSIGHT_SCAN_STEP_RIGHT", 3)
    scan_step_left: int = _i("PAWSIGHT_SCAN_STEP_LEFT", 2)
    scan_steps: int = _i("PAWSIGHT_SCAN_STEPS", 4)
    scan_dwell_sec: float = _f("PAWSIGHT_SCAN_DWELL_SEC", 5)
    # The dwell at a stop waits not just for the clock but for live frames: after
    # a pan the stream freezes for 6-19 s, and the sector would be swept blind (AGENTS.md, 8).
    scan_dwell_frames: int = _i("PAWSIGHT_SCAN_DWELL_FRAMES", 2)
    # A safety fuse instead of the removed steps-per-minute limit: a bug must not
    # run the motor forever.
    scan_max_pulses: int = _i("PAWSIGHT_SCAN_MAX_PULSES", 60)
    # Delay after the dog disappears before patrol takes the camera back from
    # following. Without it, flickering detection pits the two loops against
    # each other: following pulls towards the dog, patrol pulls home, and the
    # motor runs for nothing.
    patrol_release_sec: float = _f("PAWSIGHT_PATROL_RELEASE_SEC", 10)
    home_ref: Path = ROOT / "storage" / "home_ref.json"

    collect_dataset: bool = os.getenv("PAWSIGHT_COLLECT_DATASET", "1") == "1"
    dataset_dir: Path = ROOT / "dataset"

    db_path: Path = SHARED_DB
    snap_dir: Path = ROOT / "storage" / "snapshots"
    zones: list[Zone] = field(default_factory=list)
    # Notify when the dog is outside every zone (zones.yaml: notify_outside_zones).
    notify_outside: bool = True

    def __post_init__(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.zones = self._load_zones()
        self.notify_outside = self._load_notify_outside()

    @staticmethod
    def _read_zones_file(path: Path | None = None) -> dict:
        f = path or ROOT / "zones.yaml"
        if not f.exists():
            return {}
        return yaml.safe_load(f.read_text()) or {}

    @staticmethod
    def _load_notify_outside(path: Path | None = None) -> bool:
        return bool(Config._read_zones_file(path).get("notify_outside_zones", True))

    def resolve_device(self) -> str:
        """Turn device "auto" into the best available one, once."""
        if self.device in ("", "auto"):
            self.device = detect_device()
        return self.device

    def apply_zones(self, raw: dict) -> None:
        """Swap in zones edited at runtime (the zone editor), without a restart.

        Both attributes are replaced whole: the brain's report thread reads them
        concurrently and must see either the old list or the new one.
        """
        self.zones = self.zones_from_raw(raw)
        self.notify_outside = bool(raw.get("notify_outside_zones", True))

    @staticmethod
    def _load_zones(path: Path | None = None) -> list[Zone]:
        return Config.zones_from_raw(Config._read_zones_file(path))

    @staticmethod
    def zones_from_raw(raw: dict) -> list[Zone]:
        """Zones from a parsed zones.yaml mapping."""
        out = []
        for name, spec in (raw.get("zones") or {}).items():
            a = spec.get("anchor")
            if a:
                exp = a.get("expand", [60, 40, 60, 120])
                out.append(
                    Zone(
                        name=name,
                        label=spec.get("label", name),
                        anchor_class=a["class"],
                        anchor_min_conf=float(a.get("min_conf", 0.20)),
                        anchor_expand=tuple(exp),
                        anchor_memory_sec=float(a.get("memory_sec", 20.0)),
                        anchor_select=a.get("select", "all"),
                        anchor_shape=_parse_shape(name, a.get("shape")),
                        notify=bool(spec.get("notify", True)),
                    )
                )
            elif spec.get("polygon"):
                out.append(
                    Zone(
                        name=name,
                        label=spec.get("label", name),
                        polygon=[tuple(p) for p in spec["polygon"]],
                        notify=bool(spec.get("notify", True)),
                    )
                )
        return out

    @property
    def tg_chats(self) -> list[str]:
        """There can be several recipients: TELEGRAM_CHAT_ID, comma-separated."""
        return parse_chats(self.tg_chat)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.tg_token and self.tg_chats)
