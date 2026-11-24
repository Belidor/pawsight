"""Following the dog by panning the camera, with no tilt.

The camera's built-in tracking is unsuitable for two reasons the owner named:
it tilts the camera (and the height must hold) and it follows any motion,
people included. Here the camera only pans left/right, and only after the dog.

The logic is deliberately simple; this is not an autopilot:
  * goal: keep the dog near the horizontal centre;
  * outside the dead zone -> ONE step per tick towards it; the next one comes
    when the camera has arrived and the dog's position has been recomputed from
    a fresh frame. A burst of several steps with pauses inside the detector loop
    was a mistake: frames were not processed meanwhile, the motion happened
    "off-screen", and the edge heuristic decided "did not move" (falsely
    blocking the direction);
  * dog left the frame: the chase decision is made ONCE, at the moment of loss,
    and logged in full (see `_plan_chase`): at the edge or walking towards it ->
    up to SEARCH_STEPS steps that way; vanished mid-frame -> as many steps as
    following would have made with the dog visible (dead reckoning), but no more
    than two; vanished near the centre -> stay put;
  * at most MAX_STEPS_PER_MIN steps per minute, as protection against jitter;
  * do nothing while the camera is moving;
  * SEARCH: no dog -> the patrol (`patrol.py`) is in control: the camera stays
    on the bowls and after SEARCH_IDLE_SEC sweeps sectors right and left.
    The old search ("a step every SEARCH_SEC, swinging between edges") remains
    as a fallback and only runs when the patrol is disabled.

The first version waited for an offset of 18% of the width, made one step and
required 2 s to "settle": a walking dog simply left the frame in that time
(tested on the owner: "turned a tiny bit and that was it").
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque

from .patrol import Scene

log = logging.getLogger("pawsight.follow")

# Manual pan from the dashboard: only these directions (ptz.DIRECTIONS).
MANUAL_DIRS = ("left", "right", "up", "down")

DEADBAND = 0.10          # fraction of frame width from the centre within which we don't pan
COOLDOWN_SEC = 3.5       # the motor moves in two phases (~1st and ~3rd second): wait for both
MAX_STEPS_PER_MIN = 20
STEP_FRAC = {"right": 221 / 2304, "left": 372 / 2304}   # measured 2026-09-06
NOMOVE_WAIT_SEC = 4.0    # the motor moves in two phases: motion is visible at the 1st and 3rd second
NOMOVE_STRIKES = 3       # "did not move" is only a safety net: it gave a false edge twice
SEARCH_STEPS = 3         # chase steps when the dog clearly went past the edge
SEARCH_EDGE = 0.30       # "at the edge" = further than this fraction of width from the centre
SEARCH_BOX_MARGIN = 0.05 # the box touches the outer 5% even when its centre is closer to the middle
SEARCH_WINDOW_SEC = COOLDOWN_SEC * SEARCH_STEPS + NOMOVE_WAIT_SEC
SEARCH_SEC_DEFAULT = 10.0   # interval between search steps; 0 disables search
# The edge of travel is a firmware counter, and it releases exactly as many pulses
# as the camera has moved back: on 2026-09-08 06:00:35 the patrol made eight steps
# left, then eight steps right went through and the ninth got overflow again. Until
# the camera has moved the other way the answer will be the same, so retries are
# rare: a one-minute retry produced 587 rejected commands a day and not a single
# successful pulse from 00:30 to 06:00. The block is lifted not by a timer but by
# confirmed motion in the other direction (`_note_motion`).
EDGE_BLOCK_SEC = 300.0
# Detection at the frame edge flickers from frame to frame (05:50:05-05:50:30: eight
# "dog vanished ... 0.2 s ago" lines in twenty seconds). A gap within this time is
# not a loss: the chase plan is not rebuilt and nothing is logged.
LOST_GRACE_SEC = 0.8
# Dead reckoning (dog vanished away from the edge): at most two steps. The real
# left step in the logs is smaller than the calibrated one (~0.10 vs 0.16 of the
# width), and blindly overshooting a standing dog costs more than undershooting:
# the second loop then drags the camera back.
RECKON_MAX_STEPS = 2
# "Walking towards the edge": at least TRAJ_MIN_SAMPLES quiet frames in a row
# (camera still, no commands) with a monotonic outward motion of at least
# EXIT_SPEED widths/s. Box jitter at 5 fps gives up to ~0.05 widths/s and is not
# monotonic.
EXIT_SPEED = 0.10
TRAJ_SEC = 1.2
TRAJ_MIN_SAMPLES = 3
TRAJ_FRESH_SEC = COOLDOWN_SEC + 1.0   # a trajectory older than this at the moment of loss does not count
# The dog vanished mid-frame (not at the edge, not moving): most likely it is
# still there and the detector is flickering; it loses a lying dog for 10-40 s.
# The patrol does not take the camera for this long (the normal pause is
# release_sec, 10 s).
MIDFRAME_HOLD_SEC = 60.0


class _PatrolCamera:
    """Motor for the patrol.

    COOLDOWN_SEC deliberately does not apply here: the patrol keeps its own
    pauses at stops, and the return home should go as a burst. The owner asked
    for "one motion", and the only way to get close is a queue without extra
    pauses between pulses.
    """

    def __init__(self, follow: "DogFollow") -> None:
        self._f = follow

    @property
    def busy(self) -> bool:
        f = self._f
        return bool(f.worker.busy) if f.worker is not None else False

    def blocked(self, direction: str, now: float) -> bool:
        """The direction is closed by the edge of travel. The patrol must know this:
        otherwise a sector towards the closed side spins idly (the pulse does not
        go out, the stop counter does not grow) until the block expires."""
        return now < self._f._blocked_until.get(direction, 0.0)

    def step(self, direction: str) -> bool:
        f, now = self._f, time.time()
        if now < f._blocked_until.get(direction, 0.0):
            return False
        if f.worker is not None:
            if f.worker.busy:
                return False
            f.worker.submit(direction, "patrol")
            f._last_cmd_ts = now
            return True
        try:
            f.ptz.rotate(direction)
        except Exception as e:
            if "overflow" in str(e).lower():
                f._hit_edge(direction, now, "overflow", tag="patrol")
            else:
                log.warning("patrol: pan failed: %s", str(e)[:80])
            return False
        # The edge of travel is caught during search too, so we watch for motion;
        # but the height reference must not be reset on every pulse: a sector of
        # a dozen pulses would never let it settle.
        f._pending_move = (direction, now)
        f._last_cmd_ts = now
        return True


class DogFollow:
    def __init__(self, ptz, frame_w: int, enabled: bool = True,
                 search_sec: float = SEARCH_SEC_DEFAULT, worker=None,
                 patrol=None) -> None:
        self.ptz = ptz
        self.patrol = patrol
        self.worker = worker             # PtzWorker: commands go to a thread, the loop does not wait
        self.w = frame_w
        self.enabled = enabled
        self.search_sec = search_sec
        self._search_dir = "right"
        self._last_search = 0.0
        self._searching = False          # explicit flag instead of parsing the status text
        self._stalled = False            # stream froze: we are blind, must not step
        self._diag_at = 0.0
        self._moving_frames = 0          # how many frames in a row "camera is moving"
        self._next_ok = 0.0
        self._recent: list[float] = []
        self._last_seen: tuple[float, float] | None = None   # (offset, ts) of the last observation
        self._last_box: tuple[float, float] | None = None    # x1, x2 of the same box
        self._last_step: tuple[str, float] | None = None     # last following step
        self._last_cmd_ts = float("-inf")                    # any motor command
        # Trajectory from quiet frames and the estimated position of the lost dog.
        self._traj: list[tuple[float, float]] = []
        self._reckoned: float | None = None
        self._chase: tuple[str, int, str] | None = None      # direction, steps, reason
        self._chase_done = 0
        self._chase_planned = False
        self._stuck_since: float | None = None   # since when the dog's side has been closed
        self._scene_bowls: float | None = None               # for the log on hitting the edge
        self._raw_dogs: list = []                            # raw dog boxes of the frame, for the loss log
        # Hitting the edge: the camera answers OK but does not move. Detected by
        # the absence of motion after a step; the direction is then blocked.
        self._pending_move: tuple[str, float] | None = None
        self._nomove: dict[str, int] = {"left": 0, "right": 0}
        self._blocked_until: dict[str, float] = {"left": 0.0, "right": 0.0}
        self.last = {"status": "off" if not enabled else "no dog"}
        self.on_step = None          # callback: tell the tilt loop about our step
        # Manual pans from the dashboard awaiting the camera's answer (FIFO, like
        # the PtzWorker queue), and where to report the result: on_manual(rid, dir, ok, err).
        self._manual: deque = deque()
        self.on_manual = None
        if self.patrol is not None:
            self.patrol.cam = _PatrolCamera(self)

    def state(self) -> dict:
        st = {"enabled": self.enabled, **self.last}
        if self.patrol is not None:
            st["patrol"] = self.patrol.state_dict()
        return st

    def _forget_target(self) -> None:
        self._last_seen = None
        self._last_box = None
        self._traj.clear()
        self._reckoned = None
        self._chase = None
        self._chase_done = 0
        self._chase_planned = False
        self._stuck_since = None

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self._forget_target()
        self.last = {"status": "no dog" if on else "off"}
        log.info("dog following -> %s", "on" if on else "off")

    def request_home(self, now: float | None = None) -> bool:
        """Hand the motor to the return-to-bowls, without changing the follow setting."""
        if self.patrol is None or self.ptz is None:
            return False
        self._chase = None
        self._chase_done = 0
        self._chase_planned = True       # chase after the previous target is cancelled
        self._searching = False
        self.patrol.request_home(time.time() if now is None else now)
        self.last = {"status": "returning to the bowls on command"}
        return True

    def manual(self, direction: str, rid, now: float | None = None) -> bool:
        """Manual pan from the dashboard, through the same motor thread as the
        patrol and following. The dashboard used to turn the camera over its own
        connection, bypassing the detector, and the patrol odometry (patrol.pos)
        did not know about the pan: "home" ended up in the wrong place after a
        manual pan. The result arrives in _drain_results, like all other commands."""
        if self.worker is None or direction not in MANUAL_DIRS:
            return False
        self.worker.submit(direction, "manual")
        self._manual.append((direction, rid))
        self._last_cmd_ts = time.time() if now is None else now
        return True

    def _manual_done(self, direction: str, ok: bool, err: str | None, now: float) -> None:
        rid = None
        if self._manual:
            d0, rid = self._manual.popleft()
            if d0 != direction:
                log.warning("manual pan: expected %s, the answer came for %s", d0, direction)
        pan = direction in ("left", "right")
        if ok and pan:
            self._pending_move = (direction, now)     # catch the edge of travel here too
            if self.patrol is not None:
                self.patrol.note_pulse(direction)     # the patrol odometry knows about the pan
            if self.on_step:
                self.on_step()                        # height reference: recapture
        elif not ok and pan and "overflow" in (err or "").lower():
            self._hit_edge(direction, now, "overflow", tag="manual")
        if self.on_manual is not None:
            self.on_manual(rid, direction, ok, err)

    def _step(self, direction: str, now: float) -> bool:
        """Exactly one step. The detector loop is not blocked: frames must keep
        flowing, otherwise the camera's motion goes unnoticed."""
        self._recent = [x for x in self._recent if now - x < 60]
        if now < self._blocked_until.get(direction, 0.0):
            self.last = {"status": f"at the edge of travel ({direction}), waiting"}
            return False
        if len(self._recent) >= MAX_STEPS_PER_MIN:
            self.last = {"status": "steps-per-minute limit"}
            return False
        if self.worker is not None:
            if self.worker.busy:
                self.last = {"status": "waiting for the camera to answer"}
                return False
            self.worker.submit(direction, "follow")
            self._commanded(direction, now)
            return True                   # the result arrives via _drain_results()
        try:
            self.ptz.rotate(direction)
        except Exception as e:
            if "overflow" in str(e).lower():
                self._hit_edge(direction, now, "overflow")
            else:
                log.warning("follow: pan failed: %s", str(e)[:80])
            return False
        self._after_step(direction, now)
        self._commanded(direction, now)
        return True

    def _commanded(self, direction: str, now: float) -> None:
        """The step went to the motor: pause, limit and dead reckoning count from the command."""
        self._recent.append(now)
        self._next_ok = now + COOLDOWN_SEC
        self._last_step = (direction, now)
        self._last_cmd_ts = now
        if self._reckoned is not None:
            self._reckoned += self._shift(direction)

    @staticmethod
    def _shift(direction: str) -> float:
        """How far the dog moves IN THE FRAME from one camera pulse:
        camera goes left, the scene moves right."""
        return STEP_FRAC["left"] if direction == "left" else -STEP_FRAC["right"]

    def _after_step(self, direction: str, now: float) -> None:
        self._pending_move = (direction, now)
        if self.patrol is not None:
            self.patrol.note_pulse(direction)   # following also takes the camera away from home
        if self.on_step:
            self.on_step()

    def _drain_results(self, now: float) -> None:
        """Command results from the thread, handled in the main thread where all the state lives."""
        if self.worker is None:
            return
        for direction, tag, ok, err, dt in self.worker.poll():
            self._last_cmd_ts = now
            if dt > 1.5:
                log.warning("camera answered %s in %.1f s", direction, dt)
            if tag == "manual":
                self._manual_done(direction, ok, err, now)
                continue
            if ok:
                if tag == "follow":
                    self._after_step(direction, now)
                elif tag == "patrol":
                    # Watch for motion (edge of travel), leave the height reference alone.
                    self._pending_move = (direction, now)
                elif self.on_step:
                    self.on_step()        # a height-loop step: recapture the reference too
                continue
            if tag == "patrol" and self.patrol is not None:
                self.patrol.note_pulse_failed(direction)   # the camera did not move
            if tag == "follow":
                # The step was counted in dead reckoning when it was sent
                # (`_commanded`), but the camera did not move. Without rolling it
                # back, following decided "estimated already at the centre" and
                # refused to chase: 23:49:49 overflow ->
                # 23:49:52 "not chasing: estimated already at the centre (+0.06)".
                if self._reckoned is not None:
                    self._reckoned -= self._shift(direction)
                if self._last_step is not None and self._last_step[0] == direction:
                    self._last_step = None      # there is no unfinished step to subtract
            if "overflow" in (err or "").lower():
                self._hit_edge(direction, now, "overflow", tag=tag)
            else:
                log.warning("pan %s failed: %s", direction, (err or "")[:80])

    def _hit_edge(self, direction: str, now: float, how: str, tag: str = "follow") -> None:
        self._pending_move = None
        self._blocked_until[direction] = now + EDGE_BLOCK_SEC
        self._nomove[direction] = 0
        if self.patrol is not None:
            self.patrol.note_edge(direction)
        # The bowls' offset at the moment of hitting the edge is the only way to
        # see whether the firmware limit drifts relative to the scene
        # (docs/architecture.md, 2026-09-07).
        bowls = (f"bowls at {self._scene_bowls:+.2f} width" if self._scene_bowls is not None
                 else "no bowls in frame")
        if self._searching:
            self._search_dir = "left" if direction == "right" else "right"
            self.last = {"status": f"edge of travel ({direction}), turning around"}
            log.info("search: edge of travel %s (%s), %s, turning around", direction, how, bowls)
        elif tag == "patrol":
            log.info("patrol: edge of travel %s (%s), %s; direction closed for %.0f s "
                     "or until motion the other way", direction, how, bowls, EDGE_BLOCK_SEC)
        elif tag == "manual":
            log.info("manual pan: edge of travel %s (%s), %s; direction closed to following "
                     "and patrol for %.0f s", direction, how, bowls, EDGE_BLOCK_SEC)
        else:
            self.last = {"status": f"at the edge of travel ({direction}), pause {EDGE_BLOCK_SEC:.0f} s"}
            log.warning("follow: edge of travel %s (%s), %s; direction blocked for %.0f s",
                        direction, how, bowls, EDGE_BLOCK_SEC)

    def _edge_locked(self, off: float, now: float) -> bool:
        """The dog is outside the dead zone, and the side we would pan towards is
        closed by the edge of travel. Following has nothing to do in this position,
        and it must not pretend it is working either: the camera goes to search."""
        if abs(off) < DEADBAND:
            return False
        direction = "right" if off > 0 else "left"
        return now < self._blocked_until.get(direction, 0.0)

    def _note_motion(self, moving: bool, now: float) -> None:
        """Confirm motion; a series of steps without motion means we hit the edge."""
        if self._pending_move is None:
            return
        direction, t = self._pending_move
        if moving:
            self._pending_move = None
            self._nomove[direction] = 0
            if direction in ("left", "right"):
                opposite = "right" if direction == "left" else "left"
                # After actually moving away from the edge, the opposite direction
                # is available again. A single OK reply without motion proves nothing.
                if self._blocked_until.get(opposite, 0) > now:
                    log.info("follow: moved away from edge %s, block lifted", opposite)
                self._blocked_until[opposite] = 0.0
                self._nomove[opposite] = 0
                if self.patrol is not None:
                    self.patrol.note_motion(direction)
        elif now - t > NOMOVE_WAIT_SEC:
            self._pending_move = None
            self._nomove[direction] += 1
            # The explicit edge signal is the camera error 'overflow' (handled in _step).
            # "Did not move" gave a FALSE edge twice (verified: afterwards the camera
            # moved fine), so only after three misses in a row, in case the other
            # edge does not report overflow.
            if self._nomove[direction] >= NOMOVE_STRIKES:
                self._hit_edge(direction, now, f"did not move {NOMOVE_STRIKES} times in a row")

    def _search(self, now: float) -> None:
        """No dog: every search_sec, one pan step, swinging between edges."""
        if self.search_sec <= 0:
            self.last = {"status": "no dog"}
            return
        since_seen = now - self._last_seen[1] if self._last_seen else float("inf")
        if since_seen < self.search_sec or now - self._last_search < self.search_sec:
            if not self.last.get("status", "").startswith("searching"):
                self.last = {"status": "no dog"}
            return
        if self._last_seen and abs(self._last_seen[0]) > 0.15 and since_seen < 60:
            self._search_dir = "right" if self._last_seen[0] > 0 else "left"
        if now < self._blocked_until.get(self._search_dir, 0.0):
            self._search_dir = "left" if self._search_dir == "right" else "right"
            if now < self._blocked_until.get(self._search_dir, 0.0):
                self.last = {"status": "searching for the dog: both edges blocked, waiting"}
                return
        self._last_search = now
        self._searching = True
        if self._step(self._search_dir, now):
            self.last = {"status": f"searching for the dog: step {self._search_dir}"}
            log.info("search: no dog for %s -> step %s",
                     f"{since_seen:.0f} s" if since_seen < 1e6 else "since startup", self._search_dir)

    def stream_stalled(self, stalled: bool) -> None:
        """A gap in the video stream (after a pan the camera freezes for 6-19 s).
        Stepping blind is not allowed: every step can dig the hole deeper. Wait for frames."""
        self._stalled = stalled
        if stalled:
            self.last = {"status": "stream froze, waiting for frames"}

    def _diag(self, now: float, moving: bool, dogs_present: bool) -> None:
        """Every 15 s: why the search is not stepping. Without this a pause in the log is unexplained."""
        if now - self._diag_at < 15:
            return
        self._diag_at = now
        log.info("search/diag: camera_moving=%s (frames in a row %d) pause_left=%.1fs "
                 "since_last_step=%.0fs dir=%s blocked=%s dog=%s target=%s status=%s",
                 moving, self._moving_frames, max(0.0, self._next_ok - now),
                 now - self._last_search if self._last_search else -1, self._search_dir,
                 {d: round(t - now) for d, t in self._blocked_until.items() if t > now},
                 dogs_present, self._last_seen, self.last.get("status"))

    # ------------------------------------------------------------ target loss

    def _camera_quiet(self, moving: bool, now: float) -> bool:
        """The frame is usable for the dog's trajectory: the camera is still and
        there were no motor commands. Between the motor's two phases the motion
        detector is silent, and the frame shift from a pan would read as the dog
        walking."""
        return (not moving and now >= self._next_ok and self._pending_move is None
                and now - self._last_cmd_ts >= NOMOVE_WAIT_SEC)

    def _pending_shift(self, now: float) -> float:
        """The step has already gone to the motor but the frame is still old: the
        dog IN THE FRAME will move by this much when the camera arrives. Account
        for it, otherwise dead reckoning overshoots."""
        if self._last_step is None or now >= self._last_step[1] + COOLDOWN_SEC:
            return 0.0
        return self._shift(self._last_step[0])

    def _outward_speed(self, off: float) -> float:
        """Speed towards its own edge from quiet frames, widths/s; 0 means not walking."""
        if len(self._traj) < TRAJ_MIN_SAMPLES:
            return 0.0
        sign = 1.0 if off > 0 else -1.0
        for (a, _), (b, _) in zip(self._traj, self._traj[1:]):
            if (b - a) * sign <= 0:
                return 0.0                       # not monotonic: box jitter
        dt = self._traj[-1][1] - self._traj[0][1]
        if dt < 0.4:
            return 0.0
        return (self._traj[-1][0] - self._traj[0][0]) * sign / dt

    def _kept_walking(self, off: float, ts: float, speed: float) -> bool:
        """The trajectory was taken BEFORE the step, and the last frame is already
        within the motor pause. Did the dog keep walking or stop? Compare the last
        observation with two predictions, "kept walking" and "stopped"; the shift
        from the unfinished step is unknown (0..full), so take half."""
        o0, t0 = self._traj[-1]
        if ts <= t0:
            return True                          # the trajectory is the last observation
        if ts - t0 > TRAJ_FRESH_SEC:
            return False
        sign = 1.0 if off > 0 else -1.0
        shift = self._shift(self._last_step[0]) / 2 if self._last_step else 0.0
        stopped = o0 + shift
        walking = o0 + shift + sign * speed * (ts - t0)
        return abs(off - walking) < abs(off - stopped)

    def _plan_chase(self, now: float) -> tuple[str, int, str]:
        """The chase decision: once per loss, and logged in full."""
        off, ts = self._last_seen
        x1, x2 = self._last_box if self._last_box else (0.0, 0.0)
        side = "right" if off > 0 else "left"
        est = self._reckoned if self._reckoned is not None else off
        speed = self._outward_speed(off)
        traj = ", ".join(f"{o:+.2f}" for o, _ in self._traj)
        # The tracker only reports confirmed tracks: after a track is lost, a dog
        # with conf < 0.30 does not start a new one (BoT-SORT, fuse_score). Raw
        # boxes in the frame of loss tell "left" apart from "detector did not confirm".
        raw = (f"raw dog boxes {len(self._raw_dogs)}, max conf "
               f"{max(s for s, _, _ in self._raw_dogs):.2f}" if self._raw_dogs else "no raw dog boxes")
        if abs(off) <= DEADBAND:
            steps, why = 0, "vanished near the centre"
        elif abs(off) > SEARCH_EDGE or (x2 >= self.w * (1 - SEARCH_BOX_MARGIN) if off > 0
                                        else x1 <= self.w * SEARCH_BOX_MARGIN):
            steps, why = SEARCH_STEPS, "at the edge"
        elif speed >= EXIT_SPEED and self._kept_walking(off, ts, speed):
            steps, why = SEARCH_STEPS, f"walking towards the edge at {speed:.2f} widths/s"
        elif (est > 0) != (off > 0) or abs(est) <= DEADBAND:
            steps, why = 0, f"estimated already at the centre ({est:+.2f})"
        else:
            steps = min(RECKON_MAX_STEPS, math.ceil((abs(est) - DEADBAND) / STEP_FRAC[side]))
            why = f"dead reckoning {est:+.2f}"
        # Not at the edge and not walking: the dog is probably still in frame, the patrol should wait.
        if steps < SEARCH_STEPS and self.patrol is not None:
            self.patrol.hold(now + MIDFRAME_HOLD_SEC)
        log.info("follow: dog vanished (last %+.2f, %.1f s ago, box x=%d..%d, "
                 "trajectory [%s], %s) -> %s", off, now - ts, x1, x2, traj, raw,
                 f"chasing {side}: {steps} step(s), {why}" if steps else f"not chasing: {why}")
        return side, steps, why

    def _chase_tick(self, now: float) -> bool:
        """Chasing the lost dog. True means following owns the motor and the patrol waits."""
        if not self._last_seen or now - self._last_seen[1] >= SEARCH_WINDOW_SEC:
            return False
        if not self._chase_planned:
            self._chase_planned = True
            self._chase = self._plan_chase(now)
        if self._chase is None or self._chase_done >= self._chase[1]:
            return False
        direction, total, why = self._chase
        if now < self._blocked_until.get(direction, 0.0):
            # The direction is locked by the edge of travel: nothing to chase with, the camera goes to the patrol.
            self.last = {"status": f"chase impossible: at the edge of travel ({direction})"}
            return False
        if self._step(direction, now):
            self._chase_done += 1
            if self.patrol is not None:
                self.patrol.note_chase(now)
            self.last = {"status": f"chasing: step {direction} ({self._chase_done}/{total})",
                         "offset": self._last_seen[0]}
            log.info("follow: chase step %s (%d/%d, %s)", direction, self._chase_done,
                     total, why)
        return True                              # motor busy or limit hit: wait

    # ----------------------------------------------------------------- tick

    def tick(self, dogs: list[dict], moving: bool, now: float,
             scene: Scene | None = None, raw_dogs: list | None = None) -> None:
        forced_home = bool(self.patrol and self.patrol.return_requested)
        # Turning following off must not disable the explicit return button.
        # Without PTZ it cannot be carried out anyway.
        if self.ptz is None or (not self.enabled and not forced_home):
            return
        self._scene_bowls = scene.bowls if scene is not None else None
        self._raw_dogs = raw_dogs or []
        self._drain_results(now)
        if self._stalled:
            return
        self._moving_frames = self._moving_frames + 1 if moving else 0
        if self._moving_frames in (8, 40):
            log.info("search/diag: \"camera moving\" for %d frames in a row with no commands",
                     self._moving_frames)
        self._note_motion(moving, now)
        self._diag(now, moving, bool(dogs))
        if forced_home:
            if moving or now < self._next_ok:
                return
            self.patrol.tick(now, scene if scene is not None else Scene())
            self.last = {"status": self.patrol.status}
            return
        off = None
        if dogs:
            # Observe during the motor pause too: these are exactly the seconds
            # in which the dog manages to reach the other edge and disappear.
            d = max(dogs, key=lambda x: (x["box"][2] - x["box"][0]) * (x["box"][3] - x["box"][1]))
            x1, _, x2, _ = d["box"]
            off = ((x1 + x2) / 2 - self.w / 2) / self.w
            self._last_seen = (off, now)
            self._last_box = (x1, x2)
            # Dead reckoning: where the dog will be in the frame once the motor arrives.
            self._reckoned = off + self._pending_shift(now)
            if self._camera_quiet(moving, now):
                self._traj.append((off, now))
                newest = self._traj[-1][1]
                self._traj = [(o, t) for o, t in self._traj if newest - t <= TRAJ_SEC]
            self._chase = None
            self._chase_done = 0
            self._chase_planned = False
            self._searching = False
            self.last = {"status": "in frame", "offset": round(off, 2)}
        # The dog is in frame, but there is no way to pan towards it: its side is
        # closed by the edge of travel. This is not following, it is standing still,
        # and the patrol's idle timer must keep running during these seconds,
        # otherwise the camera never moves (log 2026-09-08, 00:30-06:00: dog at
        # +0.41, 5.5 hours and not a single pulse).
        stuck = off is not None and self._edge_locked(off, now)
        # Only a frame where the dog is VISIBLE and reachable clears the stuck mark.
        # A frame without the dog does not: at the edge, detection flickers every
        # third frame, and each flicker would set the mark again, so the patrol
        # would never get its turn.
        if dogs and not stuck:
            self._stuck_since = None
        first_stuck = stuck and self._stuck_since is None
        if self.patrol is not None and dogs and (not stuck or first_stuck):
            # Normally a frame with the dog resets the patrol's idle timer, before
            # the "camera is moving" check. But at the edge of travel this message
            # goes out EXACTLY ONCE: after that let the timer run, otherwise a
            # visible but unreachable dog holds the camera forever.
            self.patrol.note_dog(now)
        if first_stuck:
            self._stuck_since = now
            log.info("follow: dog at %+.2f, but the %s travel is closed by the limit; "
                     "handing the camera to search", off, "right" if off > 0 else "left")
        if moving or now < self._next_ok:
            self.last = {"status": "camera moving, waiting for it to stop" if moving
                         else "pause after pan",
                         "wait_sec": round(max(0, self._next_ok - now), 1),
                         **({"offset": round(off, 2)} if off is not None else {})}
            return

        if not dogs:
            # Missing one or two frames is not a loss: at the frame edge detection
            # flickers, and rebuilding the chase plan on every flicker means logging
            # eight decisions in twenty seconds and jerking the motor.
            if self._last_seen is not None and now - self._last_seen[1] < LOST_GRACE_SEC:
                self.last = {"status": "frame without the dog, waiting for the next",
                             "offset": round(self._last_seen[0], 2)}
                return
            if self._chase_tick(now):
                return
            if self.patrol is not None:
                self.patrol.tick(now, scene if scene is not None else Scene())
                self.last = {"status": self.patrol.status}
                return
            self._search(now)
            return

        if stuck:
            # Hand the camera to search. A sector towards the free side is the only
            # way to regain travel on the closed side: the firmware limit releases
            # exactly as many pulses as the camera has moved back
            # (06:00:35: eight to the left, then eight to the right went through).
            side = "right" if off > 0 else "left"
            self.last = {"status": f"{side} edge of travel: the camera goes no further, "
                                   "searching for the dog",
                         "offset": round(off, 2)}
            if self.patrol is not None:
                self.patrol.tick(now, scene if scene is not None else Scene())
            return
        if abs(off) < DEADBAND:
            return
        direction = "right" if off > 0 else "left"
        if self._step(direction, now):
            self.last = {"status": f"step {direction}", "offset": round(off, 2)}
            log.info("follow: dog offset by %+.2f width -> step %s", off, direction)
