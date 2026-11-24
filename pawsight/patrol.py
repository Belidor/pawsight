"""Patrol: home position on the bowls and a sector search for the dog.

By default the camera looks at the bowls. If the dog has been out of frame for
longer than `idle_sec`, sweep a sector to the right in steps with pauses; if not
found, go home and do the same to the left, then pause again and repeat the
cycle. When the dog appears the patrol stays quiet and following
(`follow.py`) is in control.

Three hardware quirks shaped this whole design:

  * **There is no absolute position.** `set_motor` takes a direction and does
    not report where the camera ended up. "Home" is not a coordinate but what
    we remember ourselves and confirm visually by the bowls.
  * **The step is asymmetric:** 221 px to the right, 372 px to the left in a
    2304 px frame (calibration in docs/architecture.md). So the position is tracked in
    FRACTIONS OF FRAME WIDTH, not in pulses: "N pulses there, N back" misses
    by 68%.
  * **The field of view has not been measured**, so there is not a single
    degree here. A sector is given in pulses and the owner tunes them by eye.

Odometry is constantly corrected from the image: if the bowls are visible, the
position is taken from them, since by definition they sit at point 0. If not,
but the learned fridge is visible, from the fridge. If nothing is visible, we
go by the counter and honestly state in the status that home is not confirmed.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("pawsight.patrol")

# Measured 2026-09-06 on a 2304x1296 frame; same calibration as in follow.py.
RIGHT_FRAC = 221 / 2304          # 0.0959 of frame width per pulse to the right
LEFT_FRAC = 372 / 2304           # 0.1615: larger to the left because of backlash

# Measured on 12 frames from the dataset: the fridge sits at +0.016..+0.027
# widths from the bowls, but on one frame YOLO produced a SECOND "fridge" 0.69
# widths from the real one, and it was more confident. So a candidate is only
# accepted if it sits where the anchor is supposed to be.
FRIDGE_TRUST = 0.35              # in widths; further away it is not our fridge
# While the dog flickers in the frame, following owns the camera. Without this
# delay the patrol pulled the camera home on every frame without the dog,
# following pulled it back, and the two loops drove the motor against each
# other: 554 pans in 40 minutes on false detections (docs/architecture.md, 18:20).
RELEASE_SEC_DEFAULT = 10.0
# An offset larger than this is not a camera position but a broken counter: the
# camera's travel is finite. In production the counter drifted to 7.2 widths and
# nobody checked it.
MAX_OFFSET = 3.0
# The return home cannot be longer than the longest reasonable sector.
RETURN_MAX_PULSES = 20
HOME_MEMORY_SEC = 5.0            # how long "home confirmed" lasts without bowls in frame
GIVE_UP_RETRY_SEC = 30.0         # after a failed return, don't hammer again immediately
# The "overflow" edge of travel is a firmware counter, and it drifts relative to
# the scene: at the same limit the bowls were at +0.30 and also at the frame
# centre (docs/architecture.md, 2026-09-07). So after a refusal we retry, each time half
# as often, up to this ceiling.
GIVE_UP_RETRY_MAX = 300.0
# The motor moves in two phases (~1st and ~3rd second after the command;
# follow.COOLDOWN_SEC). While the bowls are in frame, the return makes one pulse
# and WAITS to see what the frame shows: otherwise odometry is corrected from a
# frame taken before the motor started, the burst continues, and the camera
# overshoots home (20:24:32-39, 2026-09-07: 4 right, overflow, 3 left).
# The burst remains for the blind part of the way, where the frame has nothing
# to show anyway.
PULSE_SETTLE_SEC = 3.5
NOTES_KEPT = 6


@dataclass
class Scene:
    """What the detector sees in the current frame, in widths from its centre.

    `bowls` is the centre of the bowl pair, `fridge` the fridge. `None` means not found.
    """

    bowls: float | None = None
    # Several fridges may be "found": at a low threshold YOLO confuses other
    # white appliances with it. So ALL candidates go here (by decreasing
    # confidence), and the patrol picks the right one: it knows where the anchor was.
    fridges: tuple[float, ...] = ()
    settled: bool = False


class Patrol:
    def __init__(self, cam=None, *, idle_sec: float = 180.0, step_right: int = 3,
                 step_left: int = 2, steps: int = 4, dwell_sec: float = 5.0,
                 dwell_frames: int = 2, max_pulses: int = 60,
                 home_deadband: float = 0.10, enabled: bool = True,
                 release_sec: float = RELEASE_SEC_DEFAULT,
                 ref_path: Path | None = None) -> None:
        self.cam = cam          # DogFollow supplies the motor: it knows about the edges
        self.enabled = enabled
        self.idle_sec = idle_sec
        self.steps = steps
        self.dwell_sec = dwell_sec
        self.dwell_frames = dwell_frames
        self.max_pulses = max_pulses
        self.release_sec = release_sec
        self.ref_path = ref_path
        # The dead zone cannot be narrower than half the largest step, otherwise
        # the fine adjustment will hop around the centre. Left step 0.1615 -> minimum 0.081.
        self.home_deadband = max(home_deadband, LEFT_FRAC / 2 + 0.001)
        self._pulses = {"right": max(1, step_right), "left": max(1, step_left)}

        self.state = "returning"     # where we think we are: home / returning / scan
        self.direction = "right"     # the sector in progress or the next one
        self.status = "looking for home"
        self.notes: list[str] = []
        self.home_visits = 0
        self.next_scan_at = 0.0

        # Camera position relative to home, in fractions of frame width.
        # At startup we assume we are home: the camera is usually left that way,
        # and the first bowls we see will either confirm or correct it.
        self.pos = 0.0
        self.pos_known = True
        self.home_confirmed = False
        self.fridge_abs: float | None = None

        self._last_dog: float | None = None
        self._hold_until = 0.0           # following asks not to take the camera until this moment
        self._bowls_ts = float("-inf")
        self._bowls_visible = False      # bowls in the CURRENT frame (not from zone memory)
        self._last_pulse_ts = float("-inf")
        self._queue: list[str] = []
        self._stops_done = 0
        self._pending = 0
        self._dwell_until = 0.0
        self._frames = 0
        self._cycle_pulses = 0
        self._return_budget: int | None = None
        self._retry_at = 0.0
        self._halt_until = 0.0
        # The manual "to the bowls" command takes priority over following and
        # the usual ten-second pause after the dog. The pulse safety cutoff still
        # comes first and can still stop the motor.
        self._manual_home = False
        self._edge: str | None = None
        # Where (by the bowls) the camera hit the edge of travel, and how many
        # times in a row. While the position is the same, retries get sparser;
        # if it moved, retry right away.
        self._edge_pos: float | None = None
        self._edge_fails = 0
        self._load_ref()

    # ------------------------------------------------------------------ facts

    def note_dog(self, now: float) -> None:
        """Dog in frame: the timer resets, the sector is abandoned midway."""
        self._last_dog = now
        self._hold_until = 0.0           # the dog is visible again: the earlier "wait" request is cleared
        self.next_scan_at = 0.0
        if self.state == "scan":
            self._note("dog in frame, search stopped")
            log.info("patrol: dog in frame, sector %s abandoned", self.direction)
        self._queue.clear()
        self._pending = 0
        self.direction = "right"
        if self.state != "home":
            self._enter_return("dog in frame")
        self.status = "following"

    def note_pulse(self, direction: str) -> None:
        """A step made by someone other than the patrol (dog following) also moves the camera."""
        self._bump(direction)

    def note_pulse_failed(self, direction: str) -> None:
        """The camera refused (overflow): the pulse did not move it, so the shift
        from `_bump` is rolled back. Otherwise every rejected pulse would leave a
        phantom 0.096 widths in the odometry until the next frame with the bowls."""
        self.pos -= RIGHT_FRAC if direction == "right" else -LEFT_FRAC

    def note_chase(self, now: float) -> None:
        """Following is chasing a lost dog: the camera is still its own, and the
        pause timer counts from the last chase step, not from the last frame with
        the dog. Otherwise the patrol would turn the camera home right after the
        third chase step."""
        self._last_dog = now

    def hold(self, until: float) -> None:
        """The dog vanished in the MIDDLE of the frame: it is almost certainly
        still there, the detector just does not see it (a lying dog flickers for
        10-40 s). Taking the camera after release_sec would mean leaving a dog
        that never went anywhere, and then following drags it back (log
        20:08:04-20:08:18, 2026-09-07). This does not limit the manual return or
        the safety cutoff."""
        self._hold_until = max(self._hold_until, until)

    def note_edge(self, direction: str) -> None:
        """Hit the edge of travel. follow.py detects it; it has done so for a long time."""
        self._edge = direction
        self.pos_known = False

    def note_motion(self, direction: str) -> None:
        """Confirmed motion away from the edge allows going back towards it."""
        if self._edge is not None and direction != self._edge:
            self._edge = None

    def _at_edge(self, direction: str, now: float) -> bool:
        """The edge in this direction: our own, noted by `note_edge`, or a block
        in following that has not expired yet (it catches overflow for the patrol too)."""
        if self._edge == direction:
            return True
        blocked = getattr(self.cam, "blocked", None)
        return bool(blocked(direction, now)) if blocked is not None else False

    @property
    def return_requested(self) -> bool:
        return self._manual_home

    def request_home(self, now: float) -> None:
        """Cancel the search and start returning to the bowls at a person's command."""
        self._manual_home = True
        self._last_dog = now             # new patrol no earlier than idle_sec from now
        self.next_scan_at = now + self.idle_sec
        self._queue.clear()
        self._pending = 0
        self.direction = "right"
        # A person pressed the button: make a real attempt instead of citing the
        # pause after the previous edge hit; the limit may have moved.
        self._edge = None
        self._edge_pos = None
        self._edge_fails = 0
        self._retry_at = 0.0
        self._hold_until = 0.0
        self._enter_return("on command from the dashboard")
        self._note("return to the bowls requested manually")
        log.info("patrol: return to the bowls requested from the dashboard")

    # ------------------------------------------------------------------- loop

    @property
    def active(self) -> bool:
        """The camera is moving of its own accord: the height loop freezes meanwhile."""
        return self.state in ("scan", "returning")

    def tick(self, now: float, scene: Scene) -> None:
        if not self.enabled or self.cam is None:
            return
        if self._last_dog is None:
            self._last_dog = now          # idle time starts counting at startup
        self._observe(now, scene)
        # The safety cutoff stops both search and return. next_scan_at only
        # limits the start of a sector and is reset when the dog is seen.
        if now < self._halt_until:
            self.status = "safety cutoff: standing until the next cycle"
            return
        # A manual return runs right away, even if the dog is still visible or
        # has just disappeared. That way the button really returns the camera
        # rather than queueing the intent behind PAWSIGHT_PATROL_RELEASE_SEC.
        if self._manual_home:
            if getattr(self.cam, "busy", False):
                return
            if self.state != "returning":
                self._enter_return("on command from the dashboard")
            self._return(now)
            return
        # The dog was just in frame: don't take the camera from following.
        # False detections flicker, and without this pause the two loops drive
        # the motor against each other.
        if now - self._last_dog < self.release_sec:
            self.status = "dog nearby, following has the camera"
            return
        if now < self._hold_until:
            self.status = (f"dog vanished mid-frame, waiting for it another "
                           f"{self._hold_until - now:.0f} s")
            return
        if getattr(self.cam, "busy", False):
            return
        if self.state == "scan":
            self._scan(now, scene)
        elif self.state == "returning":
            self._return(now)
        else:
            self._home(now)

    def state_dict(self) -> dict:
        return {
            "state": self.state,
            "status": self.status,
            "home": round(self.pos, 3),
            "home_confirmed": self.home_confirmed,
            "direction": self.direction,
            "edge_retries": self._edge_fails,
        }

    # --------------------------------------------------------------- by eye

    def _observe(self, now: float, scene: Scene) -> None:
        """Odometry is corrected from the image. The bowls are the origin, at zero."""
        fridge = self._pick_fridge(scene.fridges)
        self._bowls_visible = scene.bowls is not None
        if scene.bowls is not None:
            self.pos = -scene.bowls
            self.pos_known = True
            self._bowls_ts = now
            if fridge is not None:
                self._learn_fridge(fridge + self.pos)
        elif fridge is not None and self.fridge_abs is not None:
            self.pos = self.fridge_abs - fridge
            self.pos_known = True

    def _pick_fridge(self, cands: tuple[float, ...]) -> float | None:
        """Of the candidates, the one that sits where the anchor should be."""
        if not cands:
            return None
        if self.fridge_abs is None:
            # Nothing to learn from: there is no way to pick the right one of two
            # "fridges", and a learned mistake would later steer the camera silently.
            return cands[0] if len(cands) == 1 else None
        want = self.fridge_abs - self.pos          # where it must be in the frame
        best = min(cands, key=lambda f: abs(f - want))
        return best if abs(best - want) <= FRIDGE_TRUST else None

    def _learn_fridge(self, at: float) -> None:
        """The fridge is visible from further away and more reliably than the
        bowls, so it serves as a second reference.

        Its position is neither set by a person nor invented: it is measured
        when the camera is home and the bowls are visible.
        """
        if self.fridge_abs is None:
            self.fridge_abs = at
            self._note(f"anchor (fridge) learned at {at:+.2f} width")
            log.info("patrol: fridge learned at %+.2f width from home", at)
            self._save_ref()
            return
        moved = abs(at - self.fridge_abs)
        if moved > FRIDGE_TRUST:
            return                                          # not our anchor
        self.fridge_abs += 0.2 * (at - self.fridge_abs)     # gentle refinement
        if moved > 0.25:
            self._save_ref()

    # ---------------------------------------------------------------- home

    def _home(self, now: float) -> None:
        self.home_confirmed = (now - self._bowls_ts) <= HOME_MEMORY_SEC
        off_home = abs(self.pos) > self.home_deadband
        if off_home and (now >= self._retry_at or self._moved_since_edge()):
            self._enter_return("drifted from home")
            return
        # `or now` here would be a bug: _last_dog == 0.0 is falsy, and the idle
        # timer would stay at zero forever.
        idle = now - (now if self._last_dog is None else self._last_dog)
        if idle >= self.idle_sec and now >= self.next_scan_at:
            self._start_cycle(now)
            return
        if off_home:
            wait = max(0.0, self._retry_at - now)
            if self._edge_pos is not None and self.home_confirmed:
                self.status = (f"home at the edge of travel: bowls at {-self.pos:+.2f}, the "
                               f"firmware won't go closer; retry in {wait:.0f} s")
            else:
                self.status = f"home not found, staying where it stopped; retry in {wait:.0f} s"
        elif self.home_confirmed:
            self.status = "home: bowls in frame"
        else:
            self.status = "home not confirmed: bowls not visible"

    def _moved_since_edge(self) -> bool:
        """The camera left the spot where it hit the limit (following took it
        away, or the limit moved and the bowls shifted in the frame): try to
        return right away."""
        return self._edge_pos is not None and abs(self.pos - self._edge_pos) > RIGHT_FRAC

    # ------------------------------------------------------------- return

    def _enter_return(self, why: str) -> None:
        self.state = "returning"
        self._return_budget = None
        self._pending = 0
        self.status = f"returning home ({why})"

    def _return(self, now: float) -> None:
        if self._over_budget(now):
            return
        if abs(self.pos) <= self.home_deadband:
            self._arrive(now)
            return
        direction = "left" if self.pos > 0 else "right"
        if self._at_edge(direction, now):
            self._note("return hit the edge of travel")
            self._arrive(now, gave_up="edge of travel")
            return
        if self._return_budget is None:
            # Count by the smallest step and with a margin: backlash and edges eat travel.
            self._return_budget = min(math.ceil(abs(self.pos) / RIGHT_FRAC) + 4,
                                      RETURN_MAX_PULSES)
        if self._return_budget <= 0:
            self._arrive(now, gave_up="pulse budget exhausted")
            return
        if self._bowls_visible and now - self._last_pulse_ts < PULSE_SETTLE_SEC:
            # The target is visible: the step is made, wait to see where it took
            # the camera. Otherwise odometry from a frame taken before the motor
            # started erases this step, and the burst overshoots.
            self.status = f"returning home: {self.pos:+.2f} width, waiting for it to arrive"
            return
        if self._pulse(direction, now):
            self._return_budget -= 1
            self.status = f"returning home: {self.pos:+.2f} width"

    def _arrive(self, now: float, gave_up: str | None = None) -> None:
        """Arrived, or gave up (`gave_up` says why).

        Giving up can happen in two different positions, and they must not be
        confused:
          * the bowls are IN THE FRAME, but the camera answers overflow. The
            position is measured, not counted, so zeroing it would be a lie;
            this is "home at the edge of travel". The firmware limit drifts, so
            we retry, less and less often;
          * the bowls are not visible and the budget ran out: the counter has
            proven it lies, and we honestly zero it, as before.
        """
        self.state = "home"
        self._manual_home = False
        self.home_visits += 1
        seen = (now - self._bowls_ts) <= HOME_MEMORY_SEC
        self.home_confirmed = seen
        self._edge = None
        if gave_up and seen:
            self._edge_fails += 1
            self._edge_pos = self.pos
            wait = min(GIVE_UP_RETRY_SEC * 2 ** (self._edge_fails - 1), GIVE_UP_RETRY_MAX)
            self._retry_at = now + wait
            self._note(f"home at the edge of travel: bowls at {-self.pos:+.2f}, retry in {wait:.0f} s")
            log.info("patrol: %+.2f width to home, but the camera goes no further (%s); "
                     "staying at the edge, retry in %.0f s (attempt %d)",
                     -self.pos, gave_up, wait, self._edge_fails)
        elif gave_up:
            self._retry_at = now + GIVE_UP_RETRY_SEC
            self._note("home not found, staying where it stopped")
            log.warning("patrol: failed to return home (%s, offset %+.2f width, "
                        "no bowls in frame)", gave_up, self.pos)
            # The counter has already proven it lies: keeping it means
            # accumulating error from cycle to cycle, as happened in production.
            self._lost("return failed")
        else:
            self._retry_at = 0.0
            self._edge_pos = None
            self._edge_fails = 0
            log.info("patrol: home, bowls in frame" if seen
                     else "patrol: assuming home, but no bowls in frame")
        # Between sectors home is a pass-through, not a stop: the owner asked for
        # "come back and then go left". If we lingered here, `_home` would see the
        # expired timer and start the cycle again, repeating the right sector forever.
        if self._queue:
            self._start_sector(now)
            return
        self.status = ("home: bowls in frame" if self.home_confirmed
                       else "home not confirmed: bowls not visible")

    # -------------------------------------------------------------- search

    def _start_cycle(self, now: float) -> None:
        # A sector towards the side where the camera hit the edge of travel would
        # end before it starts, and the cycle would be wasted. Start with the free
        # side; it also restores travel on the closed one: the firmware limit
        # releases exactly as many pulses as the camera moved back (log 2026-09-08
        # 06:00:35: eight pulses left, then eight to the right went through).
        self._queue = (["left", "right"] if self._at_edge("right", now)
                       else ["right", "left"])
        self._cycle_pulses = 0
        log.info("patrol: no dog for %.0f s, starting search",
                 now - (now if self._last_dog is None else self._last_dog))
        self._start_sector(now)

    def _start_sector(self, now: float) -> None:
        self.direction = self._queue.pop(0)
        self.state = "scan"
        self._stops_done = 0
        self._pending = self._pulses[self.direction]
        self._dwell_until = 0.0
        self._frames = 0
        self.status = f"searching for the dog: sector {self.direction} 0/{self.steps}"

    def _finish_sector(self, now: float, why: str) -> None:
        if self._queue:
            self._enter_return(why)
        else:
            self.next_scan_at = now + self.idle_sec
            self._enter_return(why + ", cycle complete")

    def _scan(self, now: float, scene: Scene) -> None:
        if self._at_edge(self.direction, now):
            # Don't clear the edge: the return that starts next will see it and
            # won't hit the same limit a second time. _arrive clears it.
            self._note(f"edge of travel ({self.direction}), sector ended early")
            log.info("patrol: edge of travel %s, sector ended", self.direction)
            self._finish_sector(now, "edge of travel")
            return
        if self._over_budget(now):
            return

        if self._pending > 0:
            if self._pulse(self.direction, now):
                self._pending -= 1
                if self._pending == 0:
                    self._dwell_until = now + self.dwell_sec
                    self._frames = 0
            return

        # The pause at a stop. It is not counted by the clock alone: after a pan
        # the stream freezes for 6-19 s, and "search found nothing" based on blind
        # frames would be a lie. Wait for both the clock and live frames.
        if scene.settled:
            self._frames += 1
        if now < self._dwell_until or self._frames < self.dwell_frames:
            return
        self._stops_done += 1
        self.status = f"searching for the dog: sector {self.direction} {self._stops_done}/{self.steps}"
        if self._stops_done >= self.steps:
            log.info("patrol: sector %s done, no dog", self.direction)
            self._finish_sector(now, f"sector {self.direction} done")
            return
        self._pending = self._pulses[self.direction]

    # ------------------------------------------------------------- helpers

    def _pulse(self, direction: str, now: float) -> bool:
        if not self.cam.step(direction):
            return False
        self._cycle_pulses += 1        # count ALL pulses: both sector and return
        self._last_pulse_ts = now
        self._bump(direction)
        return True

    def _bump(self, direction: str) -> None:
        self.pos += RIGHT_FRAC if direction == "right" else -LEFT_FRAC
        if abs(self.pos) > MAX_OFFSET:
            self._lost(f"offset {self.pos:+.1f} width is physically impossible")

    def _lost(self, why: str) -> None:
        """The counter can no longer be trusted. Say so out loud and zero it:
        accumulated lies are more dangerous than not knowing, since they send
        the camera the wrong way."""
        log.warning("patrol: odometry lost (%s); home will be redefined by the bowls", why)
        self._note("odometry lost: " + why)
        self.pos = 0.0
        self.pos_known = False
        self.home_confirmed = False
        self._return_budget = None

    def _over_budget(self, now: float) -> bool:
        if self._cycle_pulses < self.max_pulses:
            return False
        self._note("safety cutoff: per-cycle pulse limit exhausted")
        log.warning("patrol: safety cutoff, %d pulses in a cycle, motion stopped",
                    self._cycle_pulses)
        self._queue.clear()
        self._pending = 0
        self._cycle_pulses = 0
        self._halt_until = now + self.idle_sec
        self.next_scan_at = self._halt_until
        self.state = "home"
        self.status = "safety cutoff: standing until the next cycle"
        return True

    def _note(self, text: str) -> None:
        self.notes.append(text)
        del self.notes[:-NOTES_KEPT]

    # The anchor survives a restart: it can only be relearned at home, and after
    # a restart the camera may be looking anywhere.
    def _load_ref(self) -> None:
        if not self.ref_path or not self.ref_path.exists():
            return
        try:
            data = json.loads(self.ref_path.read_text())
            v = data.get("fridge_abs")
            self.fridge_abs = float(v) if v is not None else None
        except Exception as e:
            log.warning("patrol: anchor not read (%s), will relearn", str(e)[:60])

    def _save_ref(self) -> None:
        if not self.ref_path:
            return
        try:
            self.ref_path.parent.mkdir(parents=True, exist_ok=True)
            self.ref_path.write_text(json.dumps({"fridge_abs": self.fridge_abs}))
        except Exception as e:
            log.warning("patrol: anchor not saved: %s", str(e)[:60])
