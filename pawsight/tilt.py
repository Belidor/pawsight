"""Tilt hold: keeping the camera's height (tilt) against a reference frame.

The camera has no absolute position, only relative steps. So "always keep the
height as it is now" is implemented as a closed loop: compare the current frame
with the reference, and if the scene has moved vertically by more than half a
step, bring it back by a whole number of steps.

Calibration on this camera (2026-09-06): one tilt step = 157 px vertically in a
2304x1296 frame; the reverse step returns to within 1 px.

WHAT WE MEASURE WITH. Not optical flow against the reference frame: twice in one
day it produced consistent garbage (-214 and -144 px on a stationary camera: the
dog covered the scene; the reference was taken at a different pan angle), and
the loop made false steps. We measure by ANCHORS: static furniture that YOLO
finds independently: TV, fridge, oven, couch, bed, table, the pair of bowls.
When capturing the reference we remember where each anchor is; when measuring
we compare the vertical position of the same classes. These are physical
objects: their height in the frame depends on tilt and hardly at all on pan
(+-2 px by calibration). If the camera has panned to another room and none of
the reference anchors is visible, there is no measurement and the loop does not
move the camera.

Safety rules (the loop moves the owner's camera on its own):
  * dead zone of +-half a step: jitter produces no motion;
  * at most MAX_STEPS per correction;
  * only when the camera is still (the motion detector says "settled");
  * only if the scene is recognised: at least MIN_POINTS matched points,
    otherwise the camera is looking elsewhere and there is nothing to fix;
  * after a correction and after a manual tilt: a COOLDOWN pause.
A manual tilt by the owner redefines the reference: "as it is now" is their decision.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("pawsight.tilt")

STEP_PX_FULL = 157.0          # px per step in a 2304x1296 frame (measured)
FULL_H = 1296
MIN_POINTS = 60
LANDMARK_CLASSES = {"tv", "refrigerator", "oven", "couch", "bed", "dining table", "bowl"}
MIN_COMMON_LANDMARKS = 1
MIN_MATCH_RATIO = 0.6     # fraction of matched reference points; below it the frame is wrong (dog covering, decoder glitch)
SERIES_N = 5              # consistent frames in a row needed to trust a measurement
SERIES_TOL_PX = 25.0      # spread within a series (in the detection frame)
JUMP_PX = 60.0            # a jump from the last accepted value without camera motion is not physics but garbage
MAX_STEPS = 4
COOLDOWN_SEC = 30.0


def steps_for(dy: float, step_px: float, max_steps: int = MAX_STEPS) -> int:
    """How many steps, and in which direction, are needed to bring dy back to zero.

    Positive result = 'down' steps (the scene moved down => the camera is
    tilted up relative to the reference), negative = 'up'.
    Zero inside the dead zone of +-half a step.
    """
    if abs(dy) < step_px / 2:
        return 0
    n = int(round(dy / step_px))
    return max(-max_steps, min(max_steps, n))


class TiltHold:
    def __init__(self, ref_path: Path, frame_h: int, ptz=None, act: bool = True) -> None:
        self.ref_path = ref_path
        self.targets: dict[str, float] = {}     # reference anchors: class -> y (detection frame)
        self.act = act                    # False = only measure and log
        self._series: list[float] = []
        self._accepted: float | None = None
        self._pending_verify: float | None = None   # |dy| before the last step: verify that it got smaller
        self._rejects = 0                            # jumps rejected in a row
        self.frozen = False
        self.meta_path = ref_path.with_suffix(".json")
        self.frame_h = frame_h
        self.step_px = STEP_PX_FULL * frame_h / FULL_H
        self.ptz = ptz
        self._ref: np.ndarray | None = None
        self._pts: np.ndarray | None = None
        self._cooldown_until = 0.0
        self.last: dict = {"status": "no reference"}
        self._load()

    # ---- reference ----

    def _load(self) -> None:
        if not self.ref_path.exists():
            self._ref = None
            return
        img = cv2.imread(str(self.ref_path))
        if img is None:
            self._ref = None
            return
        w = int(img.shape[1] * self.frame_h / img.shape[0])
        g = cv2.cvtColor(cv2.resize(img, (w, self.frame_h), interpolation=cv2.INTER_AREA),
                         cv2.COLOR_BGR2GRAY)
        self._ref = g
        self._pts = cv2.goodFeaturesToTrack(g, maxCorners=500, qualityLevel=0.01, minDistance=10)
        if self.meta_path.exists():
            try:
                self.targets = json.loads(self.meta_path.read_text()).get("landmarks") or {}
            except Exception:
                self.targets = {}
        self.last = {"status": "reference loaded" if self.targets else "reference without anchors"}

    @staticmethod
    def landmarks(anchors: list | None) -> dict[str, float]:
        """Class -> vertical reference coordinate (detection frame).

        For large furniture take the box centre (the bottom is often cut off by
        the frame edge). Bowls count as an anchor only as a PAIR: a single
        "bowl" is any round object on a table (verified in the living room)."""
        out: dict[str, list[float]] = {}
        for a in anchors or []:
            cls, b = a.get("cls"), a.get("box")
            if not cls or not b:
                continue
            if cls == "bowl":
                out.setdefault("bowl", []).append(b[3])
            elif cls in LANDMARK_CLASSES and (b[3] - b[1]) >= 40:
                out.setdefault(cls, []).append((b[1] + b[3]) / 2.0)
        res = {}
        for cls, ys in out.items():
            if cls == "bowl":
                if len(ys) >= 2:
                    res["bowl"] = float(np.median(ys))
            else:
                res[cls] = float(np.median(ys))
        return res

    def bowls_y(self, anchors: list | None) -> float | None:
        return self.landmarks(anchors).get("bowl")

    def recapture(self, frame_bgr, reason: str = "manual tilt", anchors: list | None = None) -> bool:
        """The current frame becomes the new reference. Without anchors in the
        frame no reference is taken: there would be nothing to measure by.
        Returns True if captured."""
        lm = self.landmarks(anchors)
        if not lm:
            self.last = {"status": "reference not captured: no anchors visible"}
            log.warning("tilt: reference not recaptured (%s): no anchors in frame", reason)
            return False
        self.ref_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.ref_path), frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 92])
        self.meta_path.write_text(json.dumps(
            {"captured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "ts": time.time(),
             "reason": reason, "landmarks": lm, "frame_h": self.frame_h},
            ensure_ascii=False, indent=1))
        self._load()
        # the old "accepted" offset belonged to the previous reference
        self._accepted = None
        self._series.clear()
        self._rejects = 0
        self._cooldown_until = time.time() + COOLDOWN_SEC
        self.targets = lm
        log.info("tilt: reference recaptured (%s): anchors %s", reason,
                 {k: round(v) for k, v in lm.items()})
        return True

    def invalidate(self, reason: str = "pan") -> None:
        """The reference belongs to the previous view: after a pan, comparison is invalid.

        Furniture of the same class in another room stands at a different
        height, and comparing would give a false tilt. Until a successful
        recapture on the new view there are no measurements; the height itself
        is physically preserved, since a pan does not change it.
        """
        self.targets = {}
        self._series.clear()
        self._accepted = None
        self._pending_verify = None
        self.last = {"status": f"after {reason}: waiting for a new reference"}

    def pause(self, sec: float = COOLDOWN_SEC) -> None:
        self._cooldown_until = max(self._cooldown_until, time.time() + sec)

    # ---- measurement ----

    def measure(self, frame_bgr) -> tuple[float | None, float | None, int]:
        """(dx, dy, points). dy>0: the scene moved down, i.e. the camera is above the reference."""
        if self._ref is None or self._pts is None:
            return None, None, 0
        g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if g.shape != self._ref.shape:
            g = cv2.resize(g, (self._ref.shape[1], self._ref.shape[0]))
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(self._ref, g, self._pts, None,
                                              winSize=(31, 31), maxLevel=5)
        if nxt is None or st is None:
            return None, None, 0
        ok = st.ravel() == 1
        n = int(ok.sum())
        if n < MIN_POINTS:
            return None, None, n
        if n < MIN_MATCH_RATIO * len(self._pts):
            return None, None, n
        d = (nxt[ok] - self._pts[ok]).reshape(-1, 2)
        return float(np.median(d[:, 0])), float(np.median(d[:, 1])), n

    # ---- loop ----

    def feed(self, frame_bgr, settled: bool, moving: bool, now: float | None = None,
             anchors: list | None = None) -> dict | None:
        """Accumulates a series of measurements frame by frame; decides only on a consistent series.

        A single frame does not deserve trust: a single measurement was seen to
        give -214 px on a stationary camera (the dog covered the scene), and
        the loop made a false step. Returns the decision (dict) once a series is
        collected, otherwise None.
        """
        now = now or time.time()
        if moving:
            self._series.clear()
            return None
        if not self.targets:
            if not self.last.get("status", "").startswith("after "):
                self.last = {"status": "no reference with anchors, waiting for recapture"}
            return None
        lm = self.landmarks(anchors)
        common = [c for c in lm if c in self.targets]
        if len(common) < MIN_COMMON_LANDMARKS:
            self._series.clear()
            self.last = {"status": "reference anchors not in frame, not moving",
                         "seen": sorted(lm), "ref": sorted(self.targets)}
            return None
        dy = float(np.median([lm[c] - self.targets[c] for c in common]))   # >0: scene lower => camera higher => down
        n = len(common)
        self._series.append(dy)
        if len(self._series) < SERIES_N:
            return None
        s = self._series[-SERIES_N:]
        self._series.clear()
        if max(s) - min(s) > 12.0:      # bowls are static: a spread over 12 px in a 720 frame is detection noise
            self.last = {"status": "measurement unstable, waiting", "spread_px": round((max(s) - min(s)) * FULL_H / self.frame_h)}
            return None
        med = float(np.median(s))
        # Without camera motion the offset cannot jump: such a jump is garbage.
        if self._accepted is not None and abs(med - self._accepted) > JUMP_PX and now < self._cooldown_until + 5:
            pass  # right after our own step a jump is expected
        elif self._accepted is not None and abs(med - self._accepted) > JUMP_PX and self._rejects < 3:
            # Three series in a row with the same "jump" is no longer noise but a
            # fact (a slow drift the motion detector missed): accept it.
            self._rejects += 1
            self.last = {"status": "jump without motion, rejected",
                         "dy_px": round(med * FULL_H / self.frame_h), "points": n}
            log.warning("tilt: jump %+.0f -> %+.0f px without camera motion, rejected (%d/3)",
                        self._accepted * FULL_H / self.frame_h, med * FULL_H / self.frame_h, self._rejects)
            return None
        self._rejects = 0
        self._accepted = med
        return self._decide(med, n, settled, now)

    def _decide(self, dy: float, n: int, settled: bool, now: float) -> dict:
        info = {"dy_px": round(dy * FULL_H / self.frame_h), "points": n}
        # After a step, check that we got closer to the reference. If further,
        # the calibration or the sign is wrong; moving the camera further is dangerous.
        if self._pending_verify is not None:
            before, self._pending_verify = self._pending_verify, None
            if abs(dy) > before + self.step_px * 0.3:
                self.frozen = True
                self.last = {"status": "FROZEN: further away after the step", **info}
                log.error("tilt: |dy| grew after the step %.0f -> %.0f px, loop frozen",
                          before * FULL_H / self.frame_h, abs(dy) * FULL_H / self.frame_h)
                return self.last
        need = steps_for(dy, self.step_px)
        info["steps_needed"] = need
        if need == 0:
            self.last = {"status": "on target", **info}
            return self.last
        if self.frozen:
            self.last = {"status": "drifted, loop frozen", **info}
            return self.last
        if not self.act:
            self.last = {"status": "drifted (observe-only mode, not moving)", **info}
            return self.last
        if not settled:
            self.last = {"status": "drifted, camera moving, waiting", **info}
            return self.last
        if now < self._cooldown_until:
            self.last = {"status": "drifted, pause after correction", **info}
            return self.last
        if self.ptz is None:
            self.last = {"status": "drifted, PTZ unavailable", **info}
            return self.last
        direction = "down" if need > 0 else "up"
        done = 0
        for _ in range(abs(need)):
            try:
                self.ptz.rotate(direction)
                done += 1
            except Exception as e:
                log.warning("tilt: correction interrupted: %s", e)
                break
        self._cooldown_until = now + COOLDOWN_SEC
        self._pending_verify = abs(dy)
        self._accepted = None
        log.info("tilt: scene moved %+d px, made %d step(s) %s", info["dy_px"], done, direction)
        self.last = {"status": f"corrected: {done} {direction}", **info}
        return self.last

    def check(self, frame_bgr, settled: bool, now: float | None = None) -> dict:
        """Single check for the CLI (no series): only measure/decide."""
        now = now or time.time()
        dx, dy, n = self.measure(frame_bgr)
        if dy is None:
            self.last = {"status": "scene not recognised", "points": n}
            return self.last
        return self._decide(dy, n, settled, now)
