# pawsight

![pawsight: a dog drinking, with the zones pawsight sees](docs/images/banner.png)

[![tests](https://github.com/Belidor/pawsight/actions/workflows/ci.yml/badge.svg)](https://github.com/Belidor/pawsight/actions/workflows/ci.yml)
[![licence: AGPL-3.0](https://img.shields.io/badge/licence-AGPL--3.0-blue.svg)](LICENSE)

**Know how much your dog drinks and eats, and where it spends the day, without
sending a single frame to the cloud.**

pawsight watches an ordinary IP camera (or your MacBook's own camera, or a USB
webcam), finds your dog and its bowls, counts
visits to the water and food bowls, and tells you in Telegram what happened.
Everything runs on your own Mac or Linux box: IP camera → YOLO → zones → local
VLM → SQLite and Telegram. Not a single port is exposed to the outside.

- **Drinking and eating, counted.** Each visit to the bowls is recorded and
  reported as "🐕 drank — 40 s"; a local vision-language model tells drinking
  from eating.
- **[Zones you draw yourself](docs/getting-started.md#4-draw-your-zones)**, anchored to the bowls so they follow a pan/tilt
  camera, each with its own Telegram switch.
- **Telegram** notifications, and a fresh frame on `/snap`, answered only in your chats.
- **Pan/tilt Xiaomi cameras** follow the dog and return to the bowls; any
  RTSP camera works with presets for Tapo, Reolink, Hikvision, Dahua, EZVIZ and Imou.
- **No IP camera?** The MacBook's built-in camera or any USB webcam works too
  (`./install.sh --webcam`).
- **Fine-tuning on your own dog** from the dashboard, with an acceptance gate
  that refuses a worse model.
- **Private by design**: video, frames and the database never leave the machine.

## At a glance

<table>
<tr>
<td width="50%"><img src="docs/images/following.jpg" alt="A pan/tilt camera following the dog across the room"><br><b>Follows the dog.</b> A pan/tilt camera keeps the dog in frame and returns to the bowls.</td>
<td width="50%"><img src="docs/images/telegram.jpg" alt="A phone showing a Telegram message about a drink"><br><b>Telegram, not an app.</b> Visits and zones arrive as short messages in your own chat.</td>
</tr>
<tr>
<td><img src="docs/images/private.jpg" alt="A house with the camera, the computer and the dog inside"><br><b>Everything stays at home.</b> Video, frames and the database never leave the machine.</td>
<td><img src="docs/images/finetune.jpg" alt="A grid of training frames and an accepted model"><br><b>Learns your dog.</b> Fine-tune from the dashboard; the gate refuses a worse model.</td>
</tr>
<tr>
<td colspan="2"><img src="docs/images/platforms.jpg" alt="macOS and Linux, CUDA or CPU"><br><b>macOS or Linux.</b> Metal on Apple Silicon, CUDA with an NVIDIA GPU, or just the CPU.</td>
</tr>
</table>

## Quick start

You need a Mac on Apple Silicon or a Linux machine (Debian/Ubuntu; an NVIDIA GPU helps but is not required). One command from scratch, then you only grant permissions:

[![Watch the install and first setup, start to finish (2 min 20 s)](docs/images/setup-walkthrough.jpg)](https://pawsight.belidor.cc/#setup-video)

*The whole install and first setup in 2 minutes 20 seconds, recorded on a clean Ubuntu machine: the installer, the camera, the water bowl, zones and the live view. The dog in the clip is AI-generated; every box comes from pawsight itself. [Watch it](https://pawsight.belidor.cc/#setup-video) or [download the MP4](https://pawsight.belidor.cc/img/setup.mp4).*


```bash
git clone https://github.com/Belidor/pawsight.git && cd pawsight && ./install.sh --rtsp 'rtsp://LOGIN:PASSWORD@ADDRESS:554/STREAM'
```

```bash
./start.sh
```

Dashboard: `http://127.0.0.1:8099`

No IP camera? Use the computer's own camera or a USB webcam instead:
`./install.sh --webcam` (details in [Camera](#webcam-macbook-or-usb)).

Don't have the camera address at hand? Install without it (`./install.sh`) and
enter everything in the browser: `./start.sh`, then `http://127.0.0.1:8099`.
Until a camera is configured, that address opens the **setup page**: the camera
address (with a check: does it answer, is the path right, is the password
right), Telegram, and confirming which bowl is the water bowl. The page is only
reachable from this machine.

The installer sets up Homebrew, `uv`, Python 3.12 arm64, the dependencies,
go2rtc, mosquitto, Ollama and the models, creates `.env` and runs a self-test.
It is idempotent: if it fails halfway, just run it again.

**Handing the machine to an AI agent?** Tell it "install according to
AGENTS.md". [AGENTS.md](AGENTS.md) is written for exactly that, together with
the rules for what the agent must not do.

Three things the installer will not do for you, because they are your secrets
and your decision: the Telegram bot token, the Mi account login (for Xiaomi
cameras) and confirming which bowl is the water bowl. It will take you up to
those points and tell you what is needed; you can enter them on the setup page
`http://127.0.0.1:8099/setup`.

## Architecture

```
camera ──RTSP (or P2P for Xiaomi)──> go2rtc (native, mac_arm64)
webcam ──ffmpeg (avfoundation/v4l2)──┘  │ RTSP 127.0.0.1:8554
                                        ▼
   Metal (MPS) <───── YOLO26s + BoT-SORT ───── pawsight.detect (Python 3.12 arm64)
                                        │ MQTT
                ┌───────────────────────┼─────────────────┐
                ▼                       ▼                 ▼
     anchor-based zones          track heuristics      Ollama (Metal)
     (move with the camera)      speed/pose/time       qwen3-vl:8b
                └───────────────────────┼─────────────────┘
                                        ▼
                              SQLite + Telegram + dashboard
```

**Why part of it is native rather than all in Docker.** Docker Desktop on a Mac
gives you a Linux VM without `/dev/dri`, without Metal and without the Neural
Engine. A containerised detector would run on CPU only: measured at **17.8
ms/frame versus 42.4 ms**. And go2rtc has to live on the host, because it
reaches the camera over the local network, which a container only sees through
NAT.

Exactly three things are native: go2rtc, the detector and Ollama. The broker
and the brain are your choice (`PAWSIGHT_STACK` in `.env`): `native` means
our own password-protected mosquitto and the brain in `.venv`, no Docker needed at all;
`docker` means both run in containers.

## Requirements

- macOS on Apple Silicon (tested: M3 Max, macOS 26.5), **or**
- Linux on x86_64 or aarch64. The installer handles Debian and Ubuntu with
  `apt`; on other distributions it lists the packages to install. With an
  NVIDIA GPU (driver installed, `nvidia-smi` works) the detector uses CUDA,
  otherwise the CPU, which is enough for one camera at 5 fps on a modern
  processor. The VLM (Ollama) is the heavy part on a CPU-only box.
- a camera that serves RTSP, a pan/tilt Xiaomi over P2P, or a webcam: the
  MacBook's built-in camera or a USB one (needs `ffmpeg`, installed by
  `./install.sh --webcam`)
- ~10 GB of disk: torch, YOLO weights and the VLM model

The installer installs Python 3.12 itself via `uv`. The system
`/usr/local/bin/python3*` on many Macs is x86_64 under Rosetta, and torch does
not ship wheels for Rosetta. `PAWSIGHT_DEVICE=auto` (the default) picks Metal,
then CUDA, then the CPU; set `mps`, `cuda:0` or `cpu` in `.env` to force one.
On Linux the broker and the brain always run natively.

## Camera

### Plain RTSP

```bash
./install.sh --rtsp 'rtsp://user:pass@192.168.1.50:554/stream1'
```

Or on the setup page `http://127.0.0.1:8099/setup`, where the address can be
assembled from parts (login and password are encoded for you, whereas an `@` or
`/` in the password breaks a hand-built URL) and checked right away. The check
uses its own RTSP client and reports the cause, not just the fact: the port is
closed, the path is wrong, the camera rejected the password, this port is not
RTSP at all.

Every manufacturer has its own path; common ones are collected in
[go2rtc/go2rtc.example.yaml](go2rtc/go2rtc.example.yaml). The password only ends
up in `go2rtc/go2rtc.yaml`, which is in `.gitignore`; it is never shown back,
neither on the page nor in the logs.

Such a camera cannot pan, so the installer turns off tilt hold, following and
search (`PAWSIGHT_TILT_HOLD`, `PAWSIGHT_FOLLOW_DOG`, `PAWSIGHT_SEARCH_SEC`).

By default the camera looks at the bowls and returns to them as soon as the dog
leaves the frame. No dog for three minutes: it sweeps the room in a sector to
the right, then to the left, and waits again (`PAWSIGHT_HOME_HOLD`,
`PAWSIGHT_SEARCH_IDLE_SEC`). The sector is set in motor pulses, not degrees: the
camera's field of view has not been measured, and degrees would be a made-up
number.

### Webcam (MacBook or USB)

```bash
./install.sh --webcam          # the first camera
./install.sh --webcam 1        # macOS: another camera by index or by name
./install.sh --webcam /dev/video2   # Linux
```

Or on the setup page, **Webcam (MacBook / USB)** tab: it lists the cameras
go2rtc can see, takes a test frame and shows it, then saves. There is no
address and no password. go2rtc opens the camera through ffmpeg
(avfoundation on macOS, v4l2 on Linux), encodes it to H.264 and serves it as
the same `rtsp://127.0.0.1:8554/dog_cam`, so the detector and the dashboard
work exactly as with an IP camera. The installer adds `ffmpeg` in this mode.

What to know:

- **macOS asks for camera access** the first time the camera opens (the setup
  page's test button, or `./start.sh`), on behalf of the terminal that started
  pawsight. Allow it; if you said no, turn it on in System Settings → Privacy &
  Security → Camera and restart pawsight.
- **Linux**: the user running pawsight needs to read `/dev/video*` (group
  `video`: `sudo usermod -aG video $USER`, then log in again). USB cameras are
  read as MJPEG where they offer it: raw 720p rarely fits USB 2.0.
- The camera is **on all the time** while pawsight runs (its light stays lit),
  and the computer must not sleep. A laptop has to stand where it sees both
  bowls, which is the hard part: an external USB camera on a long cable is
  usually easier to place.
- A webcam cannot pan: as with a plain RTSP camera, tilt hold, following and
  search are off.
- go2rtc reads the list of cameras once, when it starts: after plugging in a
  USB camera, run `./stop.sh && ./start.sh`.

### Pan/tilt Xiaomi over P2P

```bash
./install.sh --xiaomi
```

**You** log in to the Mi account in the WebUI at `http://127.0.0.1:1984`
(Add → Xiaomi). The password does not end up in the repository: go2rtc stores
an account token in `go2rtc/go2rtc.yaml`, and that file is in `.gitignore`.
There is no field for the Mi account password, and there never will be, either
in the installer or on the setup page: that login belongs to go2rtc's own WebUI.

Nothing goes to the cloud except one thing: go2rtc fetches encryption keys from
Xiaomi for every connection. The video itself only travels over the local
network.

Panning needs a separate token, the **device** token, not the account one:

```bash
.venv/bin/python scripts/get_device_token.py     # does not ask for a password
```

## Running

```bash
./start.sh                              # everything at once, self-test at the end
./stop.sh

.venv/bin/python -m pawsight setup        # setup page only, no broker

.venv/bin/python -m pawsight selftest     # MPS, RTSP, Ollama, zones, Telegram
.venv/bin/python -m pawsight today        # today's events and bowl visits
.venv/bin/python -m pawsight drinks 7     # drinking over 7 days
.venv/bin/python -m pawsight dataset      # what has accumulated for fine-tuning
.venv/bin/python -m pawsight batch DIR    # process a folder of mp4 files
.venv/bin/python -m pawsight ptz left     # pan (Xiaomi only)
```

### Frame on request in Telegram

Once Telegram is configured, the bot accepts commands, and `/snap` sends what
the camera sees **right now**, without the dashboard and without a VPN:

| command | what it does |
|---|---|
| `/snap` (`/frame`, `/photo`, "snapshot") | a fresh frame from the camera |
| `/help` | short help |

The bot only answers chats listed in `TELEGRAM_CHAT_ID`, and only the one that
asked. It does not answer strangers at all: silence does not confirm to someone
who typed the bot's name by chance that there is somebody's home behind it.

The frame is taken from the detector, not through a new connection to the
camera: an extra consumer of the 2K stream over P2P causes gaps in the video for
everyone. So the snapshot arrives at the detector stream's resolution, and if
the stream has frozen, with a note saying how many seconds old the frame is.
Details and measurements are in
[docs/architecture.md](docs/architecture.md#video-ingest).

## Fine-tuning for your own dog

Frames accumulate on their own while the detector runs. Once you have 200-400,
see [docs/FINETUNE.md](docs/FINETUNE.md).

One warning is worth reading before you start: fine-tuning on a single "dog"
class **erases** the other 79 COCO classes, including `bowl`, which all the
zones depend on. It breaks silently: the dog is detected perfectly, while the
bowl visits disappear. That is why there are always three classes (`dog`,
`bowl`, `person`), and `pawsight/train.py` has an acceptance gate.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

Covered: Telegram delivery (parsing the recipient list, reusing `file_id`,
behaviour when one recipient refuses), bot commands (who is allowed, collapsing
repeats, an honest answer when there is no frame), frame exchange between the
detector and the brain over the bus, and the initial setup: building an RTSP
address with escaping, writing `.env` and `go2rtc.yaml` without losing other
keys, editing `zones.yaml` while keeping comments, rejecting a request from a
foreign tab, and making sure the password ends up neither in the response nor in
the logs. The camera probe is tested against its own toy camera with all three
kinds of authentication.

The tests touch neither the network nor the broker: HTTP and the bus are
replaced entirely, and the RTSP server and go2rtc are started locally on
loopback. Checks on the frame itself need OpenCV; without it they are skipped,
not failed.

## Dashboard

`http://127.0.0.1:8099`: video with overlays, camera control, drinking counter.
The **"Return camera to bowls"** button interrupts the current search or
following and starts the return to the home position using odometry and
visible anchors. Under the frame there are four tabs: the camera activity feed,
a live stream of `detect.log`, the model card with fine-tuning and cleanup
buttons, and the label review queue. The "Capture frame for fine-tuning" button
freezes the detector's frame so you can draw a box around the dog when the model
missed it; the labelled frame goes straight into `dataset/`.

In the "Dataset" tab a frame opens in place of the video: boxes are edited with
the mouse, "Clear all boxes" wipes the labels entirely, and "Delete frame"
removes both the image and its labels from the dataset. The latter cannot be
undone, so it asks for confirmation with a second click; the frame is also
struck from the frozen validation split, otherwise its share would be computed
over names that no longer exist.

For longer sessions there is a separate **labelling page** at `/label` (the
"labelling" link in the header): the frame at full width with the frame list
beside it, capture from the camera, keyboard shortcuts and undo.

Until a camera address is set, `/` opens the **setup page**; afterwards you can
reach it through the "setup" link in the header.

State-changing requests require the `X-Pawsight` header and check `Origin`,
`Host` and `Sec-Fetch-Site`: loopback is reachable from any page open in the
browser, and the buttons now delete files and start hour-long processes. Live
video goes over a WebSocket, to which the same-origin policy does not apply at
all, so `Origin` is checked there.

## Zones and the pan/tilt camera

Zones with fixed pixels lie as soon as the camera moves. So they are tied to
**anchors** that YOLO finds in the frame and move together with the camera. No
anchor in the frame means no zone; that is more honest than applying stale
coordinates. A side benefit: `zones.yaml` survives a move to another room
without edits.

While the camera is moving, no events are emitted: optical flow from a pan is
indistinguishable from the dog running. Camera motion is detected separately
(`pawsight/motion.py`), and after it stops there are 2 s to settle.

The tracker is **BoT-SORT**, not ByteTrack, precisely because of panning:
BoT-SORT has `gmc_method: sparseOptFlow` (camera motion compensation), while
ByteTrack has no gmc field at all.

**A zone is a patch of floor, not a box around a bowl.** Every anchored zone in
`zones.yaml` has a `shape`: a polygon whose vertices are offsets, in detector
frame pixels, from the point where the bowl touches the floor. Once the bowl is
found, the polygon is built from it and moves with it as the camera pans. The
shapes in the shipped `zones.yaml` were drawn on a frame from one particular
camera (water: a strip along the edge of the rug to the left bowl; food: a
similar parallel strip to the right bowl), so they describe that room: in
another room they are redrawn, not tweaked. The old `expand` key (a rectangle
around the box) remains as a fallback for a zone without a `shape`.

**Your own zones** ("by the door", "rug", "kitchen") are drawn in the browser at
`http://127.0.0.1:8099/zones`, where each zone also gets its own Telegram
switch: see [docs/getting-started.md](docs/getting-started.md#4-draw-your-zones).

**What you need to check by hand:** `zones.yaml` says the water bowl is the
**left** one. That describes the example setup. If yours is the other way round,
press the button on the setup page: it swaps `select` between the `water` and
`food` zones and `shape` along with it, because the shape belongs to the bowl's
position, not its contents. When editing by hand, swap both lines.

## Drinking: what exactly is counted

What is counted is **visits to a bowl and time spent at it**. The camera does
not measure the volume drunk; there are deliberately no millilitres here.

The bowls stand ~11 px apart, and the dog is noticeably wider than that gap, so
the floor contact point cannot tell which bowl the head is over. The VLM tells
drinking from eating using the visit's frames; the geometric hint is recorded
alongside, and a disagreement is stored in the DB in the `conflict` field rather
than hidden.

## PTZ (Xiaomi only)

Panning does **not** use the same protocol as video:

| | video | pan/tilt |
|---|---|---|
| protocol | P2P "miss"/cs2, UDP 32108 | legacy miIO, UDP 54321 |
| who does it | go2rtc | `pawsight/ptz.py` |
| which token | **account** token | **device** token (32 hex) |

PTZ is absent from this model's MIoT spec (9 services, not a single pan/tilt),
so the legacy `set_motor` command is used with `{"operation": N}`, where
1=Left, 2=Right, 3=Up, 4=Down.

The camera has **built-in motion auto-tracking**, which is most likely what
keeps turning it. For stable zones you usually need to turn it off:

```bash
.venv/bin/python -m pawsight ptz track-off
```

## Security

- `.env` and `go2rtc/go2rtc.yaml` are in `.gitignore` and are never printed to logs
- all services listen on loopback only; nothing is exposed to the outside
- the bot **polls** Telegram over an outgoing connection; a webhook would need
  an open port, so there is none
- bot commands are only executed for chats in `TELEGRAM_CHAT_ID`
- the setup page and live video only accept requests from their own tab
  (`Host`, `Origin`, `Sec-Fetch-Site` and the `X-Pawsight` header are checked):
  a local port is reachable by any site open in your browser
- secrets travel one way: an entered camera password or bot token is never
  shown back, neither on the page nor in the API
- the camera is not reflashed
- video never leaves the LAN

## Documentation

- [docs/getting-started.md](docs/getting-started.md): step by step with screenshots: camera, Telegram, zones
- [AGENTS.md](AGENTS.md): instructions for an AI agent with full access
- [docs/FINETUNE.md](docs/FINETUNE.md): fine-tuning for your own dog
- [docs/architecture.md](docs/architecture.md): engineering decisions and measurements
- [DOCUMENTATION.md](DOCUMENTATION.md): detailed system description

## Roadmap

Ideas, not promises; issues and pull requests are welcome:

- several cameras in one dashboard
- daily and weekly drinking trends with an alert when the dog drinks unusually much or little
- a container image for the detector on Linux with an NVIDIA GPU
- more PTZ cameras besides Xiaomi (ONVIF PTZ)

## Licence

[GNU AGPL-3.0](LICENSE). pawsight builds on
[Ultralytics YOLO](https://github.com/ultralytics/ultralytics), which is
AGPL-3.0 as well. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Support the project

If pawsight is useful to you, you can support its development:

- [Buy Me a Coffee](https://buymeacoffee.com/belid0r)
- [CloudTips](https://pay.cloudtips.ru/p/1652c09a) (bank cards, including Russian ones)
