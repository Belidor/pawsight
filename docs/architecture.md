# Architecture and key decisions

This document explains *why* the project looks the way it does. Each section
states a decision and the measurement or incident behind it. Read it before
changing anything in the video path, the zones, the camera control or the
training gate: most of these choices exist because the obvious alternative
failed in a way that was silent.

## Pipeline

```
camera ──RTSP (or Xiaomi P2P)──> go2rtc (native)
                                    │ RTSP 127.0.0.1:8554
                                    ▼
          YOLO26s + BoT-SORT (detector, native, GPU)
                                    │ MQTT: ~90 bytes of JSON per frame,
                                    │ a JPEG only when a dog is in view
                                    ▼
      brain: zones, track heuristics, VLM (Ollama), SQLite, Telegram, dashboard
```

The boundary between the detector and the brain is *observations*, not pixels.
That keeps the brain small (the image is ~160 MB, without torch, ultralytics or
OpenCV) and lets it run in a container while the GPU-bound parts stay native.

## Native vs. container

Docker Desktop on macOS runs a Linux VM without Metal, the Neural Engine,
`/dev/dri` or hardware HEVC decoding. Measured on the same frame: **17.8 ms per
frame natively vs. 42.4 ms in a container** for detection alone; full
containerisation came out at 90–145 ms per frame against 10–17 ms. Apple's own
container runtime was evaluated too and rejected: no GPU either, no compose, no
restart policy.

So the split is:

| native | container (optional) |
|---|---|
| go2rtc (talks to the camera on the LAN) | mosquitto |
| detector (YOLO on the GPU) | brain: zones, heuristics, VLM orchestration, SQLite, Telegram, dashboard |
| Ollama (GPU) | |

A `native` stack (everything on the host) is also supported and selected with
`PAWSIGHT_STACK=native` in `.env`.

## Video ingest

- **go2rtc** is the single consumer of the camera. For Xiaomi PTZ cameras with
  no RTSP it speaks the vendor P2P protocol (`xiaomi://`) natively, without
  proprietary blobs or custom firmware. Caveat: on every connect go2rtc fetches
  encryption keys from the Xiaomi cloud; the video itself stays on the LAN.
- **Patched receive buffer.** go2rtc v1.9.14 sizes the Xiaomi P2P media queue
  at ~120 KB, while a 2K HEVC I-frame after a scene change is 200–500 KB. An
  overflow made go2rtc reconnect to the camera, which froze video for 6–19 s
  after every pan. `patches/go2rtc-v1.9.14-cs2-popbuf.diff` enlarges the queue
  and makes overflow non-fatal (drop the chunk, count it, log it at most every
  5 s). Measured after the patch: 9 pans in 90 s, 0 gaps, 0 reconnects.
- **The detector reads the stream itself.** The ultralytics loader spun faster
  than the stream and starved its buffer. The detector `grab()`s every frame
  (cheap, no full decode) and `retrieve()`s only the 4–5 fps it needs. Decoding
  everything made the reader fall behind after I-frame bursts and pushed back
  into go2rtc.
- **Snapshots come from the detector**, never from a new RTSP connection. An
  extra consumer causes gaps for everyone, and a new consumer starts at the last
  keyframe, i.e. up to ~2 s in the past. Telegram snapshots carry the frame age
  in the caption when it is older than 3 s.
- **Dashboard video uses MSE through a loopback proxy, not WebRTC.** With the
  browser and go2rtc on the same machine, ICE picked a pair via the router's
  public IP (NAT hairpin), adding ~600 ms of jitter buffer. STUN is disabled;
  MSE over loopback gives ~1 s of latency without touching the router.

## Detection and tracking

- **Input 1280×720, `imgsz=960`.** Downscaling the input is ~25% faster with no
  loss; lowering `imgsz` to 640 dropped dog confidence from 0.70 to 0.57.
- **Larger models are worse here.** On a hard pose (dog lying under a chair)
  `yolo26s` scored 0.49, `yolo26m` 0.20 and `yolo26l` 0.06.
- **BoT-SORT, not ByteTrack**, because it compensates for camera motion
  (`gmc_method: sparseOptFlow`) and the camera pans.
- **Track threshold 0.15.** BoT-SORT's second stage keeps an already tracked dog
  through confidence dips; a higher pre-filter defeated that.
- **Classes are resolved by name** from `model.names`, never by COCO index, so
  any model (80-class COCO or the 3-class fine-tune) works.
- No events are emitted while the camera is moving: optical flow from a pan is
  indistinguishable from a running dog.

## Zones

- **Zones are anchored to objects, not pixels.** The camera pans, so fixed
  coordinates are wrong after the first move. Zones are computed from the bowls
  YOLO finds in the frame and travel with the camera. No anchor in view means
  no zone.
- **Bowls are selected by left/right order**, which panning preserves. Depth
  (near/far) was tried first and was noise: the bowls' bottom edges differed by
  1 px. A single "bowl" is never a zone: cups on a coffee table are detected as
  bowls too, so only a pair standing close together counts.
- **Shapes are polygons on the floor**, given as offsets from the bowl's floor
  contact point, and membership uses the dog's floor contact point, not the box
  centre.
- **Order in `zones.yaml` is logic.** The brain takes the first non-hint zone,
  and the `bowls` trigger deliberately overlaps room zones, so `bowls` must stay
  first.
- **Drinking is measured as visits and time, not volume.** The bowls are ~11 px
  apart and the dog is wider than the gap, so geometry cannot tell water from
  food. The `bowls` zone triggers a visit, the VLM classifies it, the geometric
  hint is stored alongside it, and disagreements are recorded.
- The installer ships `zones.example.yaml`. Which bowl holds water is confirmed
  by a human on the setup page. Agents must not invent zones for a room they
  have not been shown (`AGENTS.md`, section 6).

## VLM

The VLM (Ollama, `qwen3-vl:8b-instruct` by default) answers in three states:
`dog`, `no_dog`, `unknown`. On an empty frame it once confidently described the
dog as "lying down", so the prompt explicitly allows "no dog".

**Only an explicit `no_dog` suppresses a notification.** `unknown` (Ollama down,
timeout) suppresses nothing; otherwise a crashed Ollama would silently switch
off all alerts. The event is stored either way, together with the VLM verdict,
so the dashboard shows why a notification was not sent.

## Camera control (PTZ)

- The supported Xiaomi PTZ model has no pan/tilt in its MIoT spec. The legacy
  miIO command `set_motor` (1=left, 2=right, 3=up, 4=down) is used. The device
  token is derived from the session go2rtc already holds
  (`scripts/get_device_token.py`); no password is asked for.
- **Steps are asymmetric**: one right step moves ~9.6% of the frame width, one
  left step ~16%. "N steps there, N steps back" misses by a wide margin, so
  position is tracked in **fractions of frame width**, not pulses, and
  re-anchored on the bowls whenever they are visible.
- **The built-in motion tracking is disabled** and kept off: it follows any
  motion, including people, and moves tilt. The project's own follow logic
  pans only, follows only the dog, and has a dead zone.
- **Patrol**: home is "the bowl pair centred in the frame". After a few minutes
  without a dog the camera sweeps one sector, returns home, sweeps the other.
- **Safety limits exist because two control loops once fought.** A flickering
  false dog made follow pull left while patrol pulled home to the right: 554
  pans in 40 minutes and an odometry drift of 7 frame widths. Now patrol waits
  before taking the camera from follow, a fuse counts *all* pulses including
  returns, a return has a hard pulse cap, and an offset beyond 3 frame widths
  is declared lost odometry and reset.
- **Edge of travel**: the firmware reports `overflow` explicitly. "The image
  did not move" is not trusted on its own; it gave false edges twice.
- Camera commands go through a worker thread with a queue and a 2 s timeout, so
  a slow camera never blocks the frame loop. `set_motor` is not retried: a
  retry after a lost reply can turn the camera twice.

## Fine-tuning

- **Never fine-tune on a single class.** Training on `dog` alone does not
  degrade the other COCO classes, it *removes* them (`nc 80 → 1`). Every zone
  depends on `bowl`, so this breaks the system at epoch 0 while the dog is
  still detected perfectly. Training always uses `dog`, `bowl` and `person`;
  ultralytics (pinned) carries the pretrained head rows over by class name.
- **Acceptance gate**: per-class `bowl` mAP50 ≥ 0.70, checked separately,
  because the default fitness averages over classes and a dog gain can hide a
  bowl loss. The gate also checks that `bowl` is actually present in
  `ap_class_index`: missing classes get the overall mAP substituted, a
  convincing fake number.
- **Validation split ≥ 10%.** A frozen manifest once left 8 validation images
  for 146 pairs, so the gate was judging almost nothing.
- **Sample selection**: uncertain detections only (confidence 0.25–0.80),
  perceptual-hash dedup, at most 40 frames per hour.
- **Negatives** are an image in `images/` with an *empty* file in `labels/`.
  Auto-mining negatives ("fine-tuned model sees a dog, COCO does not") is
  unsafe: 7 of 23 candidates were real dogs that COCO misses, which is the
  whole reason for fine-tuning. Every negative is reviewed by eye.
- Apple Silicon (MPS) specifics: `amp=False` (bf16 crashes, `amp=True` is a
  silent no-op), `workers=0`.

## Storage

**The SQLite database has exactly one owner.** In docker mode the brain owns
`storage/events.db`. SQLite locks do not cross the Docker Desktop VM boundary:
a host process opening the file once recreated the `-wal`/`-shm` files, after
which the dashboard returned 500 for hours and the brain wrote 23 minutes of
observations into an orphaned WAL without a single error.

- `Store` is the only `sqlite3.connect` in the package (enforced by a test).
- A host process refuses to open the brain's database *before* connecting;
  host CLI reports fetch raw rows from the brain over HTTP instead.
- There is deliberately no override switch.
- `Store` fingerprints the WAL files and reports `db_orphaned` on the
  dashboard if they are replaced underneath it.

## Telegram

- **Long polling (`getUpdates`), not webhooks.** A webhook needs an inbound
  port; this project exposes nothing.
- Only chats listed in `TELEGRAM_CHAT_ID` are answered. Strangers get no reply
  at all, not even "access denied", which would confirm the bot is alive. An
  empty list means the command bot does not start.
- Backlog is dropped on start, repeated `/snap`s in one batch collapse into
  one answer, and replies go only to the person who asked.

## Security model

- **Everything listens on 127.0.0.1.** Nothing is exposed to the LAN or the
  internet.
- **go2rtc API/WebUI and MQTT are password-protected** on new installs. A
  go2rtc password is useless without `local_auth: true`, because go2rtc trusts
  loopback by default, which is exactly where DNS-rebinding requests arrive
  from. Existing installs enable this with
  `python -m pawsight.authsetup --go2rtc --mqtt`.
- **Mutating dashboard routes** require a custom header and check `Origin`,
  `Sec-Fetch-Site` and `Host`. aiohttp's `.json()` does not check the content
  type, so a cross-site `text/plain` POST could otherwise pan the camera.
- **No `innerHTML` with untrusted text.** VLM output and bus messages end up in
  the event log.
- **Secrets never reach logs.** `httpx` logs full request URLs, which contain
  the Telegram bot token; its logger is silenced and a redacting filter sits on
  the handlers. Secret config fields use `repr=False`.
- **Secrets are entered by the human**, in the setup page on 127.0.0.1, never
  by an agent. The setup page probes an RTSP URL with its own client (OPTIONS,
  DESCRIBE, both digest variants) *before* saving it, because go2rtc's API
  accepts a dead URL with 200.

## Testing

Tests model the scene physically (asymmetric steps, objects visible only in
frame, fake clock), because a cooperative fake proves nothing. Most incidents
above have a regression test that reproduces the failure before the fix, for
example a flickering false dog that drove patrol and follow into each other.
