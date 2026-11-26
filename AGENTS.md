# Instructions for an AI agent

This file is written for an agent with full access to the machine (Claude Code,
Codex, Cursor and the like). The human can simply say: "install according to
AGENTS.md".

Read it in full before your first action. There are places here where doing
things "the usual way" means silently breaking the system, and the human will
not notice for weeks.

---

## 0. What this project is

Local dog monitoring: IP camera → YOLO (detection and tracking) → zones at the
bowls → VLM analysis of frames → SQLite and Telegram. Everything is computed on
this machine; not a single port is exposed to the outside.

The stack is deliberately **partly native**, and that is not sloppiness:

| Part | Where it lives | Why exactly this way |
|---|---|---|
| go2rtc | native | reaches the camera over the local network directly; from a container the LAN is only visible through NAT |
| detector (YOLO) | native, `.venv` | Metal/MPS is unavailable to a Linux guest: 17.8 ms/frame versus 42.4 ms on CPU; on Linux it uses CUDA when there is an NVIDIA GPU |
| Ollama (VLM) | native | a container on a Mac has no GPU access |
| mosquitto + brain | native (by default) | portable; optionally in Docker, see `PAWSIGHT_STACK` |

Do not "improve" this by moving the detector into Docker. The measurements are
in [docs/architecture.md](docs/architecture.md#native-vs-container).

---

## 1. Hard rules

Breaking any of them is a defect, even if everything "works".

1. **Do not ask for or enter anyone's passwords.** Not for the Mi account, not
   for Telegram, not for the camera. When you reach such a point, stop and ask
   the human to do that step themselves. Homebrew (macOS) or sudo (Linux)
   will ask for the system password on its own, which is fine; you do not type it.
2. **Do not commit `.env` or `go2rtc/go2rtc.yaml`.** The first holds tokens,
   the second either the camera password inside the RTSP URL or the Mi account
   token. Both are in `.gitignore`; do not copy them into other files and do
   not print them to output.
3. **Do not expose anything to the outside.** All services listen on
   `127.0.0.1`. If something does not connect, that is not a reason to change
   the address to `0.0.0.0`.
4. **Do not invent zones for the human.** See section 6: zones describe a real
   room, and made-up coordinates produce plausible lies in the statistics.
5. **Do not fine-tune on a single "dog" class.** See section 7. It silently
   breaks the zones, while dog detection keeps working perfectly.

---

## 2. Installation

### Preliminary check

```bash
uname -s -m          # expected: Darwin arm64, or Linux x86_64 / aarch64
```

Supported: macOS on Apple Silicon (Metal) and Linux on x86_64 or aarch64
(CUDA if `nvidia-smi` works, otherwise CPU). An Intel Mac is not supported: no
Metal and no torch wheels worth having; tell the human so and stop. On Linux
without an NVIDIA GPU, tell the human that detection runs on the CPU and the
VLM will be slow, then continue.

### One command

For a regular IP camera with RTSP:

```bash
./install.sh --rtsp 'rtsp://LOGIN:PASSWORD@ADDRESS:554/STREAM'
```

For a pan/tilt Xiaomi camera (P2P, no RTSP):

```bash
./install.sh --xiaomi
```

For the computer's own camera (MacBook) or a USB webcam:

```bash
./install.sh --webcam            # the first camera
./install.sh --webcam 1          # macOS: index or name; Linux: /dev/videoN
```

It adds `ffmpeg` and writes an `ffmpeg:device?…` source for go2rtc; there is no
password involved. On macOS the first frame triggers the system camera prompt
for the terminal: that click is the human's, not yours. Do not try to grant it
through `tccutil` or the TCC database. Which camera to use is the human's call
too, since only they know which one sees the bowls; the setup page's Webcam
tab lists the cameras and shows a test frame.

Only the camera's owner knows the RTSP address. Do not guess it by brute force
and do not scan the network: ask the human. Common paths for different
manufacturers are listed in `go2rtc/go2rtc.example.yaml`.

If the address is not at hand, that is no reason to stop: install without it
(`./install.sh`), run `./start.sh` and give the human the link
`http://127.0.0.1:8099/setup`. There they enter the address themselves, and the
page checks and saves it. **They** type the password into that form, not you:
see rule 1.

The script is idempotent: if something failed halfway, run it again. It
finishes what is missing and leaves what is already done untouched.

### What it does

Checks for arm64 → installs Homebrew (if missing) → `uv`, `mosquitto`,
`ollama` → creates `.venv` with Python 3.12 arm64 and installs
`requirements.txt` → puts `bin/go2rtc` from the release → creates `.env` and
`go2rtc/go2rtc.yaml` from templates → pulls the YOLO weights and the VLM model
→ runs the self-test.

Slow spots, not hangs: `torch` takes several minutes, `ollama pull` several
gigabytes.

---

## 3. Checking after installation

```bash
.venv/bin/python -m pawsight selftest
```

Reading the output:

| Line | What it means | What to do |
|---|---|---|
| `MPS (Metal): yes` | GPU available | none |
| `MPS: NO` | falls back to CPU, ~2.5x slower | check that `.venv` is arm64: `.venv/bin/python -c 'import platform;print(platform.machine())'` |
| `RTSP: frame received` | the camera serves video | none |
| `RTSP: UNAVAILABLE` | no stream | see section 8 |
| `Ollama: ready` | the VLM answers | none |
| `zones defined: 3` | zones loaded | none |
| `Telegram: NOT configured` | there will be no notifications | normal until step 4; events are written to the database |

`Telegram: NOT configured` is not an installation error. Anything else in red
is an error.

---

## 4. What the human must do, not you

When you get here, give the human **exactly this list** and wait. Do none of
the items yourself.

All of it is entered on the setup page `http://127.0.0.1:8099/setup` (it also
opens at `http://127.0.0.1:8099` while no camera is configured). Your job is to
bring up the server and give the link. You must not type other people's
secrets into that form, fill in "typical" logins, or open it from anywhere but
127.0.0.1; the page itself only accepts requests from loopback.

1. **Telegram.** Message @BotFather → `/newbot` → get a token. Message
   @userinfobot → get the `chat_id`. Enter both on the setup page (step 3) or
   in `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Without this the
   system works, but silently: events pile up in the database.
   There can be several recipients: `TELEGRAM_CHAT_ID` takes a comma-separated
   list. **Each of them must message the bot `/start` themselves**: Telegram
   does not let a bot write first, so sending to such a recipient fails with
   `chat not found`, while the others still get the message.
   The same list is the list of chats allowed to use the bot's commands
   (`/snap`: a frame from the camera right now). You must not extend it on the
   human's behalf: someone else's `chat_id` in that line means access to video
   from their room.
2. **Mi account login**, for Xiaomi cameras only. Run
   `./bin/go2rtc -config go2rtc/go2rtc.yaml`, open `http://127.0.0.1:1984`,
   Add → Xiaomi, log in. The password stays with go2rtc.
   The go2rtc WebUI itself is password-protected (`api.username`/`password` in
   `go2rtc/go2rtc.yaml`, written by `install.sh`): the browser will ask the
   human for them. You do not print the values and do not enter them into the
   form: rule 1.
   After that you can fetch the device token for panning:
   `.venv/bin/python scripts/get_device_token.py`. It does not ask for a
   password, uses the `passToken` saved by go2rtc, and does not print the token
   to the screen.
3. **Confirm which bowl is the water bowl.** See section 6: it is one line, but
   it must not be guessed. On the setup page this is step 2: a button under the
   live video with the bowls labelled. The buttons there stay disabled until
   both bowls are visible in the frame, so confirming blindly is impossible,
   for you too.

---

## 5. Running

```bash
./start.sh     # go2rtc + broker + brain + detector, self-test at the end
./stop.sh
```

Dashboard: `http://127.0.0.1:8099` (from this machine only).
A frame right now without opening the dashboard: the `/snap` command to the
bot in Telegram, which works as soon as step 1 of section 4 is done.

```bash
.venv/bin/python -m pawsight today       # today's events and bowl visits
.venv/bin/python -m pawsight drinks 7    # drinking over 7 days
.venv/bin/python -m pawsight dataset     # how many frames have accumulated for fine-tuning
```

These commands work on the host in both stacks, but they get their data
differently, and that is not an implementation detail. **Only its owner may
touch the database `storage/events.db`.** In docker mode the owner is the brain
in the container, so `today` and `drinks` ask it over HTTP at
`127.0.0.1:8099`; if the container is not up, the command says so honestly and
shows no data: it will not open the database "around" the owner. In native
mode there is no owner, and the commands read the database directly, as
before.

The reason for the ban: SQLite locks do not cross the Docker Desktop VM
boundary, and a second connection from the host leaves the brain with an
orphaned WAL. It keeps writing, but the records vanish, and **there is no error
at all**. This already happened on 2026-09-06: the dashboard returned 500 for
3 h 40 min, and 23 minutes of observations disappeared (see
[docs/architecture.md](docs/architecture.md#storage), "Storage").
For the same reason `pawsight live` and `pawsight batch` refuse to start in
docker mode: that would be a second brain on the same database. Do not bypass
the gate: there is deliberately no variable that disables it.

### How the camera behaves

By default the camera **rests on the bowls**: that is its home position. When
the dog appears in the frame, following kicks in (pan only, never tilt). When
there has been no dog for longer than `PAWSIGHT_SEARCH_IDLE_SEC` (3 minutes by
default), the patrol sweeps the room: a sector to the right in stops, back
home, a sector to the left, back, and after the same timeout the cycle repeats.
If the dog shows up at any point of the sweep, the sweep is abandoned
mid-step.

**The sector is set in motor pulses, not degrees, and that is not sloppiness.**
The camera's field of view has not been measured in this project, and the step
is asymmetric: a pulse to the right shifts the frame by 9.6% of its width, to
the left by 16.1% (backlash when changing direction; see
[docs/architecture.md](docs/architecture.md#camera-control-ptz)). Turning "30°" into pulses is
only possible by inventing a field of view. If the owner asks for degrees,
either measure the full travel in pulses and ask whether it is a full circle,
or keep pulses. Do not silently substitute "roughly 110°".

The camera defines home not as a coordinate (`set_motor` has none) but as a
**picture**: the pair of bowls in the frame, their centre within 10% of the
width from the frame centre. It cannot be more precise: one pulse to the right
is exactly 9.6% of the width. The position is tracked in fractions of the frame
width and is constantly corrected by the bowls, and when they are not visible,
by a refrigerator whose position the camera learned by itself while at home
(`storage/home_ref.json`). None of this is configured by hand.

The dashboard shows the patrol state and the offset from home. A yellow "home
not confirmed" means the camera believes it has returned but there are no bowls
in the frame; that is an honest admission, not an error.

Logs: `storage/detect.log` (detector), `storage/brain.log` (brain),
`go2rtc/go2rtc.log`. The previous run is kept as `*.log.1`; do not delete them,
incidents are analysed from them.

---

## 6. Zones: the place where it is easy to lie

Zones are tied not to pixels but to **anchors** that YOLO finds in the frame
(class `bowl`). So they survive both camera pans and a move to another room;
this is deliberate, so that `zones.yaml` is not rewritten for every scene.

There is one exception, and it needs the human. `zones.yaml` contains:

```yaml
water:  { anchor: { select: left  } }   # water bowl: LEFT
food:   { anchor: { select: right } }   # food bowl:  RIGHT
```

This describes the author's room. If the new owner's bowls stand the other way
round, swap `left` and `right`, **but only after the human confirms**. Checking
by eye on the dashboard is the most reliable; the simplest is to give them the
setup page: there it is one button under the live video, and it also writes a
`water_bowl_confirmed` mark with the date into `zones.yaml`. Without that mark,
"left" only means "that is how the author left it".

Why this is decided so crudely: the bowls stand ~11 px apart, and the dog is
noticeably wider than that gap, so the floor contact point fundamentally cannot
tell which bowl the head is over. The VLM tells drinking from eating; geometry
is only a second opinion, and a disagreement between them is stored in the
database in the `conflict` field rather than hidden.

**The zone shape describes the room too.** Every anchored zone has a `shape`: a
polygon on the floor whose vertices are offsets from the bowl's floor contact
point, in detector frame pixels (640x360). The author **drew these shapes on a
frame**, and the coordinates were taken from that drawing. You do not invent
them and do not "adjust" them: a strip shifted towards the rug by eye produces
statistics that look plausible and lie. If the zones need redrawing (another
room, the bowls were moved), send the human a frame from the camera, get the
drawing back and convert the outlines into offsets from the bowls found on that
same frame; record in `zones.yaml` which frame they were taken from. The
fallback `expand` key (a rectangle around the box) remains for a zone without a
`shape`. The shape belongs to the bowl's position, not its contents, so when
the setup page swaps `left`/`right`, it swaps `shape` too.

Do not add new zones on your own initiative.

The patrol's home position (section 5) follows from this too: a zone only
exists while its anchor is visible, so by default the camera keeps the bowls in
the frame. Only the dog leads it away.

---

## 7. Fine-tuning for your own dog

Details are in [docs/FINETUNE.md](docs/FINETUNE.md). Here is only what an agent
must know before launching anything.

**The main trap.** Fine-tuning on a single "dog" class does not degrade the
other 79 COCO classes, it **erases** them: the head is rebuilt for `nc=1`, and
the `bowl` class ceases to exist. All of the project's zones depend on `bowl`.
It breaks silently: the dog keeps being detected perfectly, while bowl visits
simply disappear from the statistics.

That is why the dataset always has at least three classes, `dog`, `bowl`,
`person` (more can be added after them with `PAWSIGHT_CLASSES`, see
`docs/FINETUNE.md`; never reorder or drop the three), and
`pawsight/train.py` has an acceptance gate: the model is rejected if `bowl`
mAP50 drops below 0.70 or if the `bowl` class is missing from the validation
set altogether.

Fine-tuning can be started from the dashboard, and the only requirement is 50
labelled frames (`MIN_IMAGES`). The button no longer treats a small validation
split as a refusal: `train.py::_split` rebuilds it by itself and records this
in the log and in the run card. Do not bring back a refusal here with advice to
go to the terminal: the split shrinks as the dataset grows, so such a refusal
punished labelling (see
[docs/architecture.md](docs/architecture.md#fine-tuning)).

**Do not lower the threshold in the code**: it exists so that a rejection is
visible. But refusing the human is not your job either: installing a rejected
model by hand is their right, and the project's author did exactly that (see `docs/FINETUNE.md`, "If the gate
rejected it but you still want the model"). Your job is to name
the price: with a degraded `bowl`, bowls are lost more often, and bowl visits
disappear with them. That section has the measurements.

**The human, not you, throws a bad frame out of the dataset.** On the
dashboard, in the "Dataset" tab, an open frame has "Clear all boxes" (wipes
the labels) and "Delete frame" (erases the image and labels from disk, in two
clicks). The latter is irreversible and also strikes the name from
`dataset/val_manifest.json`: otherwise the validation share would be computed
over frames that do not exist, and the acceptance gate would silently stop
checking. The deleted frame's hash stays in `.hashes.json` on purpose: without
it, auto-collection would bring back an almost identical frame within a minute.
This does not extend to bulk cleanup: `dataset/images` and `dataset/labels` are
still on the `cleanup.py` deny list, and moving "let's delete the old ones
while we are at it" in there is not allowed.

One more thing that is easy to forget: a 3-class model does not know furniture
classes, so the camera's tilt hold only works while the pair of bowls is
visible in the frame.

The `ultralytics` version in `requirements.txt` is pinned not out of caution:
carrying the pretrained head's rows over by class name (`cls_remap`) is what
lets `bowl` inherit COCO quality for free. Do not bump the pin without checking
that the carry-over still works.

---

## 8. If it does not work

| Symptom | Cause | What to do |
|---|---|---|
| `selftest`: RTSP UNAVAILABLE | go2rtc did not start or does not know the camera | `curl -s 127.0.0.1:1984/api/streams`; look at `go2rtc/go2rtc.log`; check that `streams.dog_cam` in `go2rtc/go2rtc.yaml` is not empty |
| Webcam: the setup page's list is empty | go2rtc found no ffmpeg or no camera; it reads the list once at start | `brew install ffmpeg` / `sudo apt install ffmpeg`, plug the camera in, `./stop.sh && ./start.sh` |
| Webcam: "gave no frame", mentions permission or "not authorized" | macOS camera access was refused for the terminal that runs pawsight | the human turns it on: System Settings → Privacy & Security → Camera, then `./stop.sh && ./start.sh` |
| Webcam: "Selected framerate/video size … not supported" | the camera has no such mode | pick another size on the setup page |
| Webcam on Linux: "Permission denied" on `/dev/video*` | the user is not in the `video` group | `sudo usermod -aG video $USER`, log in again |
| The RTSP URL is not accepted | the camera uses a different path | paths by manufacturer are in `go2rtc/go2rtc.example.yaml`; look up the exact one in the camera's documentation |
| MPS NO | `.venv` was built for x86_64 under Rosetta | `rm -rf .venv && ./install.sh` |
| Ollama does not answer | the service is not running | `brew services start ollama`, then `curl -s 127.0.0.1:11434/api/tags` |
| The brain does not start | the broker is not up | `nc -z 127.0.0.1 1883`; `brew services list \| grep mosquitto` |
| No events, detector alive | no zones in the frame | a zone only exists while its anchor is visible: `bowl` must be in the frame |
| Video freezes for 6-19 s after a pan | Xiaomi camera without the cs2 patch | `brew install go && ./scripts/build_go2rtc.sh`, then `./stop.sh && ./start.sh` |
| Dashboard is empty | the brain receives no observations | `tail -f storage/brain.log` and `storage/detect.log` |
| Patrol: "home not confirmed" | bowls are not detected from this position | look at the dashboard: are the bowls visible at all; with a fine-tuned model check that the `bowl` class is alive (section 7) |
| The camera keeps turning without stopping | the sector is too large or the timeout too short | `PAWSIGHT_SCAN_STEPS`, `PAWSIGHT_SEARCH_IDLE_SEC`; the `PAWSIGHT_SCAN_MAX_PULSES` safety limit shows up in the log as "safety cutoff" |
| The sweep runs but does not find the dog | stops are shorter than the gap in the stream | `PAWSIGHT_SCAN_DWELL_SEC`; the gaps are fixed by the cs2 patch (row above) |
| The camera jerks left and right, the motor never stops | following chases a false dog while the patrol pulls home | the `PAWSIGHT_PATROL_RELEASE_SEC` pause fixes this; if it recurs, look at what the model mistakes for a dog (see [docs/architecture.md](docs/architecture.md#camera-control-ptz), "Safety limits") |
| "odometry lost" in the log | the counter diverged from reality and honestly reset itself (only when no bowls are in the frame) | not an error by itself; home is redefined once the bowls are in the frame |
| Status "home at the edge of travel: bowls at +0.30 … retry in N s" | the camera answers `overflow` while the bowls are visible: the firmware does not let it get closer | honest, not an error. The firmware limit drifts; the patrol retries less often and right after the camera moves; the "home at the edge of travel: bowls at +0.xx" lines in the camera feed are a measurement of that drift. Do not touch `home_deadband` |
| The dashboard's "right" button answers "camera is at the right edge of travel" | the same firmware `overflow`; a manual pan does not bypass it | pan the other way; if there is clearly room on the right, suggest the owner reboot the camera (pan counter) |
| "follow: dog vanished (…) -> not chasing" in the log when the dog leaves | the last box was near the centre or was computed to be there after a step | look at the fields of the line: trajectory, raw dog boxes; if there are no boxes, the tracker did not confirm the dog (`trackers/botsort-dog.yaml`; see [docs/architecture.md](docs/architecture.md#detection-and-tracking)) |
| The bot is silent on `/snap` | commands did not start | `grep "bot accepts commands" storage/brain.log`; empty means `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are not set |
| `/snap` answers "the detector did not answer" | no detector or broker | `pgrep -f "pawsight detect"`, `nc -z 127.0.0.1 1883` |
| `Telegram answered 409 to an updates poll` | two processes poll the same token | keep one mode: either `live` or `detect`+`brain` |
| The setup page opened instead of the dashboard | `streams.dog_cam` in `go2rtc/go2rtc.yaml` is empty | by design; the dashboard is at `http://127.0.0.1:8099/?dashboard=1` |
| The setup page does not write files | the brain is in a container, and `.env` and `go2rtc/` are not mounted into it | run the wizard on the host: `.venv/bin/python -m pawsight setup` |
| The dashboard or setup page answers "foreign Host" | opened via something other than `127.0.0.1`/`localhost` | by design: loopback is reachable from any foreign page, and this one is where the camera password is entered |
| `pawsight today`/`drinks`: "The brain did not respond on 127.0.0.1:8099" | docker stack, and the brain container is not up: it holds the database | `docker compose ps`; bring it up with `./start.sh`. Opening the database directly is not allowed, see section 5 |
| `ForeignDatabase` on `pawsight live`/`batch`/`brain` | that is a second brain on a database owned by the container | either work through the running container or stop it first: `./stop.sh` |
| "SQLite side files … were swapped from outside" in the log | a second process did touch the database after all: the brain's writes go nowhere, silently | restart the brain (`docker restart pawsight-brain-1`) and find who opened the database; analysis in [docs/architecture.md](docs/architecture.md#storage) |
| Dashboard alive, but "database unavailable" instead of numbers | `api_state` no longer returns 500 because of the database, it shows the cause in the `db_error` field | read the error text: it names the cause; the row above is the most common case |

Diagnostic sequence when it is unclear where things break:
does go2rtc serve the stream? → does the detector read frames? → does the brain
receive observations over MQTT? → are events written to the database? Check in
exactly this order.

---

## 9. Report to the human

When you are done, say briefly and to the point:

- what actually works (with lines from `selftest`, not "all good");
- what does not work and why;
- what is left for the human (section 4);
- which files you changed.

Do not report success without running `selftest` and looking at its output.
Never put token, password or RTSP URL values in the report.
