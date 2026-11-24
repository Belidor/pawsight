"""Detector: RTSP -> YOLO -> tracking -> zones -> OBSERVATIONS.

It must run natively. The reasons were measured, not assumed:
  * Metal/MPS is unavailable to a Linux guest in both Docker and Apple
    container, so inference in a container runs on CPU only;
  * measured on this host: MPS 17.8 ms/frame vs 42.4 ms on CPU.

Observations go out through a sink: either straight into the Brain
(monolith) or to MQTT (split mode, with the brain in a container).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

from .anchors import _dedupe, anchors_for
from .config import Config
from .dataset import Capture
from .labels import DATASET_CLASSES, ds_map_from_names
from .labelcheck import validate_boxes
from .motion import CameraMotion
from .follow import DogFollow
from .patrol import Patrol, Scene
from .ptz_worker import AsyncRotate, PtzWorker
from .tilt import TiltHold
from .zones import _in_polygon, ground_point

log = logging.getLogger("pawsight.detector")

# Class indices depend on the model: COCO has dog=16/person=0, a model
# fine-tuned on our 3 classes has others. So we resolve by NAME, not number.
Sink = Callable[[dict, bytes | None], None]


class Detector:
    def __init__(self, cfg: Config, sink: Sink, track_person: bool = False) -> None:
        self.cfg = cfg
        cfg.resolve_device()
        self.sink = sink
        # Commands from the dashboard. EVERYTHING that touches the model or the
        # disk is executed by the detection loop itself: it owns the model
        # (calling ultralytics from another thread re-initialises the predictor
        # and corrupts BoT-SORT state, and the brain keys all its logic on
        # track_id), and Capture._hashes must have a single writer. Same trick
        # as request_recapture.
        self._cmd_q: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=32)
        self.on_event: Callable[[str, dict, bytes | None], None] = lambda *a: None
        self.on_changed: Callable[[], None] = lambda: None   # "state changed"
        self.trainer = None                                  # set in detect mode
        # Detection paused while fine-tuning runs (checkbox on the dashboard).
        self.paused_detection = False
        self._pause_checked = 0.0
        # Ring of recent frames: the owner's video lags about a second behind
        # (MSE), while the detector works with a frame ~0.2 s old. When the
        # person clicks, they are looking at a frame OLDER than the one the
        # detector would return; at 5 fps that is five frames of motion. The
        # ring lets us step back.
        self._ring: deque = deque(maxlen=6)
        self._snap_req: str | None = None
        self._snaps: dict[str, dict] = {}
        self._hint_model = None                # separate instance for hints
        self._fps_marks: deque = deque(maxlen=40)
        self._started = time.time()
        # People are ALWAYS DETECTED (for the video overlay and the dataset),
        # but never followed and no events are created for them: only dogs go
        # to the brain.
        self.track_person = True   # detect people (overlay/dataset), do not follow
        self.classes: list[int] | None = None
        self._names: dict[int, str] = {}
        self._ds_map: dict[int, int] = {}      # model index -> dataset index

        self.motion = CameraMotion()
        self.capture = Capture(cfg.dataset_dir, enabled=cfg.collect_dataset)
        self.anchors = anchors_for(cfg.zones)
        self._anchor_classes = {z.anchor_class for z in cfg.zones if z.is_anchored}
        # landmark classes for tilt hold (static furniture): always detected;
        # it costs nothing, the class filter runs after inference
        self._landmark_classes = {"tv", "refrigerator", "oven", "couch", "bed", "dining table", "bowl"}

        # Tilt hold: the camera may tilt on its own (auto-tracking), and the
        # owner asked to keep it "as it is now". Checked once a minute.
        # Our own dog following: pan ONLY, never tilt, ignores people. The
        # camera's built-in tracking cannot do that: it tilts and follows any
        # motion.
        self.follow: DogFollow | None = None
        self.patrol: Patrol | None = None
        self._home_seen = 0
        self.tilt: TiltHold | None = None
        self._tilt_next = 0.0
        self._recapture_flag = False
        self._recapture_since = 0.0
        self._recapture_try = 0.0
        if cfg.tilt_hold:
            ptz = None
            worker = None
            try:
                from .ptz import CameraPTZ
                # 2 s instead of 5: a normal reply takes 0.1-0.5 s, a long timeout is just idle time
                ptz = CameraPTZ(timeout=2.0)
                worker = PtzWorker(ptz)
            except Exception as e:
                log.warning("tilt hold without PTZ (measurement only): %s", e)
            self.tilt = TiltHold(cfg.tilt_ref, cfg.infer_h,
                                 AsyncRotate(worker) if worker else None, act=cfg.tilt_act)
            log.info("tilt hold enabled, step ≈ %.0f px in the detection frame", self.tilt.step_px)
            if cfg.home_hold:
                self.patrol = Patrol(
                    idle_sec=cfg.search_idle_sec, step_right=cfg.scan_step_right,
                    step_left=cfg.scan_step_left, steps=cfg.scan_steps,
                    dwell_sec=cfg.scan_dwell_sec, dwell_frames=cfg.scan_dwell_frames,
                    max_pulses=cfg.scan_max_pulses, release_sec=cfg.patrol_release_sec,
                    ref_path=cfg.home_ref,
                )
            self.follow = DogFollow(ptz, cfg.infer_w, enabled=cfg.follow_dog,
                                    search_sec=cfg.search_sec, worker=worker,
                                    patrol=self.patrol)
            # The tilt reference is tied to the pan angle: after a pan, wide-angle
            # distortion produces a false vertical shift. So after every follow
            # step the reference is re-captured on a settled frame; tilt does
            # not change with pan (±2 px per calibration).
            self.follow.on_step = self._after_pan
            self.follow.on_manual = self._manual_reply
            # The reference on disk belongs to an unknown view: the camera may
            # have been panned while the detector was down. Re-capture at startup.
            self.tilt.invalidate("startup")
            self.request_recapture()
            log.info("dog following (pan only): %s; search: %s",
                     "on" if cfg.follow_dog else "off",
                     (f"patrol, home at the bowls, sector {cfg.scan_steps}x"
                      f"{cfg.scan_step_right}/{cfg.scan_step_left} pulses "
                      f"after {cfg.search_idle_sec:.0f} s without the dog")
                     if self.patrol else
                     (f"one step every {cfg.search_sec:.0f} s" if cfg.search_sec > 0 else "off"))

    def _manual_reply(self, rid, direction: str, ok: bool, err: str | None) -> None:
        msg = {"cmd": "ptz_manual", "id": rid, "ok": ok, "dir": direction}
        if not ok:
            msg["error"] = err or "the camera did not respond"
            if "overflow" in msg["error"].lower():
                msg["edge"] = direction
        self.on_event("reply", msg, None)

    def _ptz_manual(self, payload: dict) -> None:
        from .follow import MANUAL_DIRS

        rid = payload.get("id")
        d = str(payload.get("dir") or "")
        if d not in MANUAL_DIRS:
            self.on_event("reply", {"cmd": "ptz_manual", "id": rid, "ok": False,
                                    "error": f"invalid direction: {d[:16]!r}"}, None)
            return
        if self.follow is None or not self.follow.manual(d, rid):
            # The detector does not own the motor: no odometry, nothing to
            # account for; the dashboard pans by itself.
            self.on_event("reply", {"cmd": "ptz_manual", "id": rid, "ok": False,
                                    "no_ptz": True, "error": "the detector has no PTZ"}, None)

    def _poll_ptz(self) -> None:
        """Collect motor command results, even when there are no frames or
        detection is paused: the dashboard is waiting on a manual pan."""
        if self.follow is not None and self.follow.worker is not None:
            self.follow._drain_results(time.time())

    def _after_pan(self) -> None:
        if self.tilt:
            self.tilt.invalidate("pan")
        self.request_recapture()

    def submit(self, cmd: str, payload: dict) -> bool:
        """From the bus thread: put a command on the detection loop's queue."""
        try:
            self._cmd_q.put_nowait((cmd, payload))
            return True
        except queue.Full:
            self.on_event("reply", {"cmd": cmd, "id": payload.get("id"), "ok": False,
                                    "error": "the detector is busy, try again"}, None)
            return False

    def _drain(self) -> None:
        """Drained at three points in the loop, including when there are no
        frames: a stalled stream must not block saving or starting training."""
        while True:
            try:
                cmd, payload = self._cmd_q.get_nowait()
            except queue.Empty:
                self._poll_ptz()
                return
            try:
                self._handle(cmd, payload)
            except Exception as e:
                log.exception("command %s failed", cmd)
                self.on_event("reply", {"cmd": cmd, "id": payload.get("id"),
                                        "ok": False, "error": f"{type(e).__name__}: {e}"}, None)

    def _handle(self, cmd: str, payload: dict) -> None:
        rid = payload.get("id")
        if cmd == "snapshot":
            # The frame is sent not by this command but by the next loop pass,
            # which already has both the image and its detections. Only a flag here.
            self._snap_req = rid
            return
        if cmd == "snapshot_save":
            self._save_labeled(payload)
            return
        if cmd == "ptz_manual":
            self._ptz_manual(payload)
            return
        if cmd == "camera_home":
            ok = bool(self.follow and self.follow.request_home(time.time()))
            self.on_event("reply", {
                "cmd": cmd, "id": rid, "ok": ok,
                **({} if ok else {"error": "return unavailable: patrol or PTZ is off"}),
            }, None)
            return
        if cmd.startswith("dataset_"):
            self._dataset_cmd(cmd, payload)
            return
        if cmd in ("cleanup_preview", "cleanup_apply"):
            from . import cleanup
            from .config import ROOT
            keep = max(0, min(int(payload.get("keep_runs") or 1), 20))
            if cmd == "cleanup_preview":
                r = {"ok": True, "groups": cleanup.host_plan(ROOT, keep)}
            elif self.trainer is not None and self.trainer.check() == "training is already running":
                # runs/ is the working directory of the running job: ultralytics
                # writes checkpoints and results.csv there continuously.
                r = {"ok": False, "error": "fine-tuning is running; runs/ must not be touched now"}
            else:
                r = {"ok": True, "removed": [cleanup.host_apply(ROOT, keep)]}
                self.on_changed()
            self.on_event("reply", {"cmd": cmd, "id": rid, **r}, None)
            return
        if cmd in ("zones_get", "zones_save"):
            self.on_event("reply", {"cmd": cmd, "id": rid, **self._zones_cmd(cmd, payload)}, None)
            return
        if cmd in ("train_start", "train_stop"):
            if self.trainer is None:
                r = {"ok": False, "error": "starting training is disabled"}
            elif cmd == "train_start":
                r = self.trainer.start(bool(payload.get("pause_detection")))
            else:
                r = self.trainer.stop()
            self.on_changed()
            self.on_event("reply", {"cmd": cmd, "id": rid, **r}, None)
            return
        self.on_event("reply", {"cmd": cmd, "id": rid, "ok": False,
                                "error": f"unknown command: {cmd}"}, None)

    # ---- review of accumulated labels ----

    def _dataset_cmd(self, cmd: str, payload: dict) -> None:
        from . import review

        rid = payload.get("id")
        root = self.cfg.dataset_dir

        if cmd == "dataset_list":
            r = review.listing(root, payload.get("order") or "suspicious",
                               min(int(payload.get("limit") or 400), 2000))
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": True, **r}, None)
            return

        name = str(payload.get("name") or "")
        if not review.SAFE_NAME.match(name):
            # The name comes from the browser and the broker is anonymous:
            # without this check it means reading and writing any file on disk.
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": False,
                                    "error": "invalid frame name"}, None)
            return
        img = root / "images" / f"{name}.jpg"
        if not img.exists():
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": False,
                                    "error": "frame is not in the dataset"}, None)
            return

        if cmd == "dataset_frame":
            boxes = review.read_label(root / "labels" / f"{name}.txt")
            self.on_event("snapshot", {"cmd": "dataset_frame", "id": rid, "ok": True,
                                       "name": name, "boxes": boxes},
                          img.read_bytes())
            return

        if cmd == "dataset_hint":
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": True,
                                    "name": name, "boxes": self._hint(img)}, None)
            return

        if cmd == "dataset_delete":
            # The DETECTION LOOP deletes the frame, just as it writes: Capture._hashes
            # and the dataset registries must keep a single writer.
            r = self.capture.delete(name)
            log.info("frame manually deleted from the dataset: %s (%d pairs left)",
                     name, r.get("pairs", 0))
            self.on_changed()
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": True, **r}, None)
            return

        if cmd == "dataset_save":
            boxes = payload.get("boxes") or []
            ok, err = validate_boxes([{**b, "cls": b.get("cls")} for b in boxes])
            if not ok:
                self.on_event("reply", {"cmd": cmd, "id": rid, "ok": False, "error": err}, None)
                return
            # NO SILENT DISAPPEARANCE OF A CLASS. Same trick as when saving a
            # freeze-frame (_save_labeled), but the reference is not a fresh
            # detection but the labels already stored next to the frame: for a
            # dataset frame that is the model's opinion of it. Removing the LAST
            # `bowl` or `person` box teaches the model to treat them as
            # background, and every zone in the project relies on the bowl
            # class; this breaks silently. The dog is excluded: a frame without
            # the dog is a legitimate negative example, which is what the
            # "Clear all boxes" button exists for.
            if not payload.get("confirm_empty"):
                had = {b["cls"] for b in review.read_label(root / "labels" / f"{name}.txt")}
                given = {b.get("cls") for b in boxes}
                missing = sorted(had - given - {"dog"})
                if missing:
                    self.on_event("reply", {
                        "cmd": cmd, "id": rid, "ok": False,
                        "need_confirm": True, "missing": missing,
                        "error": "the frame's labels had " + ", ".join(missing) +
                                 ", and now they are gone. An unlabelled class is learned "
                                 "as background, and all zones rely on the bowl."}, None)
                    return
            n = self.capture.relabel(name, boxes)
            log.info("labels manually corrected: %s (%d boxes)", name, n)
            self.on_changed()
            self.on_event("reply", {"cmd": cmd, "id": rid, "ok": True,
                                    "name": name, "labels": n}, None)
            return

        self.on_event("reply", {"cmd": cmd, "id": rid, "ok": False,
                                "error": f"unknown command: {cmd}"}, None)

    def _hint(self, img_path) -> list[dict]:
        """Re-run the frame at a LOW threshold to hint at what was missed.

        The model is weak on bowls (bowl mAP50 = 0.597 for the manually
        accepted one), and most often it simply does not see them. At a 0.05
        threshold candidates show up, and a human decides.

        A SEPARATE model instance, not the working one: in ultralytics,
        predict and track share self.predictor, and a predict call would reset
        BoT-SORT state. The brain keys all its logic on track_id, so we would
        get duplicate events and notifications that reproduce only every other
        time. Twenty megabytes of memory are cheaper than debugging that.
        """
        from ultralytics import YOLO

        if self._hint_model is None:
            self._hint_model = YOLO(self.cfg.model)
        r = self._hint_model.predict(str(img_path), device=self.cfg.device,
                                     imgsz=self.cfg.imgsz, conf=0.05,
                                     classes=sorted(self._ds_map) or None,
                                     verbose=False)[0]
        h, w = r.orig_shape
        out = []
        for b, c, s in zip(r.boxes.xyxy, r.boxes.cls, r.boxes.conf):
            did = self._ds_map.get(int(c))
            if did is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in b)
            out.append({"cls": DATASET_CLASSES[did], "conf": round(float(s), 3),
                        "cx": round(((x1 + x2) / 2) / w, 6),
                        "cy": round(((y1 + y2) / 2) / h, 6),
                        "w": round((x2 - x1) / w, 6), "h": round((y2 - y1) / h, 6)})
        return out

    # ---- freeze-frame for manual labelling ----

    def _norm_boxes(self, raw: list, w: int, h: int) -> list[dict]:
        """Boxes normalised RIGHT AWAY: cx,cy,w,h in [0,1] is already a ready
        YOLO label line. No pixel round trip through the browser and back, and
        no resolution constant duplicated at both ends."""
        out = []
        for cid, conf, (x1, y1, x2, y2) in raw:
            did = self._ds_map.get(cid)
            if did is None:
                continue
            out.append({
                "cls": DATASET_CLASSES[did], "conf": round(float(conf), 3),
                "cx": round(((x1 + x2) / 2) / w, 6), "cy": round(((y1 + y2) / 2) / h, 6),
                "w": round((x2 - x1) / w, 6), "h": round((y2 - y1) / h, 6),
            })
        return out

    def snapshot_jpeg(self, stale_after: float = 3.0) -> tuple[bytes | None, str | None]:
        """The "right now" frame a person asked for: (jpeg, note).

        We do NOT open a second camera connection for a snapshot: at 2K over
        P2P an extra consumer means receive-buffer overflows and video gaps
        for everyone (docs/architecture.md). We take the latest frame from the ring
        that the detection loop fills anyway.

        The frame's age is checked and goes into the note: during a stream
        gap (the Xiaomi camera freezes for 6-19 s after a pan) the ring holds
        whatever arrived last, and passing it off as "now" would be a lie,
        which is exactly what the person is asking about.
        """
        if not self._ring:
            return None, "no frames: the stream is silent"
        import cv2

        ts, frame, _ = self._ring[-1]
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            return None, "could not encode the frame as JPEG"
        age = time.time() - ts
        note = (f"frame is {age:.0f} s old; the camera stream is freezing"
                if age > stale_after else None)
        return buf.tobytes(), note

    def _zones_cmd(self, cmd: str, payload: dict) -> dict:
        """Zone editor: the detector owns zones.yaml (it runs on the host in
        both stacks), so reading and writing it happens here."""
        import base64

        import cv2
        import yaml

        from . import zone_edit
        from .config import ROOT

        path = ROOT / "zones.yaml"
        if not self._ring:
            return {"ok": False, "error": "no frames: the stream is silent"}
        ts, frame, raw = self._ring[-1]
        h, w = frame.shape[:2]
        if cmd == "zones_save":
            try:
                new = zone_edit.save(path, payload.get("edit") or {}, (w, h))
            except zone_edit.ZoneEditError as e:
                return {"ok": False, "error": str(e)}
            self.cfg.apply_zones(new)
            self.anchors = anchors_for(self.cfg.zones)
            self._anchor_classes = {z.anchor_class for z in self.cfg.zones if z.is_anchored}
            log.info("zones saved from the editor: %s", ", ".join(new["zones"]))
            return {"ok": True, "raw": new}
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            return {"ok": False, "error": "could not encode the frame as JPEG"}
        bowls = [list(map(float, b)) for c, s, b in raw
                 if self._names.get(c) == "bowl" and s >= 0.20]
        pair = zone_edit.pair_point(bowls)
        # Where each zone lies on THIS frame, computed by the same code the
        # detector counts with, so the editor shows what the detector sees.
        dets = [(self._names.get(c, ""), s, b) for c, s, b in raw]
        drawn = {}
        for z in self.cfg.zones:
            if z.is_anchored:
                a = anchors_for([z])[z.name]
                a.update(dets, ts)
                poly = a.polygon(ts)
                drawn[z.name] = [list(p) for p in poly] if poly else None
            else:
                drawn[z.name] = [list(map(int, p)) for p in z.polygon]
        return {"ok": True, **zone_edit.describe(yaml.safe_load(text) or {}),
                "drawn": drawn,
                "size": [w, h], "age": round(time.time() - ts, 1),
                "frame_b64": base64.b64encode(buf.tobytes()).decode(),
                "bowls": bowls, "pair": list(pair) if pair else None}

    def _emit_snapshot(self, req_id: str) -> None:
        import cv2

        if not self._ring:
            self.on_event("reply", {"cmd": "snapshot", "id": req_id, "ok": False,
                                    "error": "no frames: the stream is silent"}, None)
            return
        frames = []
        keep = []
        for ts, frame, raw in list(self._ring):
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                continue
            jpeg = buf.tobytes()
            h, w = frame.shape[:2]
            frames.append({"ts": ts, "size": [w, h],
                           "boxes": self._norm_boxes(raw, w, h)})
            keep.append(jpeg)
        if not frames:
            self.on_event("reply", {"cmd": "snapshot", "id": req_id, "ok": False,
                                    "error": "the frame failed to encode"}, None)
            return
        # Keep EXACTLY these bytes: they are what gets written to disk later,
        # with no re-encoding, so the file matches pixel for pixel what the
        # person labelled.
        self._snaps[req_id] = {"at": time.time(), "jpegs": keep,
                               "meta": frames, "raw": [r for _, _, r in self._ring]}
        for old_id in sorted(self._snaps, key=lambda k: self._snaps[k]["at"])[:-4]:
            self._snaps.pop(old_id, None)
        self.on_event("snapshot", {
            "cmd": "snapshot", "id": req_id, "ok": True, "ts": time.time(),
            "classes": dict(DATASET_CLASSES),
            "frames": [dict(f, jpeg=None) for f in frames],
        }, None)
        # JPEGs go out as separate messages: one with six images would be half
        # a megabyte of base64 in a single packet.
        for i, jpeg in enumerate(keep):
            self.on_event("snapshot", {"cmd": "snapshot_frame", "id": req_id,
                                       "ok": True, "n": i}, jpeg)

    def _save_labeled(self, payload: dict) -> None:
        rid, sid = payload.get("id"), payload.get("snap")
        snap = self._snaps.get(sid)
        if snap is None:
            self.on_event("reply", {"cmd": "snapshot_save", "id": rid, "ok": False,
                                    "error": "the frame is stale; take a new one"}, None)
            return
        n = int(payload.get("frame") or 0)
        if not (0 <= n < len(snap["jpegs"])):
            n = len(snap["jpegs"]) - 1
        boxes = payload.get("boxes") or []
        ok, err = validate_boxes(boxes)
        if not ok:
            self.on_event("reply", {"cmd": "snapshot_save", "id": rid,
                                    "ok": False, "error": err}, None)
            return

        # NO EMPTY LABELS WHEN OBJECTS ARE VISIBLE.
        # An empty file is a legitimate negative example. But if the model sees
        # a bowl or a person in the frame and the labels are empty, that file
        # teaches it to treat them as background. ALL zones in the project rely
        # on the bowl class, and this breaks silently: the dog will still be
        # detected perfectly.
        if not payload.get("confirm_empty"):
            seen = {DATASET_CLASSES[self._ds_map[c]]
                    for c, _, _ in snap["raw"][n] if c in self._ds_map}
            given = {b.get("cls") for b in boxes}
            missing = sorted(seen - given - {"dog"})
            if missing:
                self.on_event("reply", {
                    "cmd": "snapshot_save", "id": rid, "ok": False,
                    "need_confirm": True, "missing": missing,
                    "error": "the model sees " + ", ".join(missing) +
                             " in the frame, but the labels do not. An unlabelled class "
                             "is learned as background, and all zones rely on the bowl."}, None)
                return

        try:
            name = self.capture.save_labeled(snap["jpegs"][n], boxes, time.time())
        except Exception as e:
            self.on_event("reply", {"cmd": "snapshot_save", "id": rid, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"}, None)
            return
        self._snaps.pop(sid, None)
        st = self.capture.stats()
        log.info("manually added to the dataset: %s (%d boxes)", name, len(boxes))
        self.on_changed()
        self.on_event("reply", {"cmd": "snapshot_save", "id": rid, "ok": True,
                                "image": name, "labels": len(boxes),
                                "pairs": st.get("pairs", st.get("images"))}, None)

    @property
    def fps(self) -> float:
        if len(self._fps_marks) < 2:
            return 0.0
        span = self._fps_marks[-1] - self._fps_marks[0]
        return (len(self._fps_marks) - 1) / span if span > 0 else 0.0

    def request_recapture(self) -> None:
        """Called from another thread (the bus). The reference is re-captured not
        on the next frame but on the first SETTLED one: the motor is still
        moving and the stream lags ~1 s, so capturing on a timer would record
        a blurred in-between frame."""
        self._recapture_flag = True
        self._recapture_since = time.time()

    def _tilt_tick(self, frame, settled: bool, now: float, anchors: list | None = None) -> None:
        if self.tilt is None:
            return
        if self.patrol is not None and self.patrol.active:
            return          # re-capture the reference once at home, not after every pulse
        if self._recapture_flag:
            waited = now - self._recapture_since
            if (settled or waited > 12) and now - self._recapture_try >= 2.0:
                self._recapture_try = now
                ok = self.tilt.recapture(frame, "after pan/manual tilt"
                                         + ("" if settled else " (camera did not settle within 12 s)"),
                                         anchors=anchors)
                if ok or waited > 12:
                    self._recapture_flag = False
            return
        if now < self._tilt_next:
            return
        # The frame series is collected HERE, frame by frame; the decision comes
        # once the series agrees. The interval counts from the decision.
        st = self.tilt.feed(frame, settled, self.motion.moving, now, anchors=anchors)
        if st is None:
            return
        self._tilt_next = now + self.cfg.tilt_interval
        log.info("tilt: %s", st)

    def _home_scene(self, raw, settled: bool, now: float) -> Scene:
        """What the patrol sees in the frame: the centre of the bowl pair and the fridge.

        Bowls come from the same anchor the zones use, with all of its "two
        of them side by side" checking; otherwise a random cup could become
        home. The fridge is a secondary landmark: it is bigger and visible from
        further away, but the fine-tuned three-class model has no such class
        at all (AGENTS.md, 7).
        """
        w = self.cfg.infer_w
        bowls = None
        anchor = self.anchors.get("bowls") or next(
            (a for a in self.anchors.values() if a.anchor_class == "bowl"), None)
        # Take the centre of the pair's BOX, not of the zone polygon: the floor
        # shape stretches from the bowls towards the rug, and its centre is not
        # where the bowls stand.
        box = anchor.anchor_box(now, current_only=True) if anchor else None
        if box:
            bowls = ((box[0] + box[2]) / 2.0 - w / 2.0) / w
        # There may be several candidates, and the biggest/most confident is not
        # always the real one (measured in docs/architecture.md). Merge duplicates of
        # one object and pass them all on: the patrol chooses, since it knows
        # where the landmark used to be.
        cands = sorted(((s, b) for c, s, b in raw
                        if self._names.get(c) == "refrigerator" and s >= 0.25),
                       key=lambda z: -z[0])
        conf = {b: s for s, b in cands}
        kept = _dedupe([b for _, b in cands])
        fridges = tuple(((b[0] + b[2]) / 2.0 - w / 2.0) / w
                        for b in sorted(kept, key=lambda b: -conf.get(b, 0.0)))
        return Scene(bowls=bowls, fridges=fridges, settled=settled)

    def _ensure_classes(self, model) -> None:
        self._names = dict(model.names)
        inv = {v: k for k, v in self._names.items()}
        # dog/person indices in THIS model (COCO or fine-tuned)
        self.DOG = inv.get("dog")
        self.PERSON = inv.get("person")
        if self.DOG is None:
            log.error("the model has no 'dog' class; dog detection is impossible")
        if self.patrol is not None and "refrigerator" not in inv:
            log.info("patrol: the model has no 'refrigerator' class; the second home "
                     "landmark is unavailable, return uses the bowls and the pulse count")
        avail_lm = [c for c in self._landmark_classes if c in inv]
        if not avail_lm or (len(avail_lm) == 1 and avail_lm[0] == "bowl"):
            log.warning("the model has no furniture classes (%s); tilt is held by the bowls only",
                        sorted(self._landmark_classes - {"bowl"}))
        missing = [c for c in self._anchor_classes if c not in inv]
        if missing:
            log.warning("anchors missing from the labelmap: %s", missing)
        extra = [inv[c] for c in (self._anchor_classes | self._landmark_classes) if c in inv]
        base = [i for i in (self.DOG, self.PERSON) if i is not None]
        self.classes = sorted(set(base) | set(extra))
        # Mapping to DATASET classes: also by name, same logic.
        self._ds_map = ds_map_from_names(self._names)
        log.info("detection classes: %s", {i: self._names[i] for i in self.classes})

    def _zones_at(self, box, ts: float) -> list[str]:
        pt = ground_point(box)
        out = []
        for z in self.cfg.zones:
            poly = self.anchors[z.name].polygon(ts) if z.is_anchored else z.polygon
            if poly and _in_polygon(pt, poly):
                out.append(z.name)
        return out

    def _detection_paused(self, now: float) -> bool:
        """Ask the trainer once a second; log a change and update the status at once."""
        if now - self._pause_checked >= 1.0:
            self._pause_checked = now
            paused = bool(self.trainer is not None and self.trainer.detection_paused())
            if paused != self.paused_detection:
                self.paused_detection = paused
                log.warning("detection %s", "paused for fine-tuning"
                            if paused else "resumed")
                self.on_changed()
        return self.paused_detection

    def _idle_frame(self, frame, now: float) -> None:
        """A frame without detection: only the snapshot ring."""
        self._ring.append((now, frame, []))
        if self._snap_req:
            req, self._snap_req = self._snap_req, None
            try:
                self._emit_snapshot(req)
            except Exception:
                log.exception("freeze-frame was not built")

    def _observe(self, res, frame, now: float, jpeg_fn) -> None:
        boxes = getattr(res, "boxes", None)
        if not self._names:
            self._names = dict(res.names)

        # Camera motion is estimated from the background: dog and person boxes
        # are excluded, otherwise a large figure up close looks like a pan and
        # everything freezes.
        fg = []
        if boxes is not None and boxes.cls is not None:
            fg = [[float(v) for v in b] for b, c in zip(boxes.xyxy, boxes.cls)
                  if int(c) in (self.DOG, self.PERSON)]
        moving = self.motion.update(frame, now, exclude_boxes=fg)
        settled = self.motion.settled(now)

        raw = []
        if boxes is not None and boxes.cls is not None:
            raw = [
                (int(c), float(s), tuple(float(v) for v in b))
                for b, c, s in zip(boxes.xyxy, boxes.cls, boxes.conf)
            ]

        # re-anchor the zones to the landmarks in the current frame
        if self._anchor_classes and raw:
            dets = [(self._names.get(c, ""), s, b) for c, s, b in raw]
            for a in self.anchors.values():
                a.update(dets, now)

        # frames for fine-tuning: only once the picture has settled
        if raw and settled:
            self.capture.maybe_save(frame, raw, now, self._ds_map)

        # Ring for manual snapshots. Keep ARRAYS, not JPEG: encoding every frame
        # for nothing costs more than encoding six on demand.
        self._ring.append((now, frame, raw))
        if self._snap_req:
            req, self._snap_req = self._snap_req, None
            try:
                self._emit_snapshot(req)
            except Exception as e:
                log.exception("freeze-frame failed to build")
                self.on_event("reply", {"cmd": "snapshot", "id": req, "ok": False,
                                        "error": f"{type(e).__name__}: {e}"}, None)

        dogs, persons = [], []
        # A "person" inside a TV screen is the TV, not a person.
        tvs = [b for c, s, b in raw if self._names.get(c) == "tv"]

        def _inside_tv(b):
            return any(b[0] >= t[0] - 5 and b[1] >= t[1] - 5 and b[2] <= t[2] + 5 and b[3] <= t[3] + 5
                       for t in tvs)

        if boxes is not None and boxes.cls is not None:
            for b, c, s in zip(boxes.xyxy, boxes.cls, boxes.conf):
                if int(c) == self.PERSON:
                    bb = [round(float(v), 1) for v in b]
                    if _inside_tv(bb):
                        continue
                    persons.append({"box": bb, "conf": round(float(s), 3)})
        if boxes is not None and boxes.id is not None:
            for b, c, s, i in zip(boxes.xyxy, boxes.cls, boxes.conf, boxes.id):
                if int(c) != self.DOG:
                    continue
                box = tuple(float(v) for v in b)
                dogs.append({
                    "track_id": int(i),
                    "box": [round(v, 1) for v in box],
                    "conf": round(float(s), 3),
                    "zones": self._zones_at(box, now),
                })

        # The video overlay needs more than dogs: the dashboard draws the current
        # zone polygons (they move with the camera) and the detected bowls.
        zone_polys = {}
        for z in self.cfg.zones:
            poly = self.anchors[z.name].polygon(now) if z.is_anchored else z.polygon
            if poly:
                zone_polys[z.name] = {"label": z.label,
                                      "poly": [[int(x), int(y)] for x, y in poly]}
        anchors = [
            {"cls": self._names.get(c, str(c)), "conf": round(s, 2),
             "box": [round(v, 1) for v in b]}
            for c, s, b in raw if self._names.get(c) in (self._anchor_classes | self._landmark_classes)
        ]
        self._tilt_tick(frame, settled, now, anchors)

        if self.follow:
            # Raw dog boxes (no track required), only for the loss log: they show
            # whether the dog "left" or the detector/tracker failed to confirm it.
            raw_dogs = [(s, b[0], b[2]) for c, s, b in raw if c == self.DOG]
            self.follow.tick(dogs, moving, now, self._home_scene(raw, settled, now),
                             raw_dogs=raw_dogs)
        obs = {
            "ts": now,
            "camera_moving": moving,
            "settled": settled,
            "dogs": dogs,
            "persons": persons,
            "zones": zone_polys,
            "anchors": anchors,
            "size": [self.cfg.infer_w, self.cfg.infer_h],
            "tilt": self.tilt.last if self.tilt else None,
            "follow": self.follow.state() if self.follow else None,
        }
        if self.patrol is not None and self.patrol.home_visits != self._home_seen:
            self._home_seen = self.patrol.home_visits
            self._after_pan()      # at home: re-capture the tilt reference ONCE per sweep
        self.sink(obs, jpeg_fn() if dogs else None)

    def run_batch(self, folder) -> None:
        """Process a folder of mp4 files (e.g. a NAS export). Same pipeline, without RTSP."""
        import cv2
        from pathlib import Path
        from ultralytics import YOLO

        model = YOLO(self.cfg.model)
        self._ensure_classes(model)
        files = sorted(Path(folder).glob("*.mp4"))
        log.info("batch: %d files in %s", len(files), folder)
        for f in files:
            log.info("processing %s", f.name)
            base = f.stat().st_mtime
            step = 1.0 / max(self.cfg.detect_fps, 0.1)
            for i, res in enumerate(model.track(
                source=str(f), stream=True, persist=True, classes=self.classes,
                conf=self.cfg.conf, imgsz=self.cfg.imgsz, device=self.cfg.device,
                tracker=self.cfg.tracker, verbose=False,
            )):
                frame = cv2.resize(res.orig_img, (self.cfg.infer_w, self.cfg.infer_h),
                                   interpolation=cv2.INTER_AREA) \
                    if res.orig_img.shape[1] != self.cfg.infer_w else res.orig_img

                def _jpeg():
                    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    return buf.tobytes() if ok else None

                self._observe(res, frame, base + i * step, _jpeg)

    class _Reader:
        """Frames are read in a separate thread. During a stream gap (the camera
        freezes for 6-19 s after a pan) grab() blocks, and without this the
        whole loop would freeze: search, following, status. Now the loop knows
        there are no frames and acts deliberately."""

        def __init__(self, url: str, decode_interval: float) -> None:
            import cv2
            self.url = url
            self.decode_interval = decode_interval
            self.cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            self.ok = self.cap.isOpened()
            self.frame = None
            self.frame_ts = 0.0
            self.seq = 0
            self.dead = False
            self._lock = threading.Lock()
            if self.ok:
                threading.Thread(target=self._run, name="rtsp-reader", daemon=True).start()

        def _run(self) -> None:
            # grab() takes the stream at full speed (draining the socket),
            # retrieve() decodes only the 4-5 frames/s we need. Decoding all 20
            # is not an option: after a burst of I-frames the reader fell behind,
            # the TCP buffer filled up, go2rtc blocked on write and dropped
            # chunks (1039 per hour).
            next_decode = 0.0
            while not self.dead:
                if not self.cap.grab():
                    self.dead = True
                    break
                now = time.time()
                if now < next_decode:
                    continue
                ok, f = self.cap.retrieve()
                if ok:
                    next_decode = now + self.decode_interval
                    with self._lock:
                        self.frame, self.frame_ts, self.seq = f, now, self.seq + 1

        def latest(self):
            with self._lock:
                return self.frame, self.frame_ts, self.seq

        def close(self) -> None:
            self.dead = True
            try:
                self.cap.release()
            except Exception:
                pass

    def run(self) -> None:
        import cv2
        from ultralytics import YOLO

        model = YOLO(self.cfg.model)
        self._ensure_classes(model)
        interval = 1.0 / max(self.cfg.detect_fps, 0.1)
        log.info(
            "detector: %s -> %dx%d | %s device=%s tracker=%s %.0f fps",
            self.cfg.detect_rtsp, self.cfg.infer_w, self.cfg.infer_h,
            self.cfg.model, self.cfg.device, self.cfg.tracker, self.cfg.detect_fps,
        )

        backoff = 2
        beat, processed = 0.0, 0
        while True:
            self._drain()
            reader = self._Reader(self.cfg.detect_rtsp, interval * 0.9)
            if not reader.ok:
                log.warning("stream did not open, retrying in %ss", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            log.info("stream open")
            backoff, next_at, last_seq = 2, 0.0, -1
            stall_since = 0.0
            if self.follow:
                self.follow.stream_stalled(False)   # new reader: the old one's gap is closed
            try:
                while not reader.dead:
                    now = time.time()
                    self._drain()
                    if now < next_at:
                        time.sleep(0.02)
                        continue
                    frame, fts, seq = reader.latest()
                    if frame is None or seq == last_seq:
                        # No frames: a stream gap. Wait, but do not freeze entirely;
                        # tell following/search that we are blind.
                        if frame is not None and now - fts > 1.0:
                            if not stall_since:
                                stall_since = fts
                                if self.follow:
                                    self.follow.stream_stalled(True)
                        time.sleep(0.05)
                        continue
                    if stall_since:
                        log.warning("stream froze for %.1f s", now - stall_since)
                        stall_since = 0.0
                        if self.follow:
                            self.follow.stream_stalled(False)
                    last_seq = seq
                    next_at = now + interval

                    small = cv2.resize(frame, (self.cfg.infer_w, self.cfg.infer_h),
                                       interpolation=cv2.INTER_AREA)
                    if self._detection_paused(now):
                        # Keep reading the stream (otherwise go2rtc's receive
                        # buffer overflows) but do not call the model. The frame
                        # ring stays live: /snap and freeze-frame work, just
                        # without boxes.
                        self._idle_frame(small, now)
                        continue
                    res = model.track(
                        small, persist=True, classes=self.classes, conf=self.cfg.conf,
                        imgsz=self.cfg.imgsz, device=self.cfg.device,
                        tracker=self.cfg.tracker, verbose=False,
                    )[0]

                    def _jpeg():
                        ok2, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])
                        return buf.tobytes() if ok2 else None

                    try:
                        self._observe(res, small, now, _jpeg)
                    except Exception:
                        log.exception("frame processing error (not the stream's fault)")

                    processed += 1
                    self._fps_marks.append(now)
                    if now - beat > 300:
                        if beat:
                            log.info("alive: %.1f frames/s, camera %s", processed / (now - beat),
                                     "moving" if self.motion.moving else "still")
                        beat, processed = now, 0
                log.warning("stream dropped, reconnecting in %ss", backoff)
            except KeyboardInterrupt:
                reader.close()
                log.info("stopped by the user")
                return
            except Exception as e:
                log.warning("loop error (%s: %s), reconnecting in %ss",
                            type(e).__name__, e, backoff)
            finally:
                reader.close()
                self._drain()
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
