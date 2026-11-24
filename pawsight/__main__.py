"""Entry point: python -m pawsight [live|batch|setup|selftest|today]"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

from .brain import Brain
from .config import Config, db_is_foreign
from .store import REPORT_LIMIT, Store
from .vlm import VLM

# Detector is NOT imported at module level: it pulls in torch/ultralytics/cv2,
# which are not (and must not be) in the brain container. It is imported
# inside the modes that actually need it.


def _log() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        # The date is required: the log lives for days, and with only "13:49:02"
        # the lines of a multi-day file can be neither sorted nor filtered.
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # At INFO, httpx prints the full request URL, and Telegram puts the bot
    # token right in the path: .../bot<TOKEN>/sendPhoto. With level=INFO on the
    # root it ended up in the process log (in docker, in the on-disk json log
    # forever), one line per recipient. And the docs tell people to read these
    # logs when troubleshooting, so they also get copied around.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Second line of defence: the log is now also visible on the dashboard, so
    # even a secret that leaks in some other way must not leave the process.
    # The filter is attached to the HANDLERS, see install_redaction.
    from .logstream import install_redaction
    install_redaction()


def selftest(cfg: Config) -> int:
    """Checks everything needed to run and honestly reports what is missing."""
    ok = True
    print("=== self-test ===")

    import torch
    mps = torch.backends.mps.is_available()
    cuda = torch.cuda.is_available()
    gpu = ("Metal (mps)" if mps else
           f"CUDA ({torch.cuda.get_device_name(0)})" if cuda else "none, the CPU will be used")
    print(f"  GPU              : {gpu}")
    print(f"  device           : {cfg.device}")
    # Only a device that was asked for explicitly and is missing is an error:
    # the CPU works, just slower.
    if (cfg.device == "mps" and not mps) or (cfg.device.startswith("cuda") and not cuda):
        ok = False

    import cv2
    cap = cv2.VideoCapture(cfg.rtsp)
    got = cap.isOpened() and cap.read()[0]
    cap.release()
    print(f"  RTSP {cfg.rtsp}: {'frame received' if got else 'UNAVAILABLE'}")
    ok &= got

    v = VLM(cfg.ollama_url, cfg.ollama_model)
    av = v.available()
    print(f"  Ollama {cfg.ollama_model}: {'ready' if av else 'model not found/not running'}")
    ok &= av

    print(f"  zones defined    : {len(cfg.zones)} ({', '.join(z.name for z in cfg.zones) or '—'})")
    ok &= bool(cfg.zones)
    if cfg.home_hold:
        patrol = (f"home at the bowls, search after {cfg.search_idle_sec:.0f} s; "
                  f"sector: {cfg.scan_steps} stops, step "
                  f"{cfg.scan_step_right} pulses right / {cfg.scan_step_left} left")
    else:
        patrol = ("off, legacy search: one step every "
                  f"{cfg.search_sec:.0f} s" if cfg.search_sec > 0 else "off, no search")
    print(f"  patrol           : {patrol}")
    tg = (f"configured, recipients: {len(cfg.tg_chats)}" if cfg.telegram_ready
          else "NOT configured (events go to the DB only)")
    print(f"  Telegram         : {tg}")
    print(f"  DB               : {cfg.db_path}")
    print("=== result:", "ready to run" if ok else "blockers found", "===")
    return 0 if ok else 1


def _start_logtail(bus, path) -> None:
    """Raw log to the dashboard, read on the HOST.

    From the container the same file is visible with a lag of up to tens of
    seconds (VirtioFS caches the attributes of a file held open by a host
    writer), so the live stream goes over the bus and the container touches
    the file only for history.
    """
    import json as _j
    from .bus import TOPIC_LOGS
    from .logstream import redact
    from .logtail import FileTailer, stdout_is

    run_id = f"{time.time():.0f}"

    def send(lines: list[str], n0: int) -> None:
        bus.publish(TOPIC_LOGS, _j.dumps({
            "src": "detect", "run_id": run_id, "n0": n0,
            "lines": [redact(x) for x in lines],
        }, ensure_ascii=False).encode())

    if stdout_is(path):
        FileTailer(path, send).start()
        logging.getLogger("pawsight.logtail").info("tailing %s", path.name)
        return

    # Not started via start.sh: nothing to tail, so send our own records.
    # ffmpeg output will not get here: it bypasses logging.
    class _H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                line = "%s %-7s %s: %s" % (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                    record.levelname, record.name, record.getMessage())
                send([line], 0)
            except Exception:
                pass

    h = _H(level=logging.INFO)
    logging.getLogger().addHandler(h)
    logging.getLogger("pawsight.logtail").info(
        "stdout is not a log file; sending logger records directly (ffmpeg output will be missing)")


def _day_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def _fmt_dur(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m} min {s} s" if m else f"{s} s"


class BrainUnreachable(RuntimeError):
    """No source for the report: the brain owns the database but is not responding."""


def _report_get(path: str, params: dict) -> dict:
    """Ask the brain for a report over HTTP.

    Needed when the brain lives in a container: it holds the database itself,
    and a host command is not allowed to open it (pawsight/store.py,
    ForeignDatabase). RAW rows are returned and this same CLI formats them,
    so the output matches the native one by construction, not because two
    formatters happen to agree for now.

    stdlib urllib is used on purpose: the command must work even where httpx
    is not installed. The address is always 127.0.0.1, nothing goes outside.
    """
    import http.client
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    port = int(os.getenv("PAWSIGHT_WEB_PORT", "8099"))
    url = f"http://127.0.0.1:{port}{path}?{urllib.parse.urlencode(params)}"
    # Bypass any proxy. These commands used to not touch the network at all,
    # and now an http_proxy set in the environment (corporate VPN, Proxyman, a
    # line in .zshrc) would route the 127.0.0.1 request to the proxy and fail it.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BrainUnreachable(
                f"The brain on 127.0.0.1:{port} responds but does not know the route {path}, "
                "so it is running an old image built before this change. The brain's code "
                "is baked into the image, so rebuild: ./start.sh"
            ) from e
        raise BrainUnreachable(
            f"The brain on 127.0.0.1:{port} returned error {e.code} for {path}. "
            "Check its log: docker compose logs brain"
        ) from e
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        # http.client.HTTPException means the response broke off while reading
        # the body (IncompleteRead, BadStatusLine): a brain under
        # `restart: unless-stopped` may restart mid-response, and a traceback
        # is of no use here.
        raise BrainUnreachable(
            f"The brain did not respond on 127.0.0.1:{port} ({e}). In docker mode reports "
            "come from it: it holds the database, and opening it with a second connection "
            "from the host is not allowed. Check that the container is up: "
            "`docker compose ps`; to start it: ./start.sh"
        ) from e


def _report(cfg: Config, path: str, since: float, native):
    """The same report, either from the brain over HTTP or straight from the DB.

    There is no fallback of "the brain is silent, so I will open the DB
    myself", and there must not be: exactly that fallback would bring back
    the 2026-09-06 incident. A brain under `restart: unless-stopped` comes
    back by itself, and "silent" almost always means "not up yet", not
    "gone".
    """
    if db_is_foreign(cfg.db_path):
        return _report_get(path, {"since": since})
    st = Store(cfg.db_path)
    try:
        return native(st)
    finally:
        st.close()


def drinks(cfg: Config, days: int = 1) -> int:
    """How many times and for how long the dog drank.

    Video measures VISITS and TIME at the bowl. The camera does not measure
    the amount drunk: there are deliberately no millilitres here.
    """
    # The HOST computes the day boundary: the image's TZ is set in
    # docker-compose.yml and may differ from the Mac's, and "today" must mean
    # today for the person.
    since = _day_start() - (days - 1) * 86400
    try:
        rows = _report(cfg, "/api/report/drinks", since,
                       lambda st: {"rows": st.bouts_by_kind(since, "drink")})["rows"]
    except BrainUnreachable as e:
        print(e)
        return 1
    print(f"=== Drinking {'today' if days == 1 else f'over {days} days'} ===")
    if not rows:
        print("  no visits to the water bowl recorded")
        print("  (amount is not measured; visits and time are counted)")
        return 0
    total = sum(r[1] for r in rows)
    print(f"  visits: {len(rows)}   total at the bowl: {_fmt_dur(total)}")
    if len(rows) >= REPORT_LIMIT:
        print(f"  (showing the first {REPORT_LIMIT}; the result is capped)")
    print("  (amount is not measured; visits and time are counted)\n")
    for ts, dur, conflict, raw in rows:
        flag = "  ⚠️ disagrees with geometry" if conflict else ""
        print(f"    {time.strftime('%H:%M', time.localtime(ts))}  {_fmt_dur(dur)}{flag}")
    return 0


def today(cfg: Config) -> int:
    since = _day_start()
    try:
        data = _report(cfg, "/api/report/today", since,
                       lambda st: {"summary": st.bout_summary(since),
                                   "events": st.today(since)})
    except BrainUnreachable as e:
        print(e)
        return 1

    summary = data["summary"]
    if summary:
        human = {"drink": "drank", "eat": "ate", "unknown": "at the bowls (unrecognised)"}
        print("=== Bowl visits today ===")
        for kind, cnt, total, conflicts in summary:
            line = f"  {human.get(kind, kind):<26} {cnt} times, total {_fmt_dur(total or 0)}"
            if conflicts:
                line += f"  ({conflicts} with disagreement)"
            print(line)
        print()

    rows = data["events"]
    if not rows:
        print("No events today.")
        return 0
    print(f"=== Events today: {len(rows)} ===")
    if len(rows) >= REPORT_LIMIT:
        print(f"  (showing the first {REPORT_LIMIT}; the result is capped)")
    for ts, zone, state_, vlm in rows:
        hhmm = time.strftime("%H:%M", time.localtime(ts))
        print(f"  {hhmm}  {zone or 'no zone':<10} {state_:<12} {vlm or ''}")
    return 0


def tilt(cfg: Config, args: list[str]) -> int:
    """Camera tilt: status | restore | recapture.

    The frame is taken LIVE: a new RTSP consumer in go2rtc starts from the
    last keyframe, so the first frame read may be ~2 s older than reality.
    The stream is therefore read for ~2.5 s before a frame goes into the
    measurement.
    """
    import cv2
    from .tilt import TiltHold

    cmd = args[0] if args else "status"

    if cmd == "recapture":
        # The DETECTOR captures the reference on its own settled frame: it has
        # the only continuous (non-lagging) view of the camera. We only send
        # the command from here.
        import json as _j
        from .bus import Bus
        Bus(cfg.mqtt_host, cfg.mqtt_port, client_id="pawsight-cli",
                  username=cfg.mqtt_user, password=cfg.mqtt_pass).connect().publish(
            "pawsight/commands", _j.dumps({"cmd": "tilt_recapture"}).encode())
        time.sleep(0.5)
        print("  command sent to the detector: the reference will be re-captured on the first settled frame")
        return 0

    cap = cv2.VideoCapture(cfg.rtsp, cv2.CAP_FFMPEG)
    t0 = time.time()
    frame = None
    while time.time() - t0 < 2.5:
        if not cap.grab():
            break
        ok, f = cap.retrieve()
        if ok:
            frame = f
    cap.release()
    if frame is None:
        print("  no frame received; is go2rtc running?")
        return 1
    frame = cv2.resize(frame, (cfg.infer_w, cfg.infer_h), interpolation=cv2.INTER_AREA)

    ptz = None
    if cmd == "restore":
        from .ptz import CameraPTZ
        ptz = CameraPTZ()
    th = TiltHold(cfg.tilt_ref, cfg.infer_h, ptz)
    if cmd in ("status", "restore"):
        from ultralytics import YOLO
        mdl = YOLO(cfg.model)
        inv = {v: k for k, v in mdl.names.items()}
        from .tilt import LANDMARK_CLASSES
        r = mdl.predict(frame, device=cfg.device, imgsz=cfg.imgsz, conf=0.25,
                        classes=[inv[c] for c in LANDMARK_CLASSES if c in inv], verbose=False)[0]
        anchors = [{"cls": mdl.names[int(c)], "box": [float(v) for v in b]}
                   for b, c in zip(r.boxes.xyxy, r.boxes.cls)]
        lm = th.landmarks(anchors)
        if not th.targets:
            print("  the reference has no landmarks; run: python -m pawsight tilt recapture")
            return 1
        common = [c for c in lm if c in th.targets]
        print(f"  landmarks in frame: {sorted(lm)}; in reference: {sorted(th.targets)}; common: {common}")
        if not common:
            print("  none of the reference landmarks are visible; nothing to measure tilt by (different room?)")
            return 1
        import numpy as _np
        dy = float(_np.median([lm[c] - th.targets[c] for c in common]))
        print(f"  offset from reference: {dy*1296/cfg.infer_h:+.0f} px ({dy/th.step_px:+.1f} steps)")
        if cmd == "restore":
            print(f"  {th._decide(dy, len(common), True, time.time())['status']}")
        elif abs(dy) < th.step_px / 2:
            print("  tilt is on target")
        else:
            print("  tilt has drifted; restore with: python -m pawsight tilt restore")
        return 0
    print("usage: python -m pawsight tilt status|recapture|restore")
    return 2


def ptz(args: list[str]) -> int:
    """Camera pan/tilt and the built-in auto-tracking."""
    from .ptz import DIRECTIONS, CameraPTZ, PTZError

    if not args:
        print("usage: python -m pawsight ptz <left|right|up|down|status|track-on|track-off>")
        return 2
    cmd = args[0].lower()
    try:
        p = CameraPTZ()
        if cmd in DIRECTIONS:
            print(f"  move {cmd}: {p.rotate(cmd)}")
        elif cmd == "status":
            for k, v in p.status().items():
                print(f"  {k:<16} {v}")
        elif cmd in ("track-on", "track-off"):
            on = p.set_tracking(cmd == "track-on")
            print(f"  auto-tracking: {'on' if on else 'off'}")
        else:
            print(f"unknown ptz command: {cmd}")
            return 2
        return 0
    except PTZError as e:
        print(f"  PTZ unavailable: {e}")
        return 1
    except Exception as e:
        print(f"  camera communication error: {type(e).__name__}: {e}")
        return 1


def dataset_status(cfg: Config, args: list[str] | None = None) -> int:
    """What has been collected for fine-tuning."""
    from .hoststat import dataset_stats
    from .labels import DATASET_CLASSES, MIN_IMAGES

    args = args or []
    st = dataset_stats(cfg.dataset_dir)

    if "--refreeze-val" in args:
        # The same rebuild train.py::_split does at the start of a run. The
        # command remains for anyone who needs it ahead of time, separately
        # from training; the dashboard button no longer needs it.
        from .train import refreeze_val

        if st["pairs"] < MIN_IMAGES:
            print(f"  {st['pairs']} labelled frames, at least {MIN_IMAGES} needed")
            return 1
        val, pairs = refreeze_val(cfg.dataset_dir)
        print(f"  validation split rebuilt: {val} of {pairs} frames")
        print("  WARNING: metrics of past runs are no longer comparable with future ones;")
        print("  that is the price of the acceptance gate actually checking something again.")
        return 0

    print("=== Fine-tuning dataset ===")
    print(f"  frames:            {st['images']}")
    print(f"  label files:       {st['labels']}")
    print(f"  frame+label PAIRS: {st['pairs']}" +
          (f"   (unlabelled: {st['unlabeled']})" if st["unlabeled"] else ""))
    print(f"  labelled by hand:  {st['manual']}")
    print(f"  validation:        {st['val']} frames")
    print(f"  oldest:            {st['oldest']}")
    print(f"  classes:           {', '.join(DATASET_CLASSES.values())}")
    print(f"  folder:            {cfg.dataset_dir}")
    # PAIRS are counted, not images: _split drops frames without labels and
    # then fails on what is left. This used to compare the image count, and
    # the threshold could look passed when training would not have started.
    if st["pairs"] < MIN_IMAGES:
        print(f"\n  Training needs at least {MIN_IMAGES} pairs, currently {st['pairs']}.")
        return 0
    if not st["val_ok"]:
        print(f"\n  The validation split is {st['val']} frames out of {st['pairs']}:")
        print("  the acceptance gate (bowl mAP50) would be decided by exactly these frames.")
        print("  It will be rebuilt when training starts; metrics of past runs are not")
        print("  comparable with future ones after that. Ahead of time, separately from training:")
        print("    python -m pawsight dataset --refreeze-val")
    print("\n  Enough frames. Check the labels and run:")
    print("    python -m pawsight train")
    return 0


def train(cfg: Config) -> int:
    from .train import run

    st = cfg.db_path.parent          # storage/: the volume the brain also sees
    return run(cfg.dataset_dir, cfg.model, device=cfg.device,
               epochs=int(os.getenv("PAWSIGHT_TRAIN_EPOCHS", "80")),
               imgsz=int(os.getenv("PAWSIGHT_TRAIN_IMGSZ", "960")),
               batch=int(os.getenv("PAWSIGHT_TRAIN_BATCH", "16")),
               status_path=st / "train_status.json",
               result_path=st / "train_result.json")


def _start_bot(brain, snapshot) -> None:
    """Accept Telegram commands, if Telegram is configured.

    Lives next to the brain, not the detector: the token and recipient list
    are already here, and replying to a person is the same broadcast, just
    addressed.

    `snapshot` is how to get a frame on command: it differs between monolith
    and split mode, but the bot does not know that (see pawsight/snapshot.py).
    """
    from .bot import CommandBot

    CommandBot(brain.tg, snapshot).start()


def _subscribe_observations(bus, brain, dash):
    """Live boxes go straight to the dashboard; VLM and events go to a separate thread."""
    from .bus import TOPIC_OBS, decode
    from .observations import ObservationWorker

    worker = ObservationWorker(brain.observe)

    def handle(raw: bytes) -> None:
        obs, jpeg = decode(raw)
        dash.on_observation(obs)
        worker.submit(obs, jpeg)

    bus.subscribe(TOPIC_OBS, handle)
    return worker


def main() -> int:
    _log()
    cfg = Config()
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    if mode not in ("brain", "setup", "today", "drinks", "ptz"):
        cfg.resolve_device()

    if mode == "ptz":
        return ptz(sys.argv[2:])
    if mode == "tilt":
        return tilt(cfg, sys.argv[2:])
    if mode == "selftest":
        return selftest(cfg)
    if mode == "today":
        return today(cfg)
    if mode == "dataset":
        return dataset_status(cfg, sys.argv[2:])
    if mode == "train":
        return train(cfg)
    if mode == "drinks":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        return drinks(cfg, n)
    if mode == "batch":
        folder = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("storage/clips")
        from .detector import Detector

        brain = Brain(cfg)
        Detector(cfg, sink=brain.observe).run_batch(folder)
        brain.close(timeout=None)       # wait for reports on the last events
        return 0
    if mode == "live":
        # Monolith: detector and brain in one process. All native.
        from .detector import Detector

        brain = Brain(cfg)
        det = Detector(cfg, sink=brain.observe)
        # No bus here: on-demand frames come from the detector directly.
        _start_bot(brain, det.snapshot_jpeg)
        try:
            det.run()
        finally:
            brain.close()
        return 0
    if mode == "detect":
        # Native half of split mode: publishes observations to MQTT.
        import json as _j
        from . import snapshot
        from .bus import TOPIC_CMD, TOPIC_LOGS, TOPIC_OBS, Bus, MqttLogHandler, encode
        from .config import ROOT
        from .detector import Detector

        from .bus import TOPIC_REPLIES, TOPIC_SNAPSHOT, TOPIC_STATUS
        from .hoststat import StatusPublisher

        bus = Bus(cfg.mqtt_host, cfg.mqtt_port, client_id="pawsight-detector",
                  username=cfg.mqtt_user, password=cfg.mqtt_pass).connect()
        # camera actions go to the dashboard activity feed
        logging.getLogger("pawsight").addHandler(MqttLogHandler(bus))
        _start_logtail(bus, ROOT / "storage" / "detect.log")
        det = Detector(cfg, sink=lambda obs, jpg: bus.publish(TOPIC_OBS, encode(obs, jpg)))

        def on_event(kind: str, payload: dict, jpeg: bytes | None) -> None:
            topic = TOPIC_SNAPSHOT if kind == "snapshot" else TOPIC_REPLIES
            bus.publish(topic, encode(payload, jpeg))
        det.on_event = on_event

        if os.getenv("PAWSIGHT_TRAIN_FROM_DASHBOARD", "1") == "1":
            from .trainer import Trainer

            def train_lines(lines: list[str], n0: int) -> None:
                from .logstream import redact
                bus.publish(TOPIC_LOGS, _j.dumps({
                    "src": "train", "run_id": "train", "n0": n0,
                    "lines": [redact(x) for x in lines]}, ensure_ascii=False).encode())
            det.trainer = Trainer(cfg, on_log=train_lines)

        status = StatusPublisher(
            cfg, lambda d: bus.publish(TOPIC_STATUS, _j.dumps(d, ensure_ascii=False).encode(),
                                       retain=True),
            detector=det)
        status.start()
        det.on_changed = status.poke      # frame saved / training started: report at once

        # commands from the dashboard
        def on_cmd(raw: bytes) -> None:
            d = _j.loads(raw)
            cmd = d.get("cmd")
            if cmd == "tilt_recapture":
                det.request_recapture()
            elif cmd == "tilt_pause" and det.tilt:
                det.tilt.pause()
            elif cmd in ("follow_on", "follow_off") and det.follow:
                det.follow.set_enabled(cmd == "follow_on")
            elif cmd == "status":
                status.poke()
            elif cmd == snapshot.CMD:
                # A person asks for a frame via the bot. Answer right away, not
                # via the loop queue: the detector already has it decoded in the ring.
                snapshot.serve(bus, det, raw)
            else:
                # anything touching the model or disk goes through the detection loop queue
                det.submit(cmd, d)
        bus.subscribe(TOPIC_CMD, on_cmd)
        det.run()
        return 0
    if mode == "setup":
        # Setup page only: no broker, no brain.
        # A separate mode is needed precisely for the first run: without
        # mosquitto, `pawsight brain` fails on Bus.connect() before the
        # dashboard is even created, leaving nowhere to set up the camera.
        from .web import Dashboard

        dash = Dashboard(cfg)
        try:
            dash.run(host=cfg.web_host, port=cfg.web_port)
        except OSError as e:
            if e.errno not in (48, 98):        # EADDRINUSE: macOS / Linux
                raise
            print(f"port {cfg.web_port} is already in use; the brain seems to be running already.")
            print(f"In that case the setup page is already available: http://127.0.0.1:{cfg.web_port}/setup")
            print("If you really need the standalone wizard: ./stop.sh, or")
            print(f"  PAWSIGHT_WEB_PORT={cfg.web_port + 1} .venv/bin/python -m pawsight setup")
            return 1
        return 0
    if mode == "brain":
        # Container half: no YOLO or Metal needed here.
        # MQTT runs on a background thread, the web dashboard on the main one.
        from .bus import (TOPIC_CAMLOG, TOPIC_LOGS, TOPIC_OBS, TOPIC_REPLIES,
                          TOPIC_SNAPSHOT, TOPIC_STATUS, Bus)
        from .snapshot import BusSnapshot
        from .config import ROOT
        from .logstream import BufferHandler, LineParser, LogBuffer, backfill
        from .web import Dashboard

        brain = Brain(cfg)
        bus = Bus(cfg.mqtt_host, cfg.mqtt_port, client_id="pawsight-brain",
                  username=cfg.mqtt_user, password=cfg.mqtt_pass).connect()
        logs = LogBuffer(int(os.getenv("PAWSIGHT_LOG_BUFFER", "3000")))
        # The brain's Store is shared with the dashboard: one DB, one connection, one lock.
        dash = Dashboard(cfg, bus=bus, logs=logs, store=brain.store)

        # On-demand frame: the brain cannot see the camera, so it asks the
        # detector over the bus. This is the SECOND TOPIC_SNAPSHOT subscription
        # (the first is the dashboard's), which is fine: replies differ by the
        # `req` field and each subscriber skips the others'.
        snap = BusSnapshot(bus)
        bus.subscribe(TOPIC_SNAPSHOT, snap.on_reply)
        _start_bot(brain, snap.grab)

        # History: read the file tail ONCE at startup. It is historical, so
        # volume lag does not matter here, unlike the live stream, which
        # arrives over the bus.
        for rec in backfill(ROOT / "storage" / "detect.log"):
            logs.add(rec)

        # Our own records go on the ROOT logger, not on pawsight: one day it is
        # not pawsight that breaks but aiohttp or paho, and that has to be
        # visible in the same place.
        logging.getLogger().addHandler(BufferHandler(logs))

        _parsers: dict[str, LineParser] = {}

        def handle_logs(raw: bytes) -> None:
            import json as _j
            d = _j.loads(raw)
            src = d.get("src", "detect")
            pr = _parsers.get(src)
            if pr is None:
                pr = _parsers[src] = LineParser(src)
            for ln in d.get("lines") or []:
                rec = pr.feed(ln)
                if rec is not None:
                    logs.add(rec)
            # end of batch: release the held record now, otherwise the last line
            # would hang until the next message, which may never come
            tail = pr.flush()
            if tail is not None:
                logs.add(tail)
        bus.subscribe(TOPIC_LOGS, handle_logs)

        def handle_status(raw: bytes) -> None:
            import json as _j
            dash.on_status(_j.loads(raw))
        bus.subscribe(TOPIC_STATUS, handle_status)
        bus.subscribe(TOPIC_REPLIES, dash.on_reply)
        bus.subscribe(TOPIC_SNAPSHOT, dash.on_snapshot)

        observations = _subscribe_observations(bus, brain, dash)

        def handle_camlog(raw: bytes) -> None:
            import json as _j
            e = _j.loads(raw)
            dash.camlog(float(e["ts"]), e.get("kind", "info"), e.get("level", "info"), e.get("text", ""))
        bus.subscribe(TOPIC_CAMLOG, handle_camlog)
        logging.getLogger("pawsight").info("brain listening on %s", TOPIC_OBS)
        try:
            dash.run(host=cfg.web_host, port=cfg.web_port)
        finally:
            observations.stop()
            brain.close()
            bus.close()
        return 0
    print(f"unknown mode: {mode}. Available: live, batch, detect, brain, setup, ptz, tilt, selftest, today, drinks, dataset, train")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
