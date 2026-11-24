# Patrol: home position on the bowls and sector search (2026-09-06)

Design approved by the owner. Replaces the old search, "one step every 10
seconds, swinging between the edges" (`follow.py:_search`).

## What the owner wants

By default the camera looks at the bowls. No dog for three minutes: scan a
sector to the right in steps with pauses; if not found, return home and do the
same to the left. Dog found at any moment: follow.

## What the hardware gets in the way of

Three facts the whole design grew out of. None of them can be worked around
with "careful code"; each had to be built into the scheme.

1. **There is no absolute position.** `set_motor` (`ptz.py`) only takes a
   direction and returns nothing about the position. The "starting position"
   is not a coordinate but what we remember about ourselves and confirm by
   eye.
2. **A step is not an angle, and it is asymmetric:** right ≈221 px, left
   ≈372 px of a 2304 px frame (calibration 2026-09-06; see
   [docs/architecture.md](../../architecture.md#camera-control-ptz)). Backlash when changing direction is the reason for
   the difference. So "N pulses there, N back" misses by 68%, and degrees
   cannot be computed at all without measuring the field of view.
3. **The field of view has not been measured.** So the config and the logs use
   pulses and fractions of the frame width, not degrees. There is no made-up
   "30°" in the code.

## The owner's decisions

| Question | Decision |
|---|---|
| Dog is visible but not at the bowls | **The dog matters more.** Following as today; home only when there is no dog in the frame. "3 minutes not in the zone" = "3 minutes not in the frame" |
| Where do degrees come from | **Nowhere.** The sector and the step are set in pulses in `.env` |
| How to tell we are back | **Hybrid:** roughly by odometry, precisely by the bowls |
| What after both sectors | **Home, repeat the cycle after 3 minutes** |
| What counts as home | **Bowls in the centre of the frame**, the refrigerator is an auxiliary landmark |

## State machine

States: `FOLLOW` · `HOME` · `RETURN` · `SCAN`.

* The dog appears in the frame: immediately `FOLLOW`; scanning is abandoned
  mid-step.
* The dog is lost: today's catch-up at the edge (up to 3 steps, 6 s window),
  then `RETURN`.
* `HOME` without a dog for longer than `SEARCH_IDLE_SEC`: sector to the right →
  `RETURN` → straight into the sector to the left → `RETURN` → the timer is
  re-armed.
* The timer counts from the **dog's last appearance in the frame**, not from
  arriving home: otherwise a long return would eat part of the three minutes.

## Odometry in fractions of the frame width

The position is the offset from home in fractions of the width: a pulse to the
right is `+221/2304`, to the left `−372/2304`. Returning from point `P` takes
`P / 0.1615` left pulses (or `|P| / 0.0959` right ones), fired in one burst
without scan pauses: the only pause between pulses is the motor's own
(`PtzWorker.gap`). To the owner this reads as one continuous movement, which is
what they asked for; the camera cannot do "one movement" in the literal sense.

The defaults follow from this too: 3 pulses per step to the right, 2 to the
left, so both sides sweep ≈1.2 frame widths in 4 stops. Sector symmetry matters
more than round numbers.

## Home

**Criterion:** the pair of bowls in the frame with its centre within
`HOME_DEADBAND` (0.10 of the width) of the frame centre. We do not promise more
precision: a step to the right is itself 0.096 of the width, and asking for
less causes oscillation. Fine adjustment is limited to three pulses; the
remainder is written into the status rather than hushed up.

**Self-calibration by the refrigerator.** While at home, the camera remembers
the refrigerator's offset relative to the bowls in fractions of the width and
stores it in `storage/home_ref.json`. On the way back, if the bowls are not
visible yet but the refrigerator is, the target is computed from it. Not learned
or not visible: we work from the bowls alone. Coordinates are never invented
under any circumstances ([AGENTS.md](../../../AGENTS.md), section 6).

There can be several refrigerator candidates (at a low threshold YOLO confuses
other white appliances with it), so the patrol chooses: by where the landmark
must be, and only within 0.35 of the width. (Measured on 12 dataset frames: the
refrigerator sits at +0.016..+0.027 of the width from the bowls, but on one
frame YOLO reported a second, more confident "refrigerator" 0.69 widths away.)

**Degradation:** a fine-tuned three-class model (`dog`/`bowl`/`person`) has no
`refrigerator` class at all ([AGENTS.md](../../../AGENTS.md), section 7). Then
there is simply no second reference, and the return works from the bowls.

## Three places where this would break silently

**The tilt loop.** Today every pan resets the reference
(`detector.py:_after_pan`). A 12-pulse sector every three minutes would never
let the reference settle: tilt hold would die, and the owner would not notice
right away. So during `SCAN`/`RETURN` the loop is suspended, and the reference
is re-captured once: at home, on a settled frame.

**The pause at a stop.** After a pan, a camera without the cs2 patch freezes
for 6–19 s ([AGENTS.md](../../../AGENTS.md), section 8). A pause by the clock
alone would mean the camera goes through the whole sector blind and "does not
find" the dog without ever looking at it. So a stop lasts `SCAN_DWELL_SEC`
**and** until at least `SCAN_DWELL_FRAMES` fresh settled frames have arrived.
If the stream is frozen, we wait for frames rather than step.

**The step limit.** `MAX_STEPS_PER_MIN=20` would block the sector halfway. For
scanning it is lifted, but not into a void: instead there is a
`SCAN_MAX_PULSES` per-cycle safety limit, so that a bug cannot drive the motor
forever.

## Parameters (`.env`)

| Parameter | Default | Meaning |
|---|---|---|
| `PAWSIGHT_HOME_HOLD` | `1` | the new logic; `0` means the old behaviour |
| `PAWSIGHT_SEARCH_IDLE_SEC` | `180` | how long to wait at home without a dog before searching |
| `PAWSIGHT_SCAN_STEP_RIGHT` | `3` | pulses in one step to the right |
| `PAWSIGHT_SCAN_STEP_LEFT` | `2` | pulses in one step to the left |
| `PAWSIGHT_SCAN_STEPS` | `4` | stops in a sector |
| `PAWSIGHT_SCAN_DWELL_SEC` | `5` | pause at a stop |
| `PAWSIGHT_SCAN_MAX_PULSES` | `60` | per-cycle safety limit |

## Files

`pawsight/patrol.py` (the new state machine), `pawsight/follow.py` (hands over
control when there is no dog), `pawsight/config.py`, `pawsight/detector.py`
(wiring, pausing the tilt loop), `pawsight/static/dashboard.html` (status),
`.env.example`, `tests/test_patrol.py`, documentation.

## Verification

The state machine is tested against a fake camera and a fake clock, without
hardware: return instead of scanning before the timer expires; exactly N stops;
the sector is abandoned when the dog appears; right → home → left → home →
timer again; hitting the edge cuts the sector short and does not jam; odometry
compensates for the asymmetric step; the safety limit triggers; the
refrigerator is learned and applied, and its absence breaks nothing.
