# pawsight: user guide

The system watches a dog through a home camera: it finds the dog in the frame,
works out which zone it is in, counts visits to the bowl and describes in words
what the dog is doing. Everything is computed on your computer. Video never
leaves the local network.

---

## 1. What the system can and cannot do

**It can:**
- show live video from a camera that has no stock RTSP;
- find the dog and track it across frames;
- tell whether it is standing, lying, walking or running;
- count **visits to the water bowl and time spent at it**;
- tell drinking from eating using the frames;
- write the history to a database and send notifications to Telegram;
- send a frame to Telegram on the `/snap` command: what the camera sees right
  now;
- pan the camera;
- collect frames so it can be fine-tuned for your dog once a week.

**It cannot, and will not:**
- **measure the volume of water drunk.** The camera sees that the dog came up
  and stood at the bowl, but how many millilitres it drank cannot be determined
  from video. Visits and time are counted, not volume;
- tell drinking from eating by geometry. The bowls stand right next to each
  other (11 pixels apart) and the dog is wider than that gap, so the position of
  the paws cannot tell which bowl the head is over. The language model decides
  that from the frames, and it is sometimes wrong. The system shows
  disagreements rather than hiding them.

---

## 2. How it works

```
camera ──P2P──> go2rtc ──RTSP──> detector ──MQTT──> brain ──> database + Telegram
        (LAN)   (host)          (host, Metal)      (container)
                                                        │
                                   Ollama (host, Metal) ┘
```

Three things run **natively on the machine**, not in a container, and that is
not a whim:

| What | Why native |
|---|---|
| go2rtc | reaches the camera over P2P on the local network; from a container the network goes through NAT |
| detector | needs the GPU (Metal on a Mac, CUDA on Linux). A container on a Mac has none: 10–17 ms on Metal versus 90–145 ms on CPU |
| Ollama | same thing: a 6 GB model in a container would run on CPU |

Everything else (zones, heuristics, counting visits, the database, Telegram)
is the "brain". By default it runs natively too, from `.venv`; on a Mac it can
optionally run in Docker together with the broker (`PAWSIGHT_STACK=docker` in
`.env`).

---

## 3. Everyday use

### Initial setup (once)

A step-by-step version with screenshots is in
[docs/getting-started.md](docs/getting-started.md).

It opens by itself: while no camera address is set, `http://127.0.0.1:8099`
shows the setup page instead of the dashboard. The direct address is
`http://127.0.0.1:8099/setup`.

What it does:

| Step | What you enter | Where it goes |
|---|---|---|
| 1. Camera | address, port, path, login and password, or a complete RTSP URL; or, on the Webcam tab, the MacBook / USB camera and its size | `go2rtc/go2rtc.yaml` (mode 600, the file is in `.gitignore`) |
| 2. Bowl | which bowl is the water bowl: the left or the right one **in the frame** | `zones.yaml` |
| 3. Telegram | bot token and the recipients' chat_id | `.env` (mode 600) |

The camera address is checked before it is saved: the page itself connects to
the camera over RTSP and reports the cause, not just the fact: "port closed",
"path not found (404)", "the camera rejected the login or password", "this port
is not RTSP". You can save an unchecked address, but only explicitly, with a
checkbox.

A webcam (the MacBook's own camera or a USB one) is checked differently: there
is nothing to dial, so the page asks go2rtc for one real frame through ffmpeg
and shows it. When that fails, the page quotes ffmpeg's own reason from the
go2rtc log (no camera access on macOS, a size the camera does not have, the
device is busy). go2rtc needs `ffmpeg` for this: `./install.sh --webcam`
installs it.

What you enter is never shown back: the camera password and the bot token go
into files and never appear in the page's responses again. If you made a
mistake, enter them again.

The page does not ask for the Mi account password: for Xiaomi cameras the
login happens in go2rtc's own WebUI (`http://127.0.0.1:1984` → Add → Xiaomi),
and the page only shows whether the account has appeared and can fetch the
device token for panning (that script does not ask for a password either).

Values from `.env` are read once at process start, so after saving you need
`./stop.sh && ./start.sh`. The bowl choice is read by the **detector**, which
also needs a restart.

If the brain did not come up (for example, mosquitto is not running), the page
can be started on its own, without the broker:

```bash
.venv/bin/python -m pawsight setup
```

### Start

```bash
cd ~/pawsight
./start.sh
```

The script is idempotent: if something is already running, it will not start it
twice, and at the end it runs the self-test by itself.

Stop everything: `./stop.sh`

### Check that everything is alive

```bash
./.venv/bin/python -m pawsight selftest
```

It shows, item by item: whether Metal is available, whether the camera stream
is flowing, whether the language model answers, how many zones are loaded, and
whether Telegram is configured.

### Stop

```bash
pkill -f "pawsight detect"
pkill -f go2rtc
docker compose down
```

### Watch the video

```bash
open "http://127.0.0.1:1984/stream.html?src=dog_cam"
```

> **go2rtc here is patched.** In the original, after every camera pan the
> video froze for 6–19 seconds: the 100-chunk P2P receive buffer could not hold
> a 2K keyframe, and go2rtc reconnected to the camera. The patch is in
> `patches/`, the build is `scripts/build_go2rtc.sh`, the original is
> `bin/go2rtc.release`. Rollback: `cp bin/go2rtc.release bin/go2rtc`.
> Do not update go2rtc "just because": the gaps will come back.

If WebRTC acts up, switch the mode to **MSE** right on the page.

### What is on the dashboard under the video

Four tabs:

* **Camera feed**: a human-readable feed of pans, search, following, tilt
  hold, stream gaps, saves to the dataset, errors. The kind labels work as a
  filter: a click hides a kind, ⌘-click leaves only that kind.
* **Logs**: the same `storage/detect.log`, but live and in the browser: a level
  filter, search with highlighting, a "Pause" button (so you can select and
  copy a line). Newest at the bottom, like `tail -f`.
* **Model**: which weights are actually loaded, how many frames have
  accumulated, how the last fine-tuning ended, and two buttons: start
  fine-tuning and cleanup.
* **Dataset**: the label review queue: what auto-collection has gathered and
  where it most likely got things wrong. See section 6.

Under the frame there is the **"Capture frame for fine-tuning"** button. See
section 6.

---

## 4. How much the dog drank

```bash
./.venv/bin/python -m pawsight drinks       # today
./.venv/bin/python -m pawsight drinks 7     # this week
```

```
=== Drinking today ===
  visits: 4   total at the bowl: 2 min 18 s
  (amount is not measured; visits and time are counted)

    08:14  35 s
    11:02  48 s
    15:30  22 s
    19:47  33 s
```

The `⚠️ disagrees with geometry` mark means the language model said "drinking"
while the dog's position pointed at the food bowl (or the other way round). The
event is recorded anyway; the call is yours.

The full picture of the day, including movement:

```bash
./.venv/bin/python -m pawsight today
```

---

## 5. Camera control

```bash
./.venv/bin/python -m pawsight ptz left     # left | right | up | down
./.venv/bin/python -m pawsight ptz status   # power, auto-tracking, night mode
```

One call means one pan step.

### Following the dog and camera height

The camera's built-in tracking is **turned off on purpose**: it tilts the
camera and follows any motion, including people. Instead, the system follows
the dog itself: **pan only, no tilt, the dog only**.

The "Pan the camera to follow the dog" switch on the dashboard turns this
following on and off. When the dog's centre moves more than 10% of the frame
width away from the middle, the camera takes a step towards it; no more often
than once every 3.5 seconds and no more than 20 steps a minute. The camera does
not chase people; they are only outlined with a dashed line on the video.

The dog's position is updated both while the camera moves and during the motor
pause. When the dog disappears from the frame, the decision is made once and
logged in full (`follow: dog vanished (...) -> ...`): at the edge (centre more than
30% of the width away, or the box in the outermost 5%) or while heading for the
edge, up to three steps that way within 14.5 seconds; lost in the middle of the
frame, as many steps as following would have made with the dog visible (no
more than two), after which the patrol waits a minute, because the dog is most
likely still there and the detector simply does not see it; lost near the
centre, the camera stays put. If the video stream drops, the camera waits for
fresh frames. After the camera answers "edge of travel", that direction is
blocked for a minute; a confirmed movement the other way lifts the block
earlier.

The **"Return camera to bowls"** button hands the motor to the return loop
immediately: the current search sector and catching up with the dog are
cancelled. The camera heads for the home position using the accumulated offset,
refining it by the bowls and the learned refrigerator. The button does not
change the following setting; after the return, a new patrol starts no earlier
than a full `PAWSIGHT_SEARCH_IDLE_SEC`.

**Height is held by landmarks.** The camera has no absolute position, only
relative steps (measured: one tilt step = 157 px in the frame). So when taking a
reference, the system remembers at what height in the frame static furniture
sits (TV, refrigerator, oven, sofa, bed, table, the pair of bowls) and later
compares the same objects. If they have moved vertically by more than half a
step, the camera moves back by a whole number of steps, only while it is
standing still and only after a series of consistent measurements. If it has
turned towards another room where none of the reference landmarks is visible,
it does not move at all. After a step the system checks that it got closer; if
it got further, it freezes.

**No dog: the camera searches for it.** After `PAWSIGHT_SEARCH_IDLE_SEC` without
a dog, the patrol sweeps a sector to the right with stops, returns to the bowls,
then sweeps a sector to the left the same way and returns again. If the dog
appears, the search is abandoned at the current step and following kicks in;
the next cycle starts only after a new full timeout.

When following or the search pans the camera, the height reference is
re-captured at the new angle (panning does not change height).

**Edge of travel.** The camera reports it itself with an `overflow` answer, to
following, to the patrol and to a manual pan from the dashboard (the toast
"camera is at the right edge of travel"). This limit is a firmware counter, and
relative to the room it drifts: at the same stop the bowls have been both in
the centre of the frame and 30% to the right. So if the patrol hits the edge
while the bowls are in the frame, it does not declare odometry lost but writes
"home at the edge of travel: bowls at +0.30, the firmware won't go closer;
retry in N s" and tries again with a growing pause (30 s → 5 min), or right
away once the camera has moved. The "Return camera to bowls" button always
tries. If the "right" button answers "edge of travel" while there is clearly
room on the right, rebooting the camera helps. The return home is fired in a
burst while the bowls are not visible, and once they are in the frame, one
pulse at a time with a pause for a frame, so as not to overshoot.

The holding precision is **±78 px** (half a step): the motor step does not
allow better.

The ↑/↓ buttons on the dashboard are locked behind the "allow tilt" checkbox.
If you did tilt the camera by hand, that becomes the new "as it is now": the
reference is re-captured automatically once the camera stops.

```bash
./.venv/bin/python -m pawsight tilt status      # offset from the reference now
./.venv/bin/python -m pawsight tilt recapture   # current height = new reference
./.venv/bin/python -m pawsight tilt restore     # restore the height manually
```

> go2rtc's `frame.jpeg` and the first frame of a new RTSP connection can be
> ~2 seconds older than reality (they start from a keyframe). For measurements
> the system reads the live stream, and the `tilt status` command skips the
> first 2.5 seconds.

---

## 6. Fine-tuning for your dog

The system sets aside the frames it is unsure about by itself. It does not save
confident detections: there is nothing to learn from them.

### When you see the dog but the model does not

This is exactly the case that cannot be collected automatically: a dog in a cone
or in someone's arms gives a confidence of 0.1–0.3, while selection into the
dataset starts with "was a dog found at all". That is why there is a button.

Press **"Capture frame for fine-tuning"** under the video. The frame freezes,
and that matters: the video in the player lags by about a second, and you need
to label exactly the frame that will go to disk. The `←` `→` arrows step back a
frame or two if the moment you wanted has already passed.

What the model found is already drawn on the frame. Draw a box around the dog
with the mouse, fix boxes that slipped by dragging a corner, delete the extra
ones. The class of a new box is set with the switch or the `1` `2` `3` keys.
Then "Save to dataset".

> **Label EVERYTHING you see, not just the dog.** The counter under the frame
> shows which classes are missing from the labels. If there is a bowl in the
> frame but no box on it, the model will learn that a bowl is background; all
> the zones depend on the `bowl` class, and they will break silently. The
> system refuses to save a frame where it sees a bowl itself but the labels have
> none, until you confirm with a second click that there really are no bowls.

A frame with empty labels is a useful example too: it tells the model "there is
nothing here". It is a legitimate way to fix false positives.

Such frames go into the dataset named `manual_*` and into the registry
`dataset/human_labeled.json`, so that automatic pre-labelling leaves them
alone.

### Reviewing what has accumulated: the "Dataset" tab

Pre-labelling is done by the model itself, so a frame arrives in the dataset
together with its mistakes, and those go into training as ground truth. The
**"Dataset"** tab sorts frames so that likely mistakes come first: empty labels,
"only the dog labelled", no bowls, low confidence, a box covering half the frame
or, conversely, a tiny one.

Clicking a row opens the frame right in place of the video. Then:

* **fix a box**: drag a corner; select it with a click first, otherwise the
  "Delete box" button stays grey;
* **"Hint"**: re-run the frame at a 0.05 threshold and show with a dashed line
  what the model almost saw (usually the bowls);
* **"Clear all boxes"**: remove the labels entirely. A frame with no boxes is a
  legitimate negative example; that is how a "dog" on the base of an armchair
  gets cured. But if there is a bowl or a person in the frame, outline them: an
  unlabelled class is learned as background;
* **"Save"**: write the labels and move on to the next frame. The frame is
  marked "human-edited" and goes to the bottom of the queue;
* **"Delete frame"**: throw both the image and its labels out of the dataset.

Deletion cannot be undone, so it asks for a second click: the first shows what
exactly will disappear. The frame is also struck from
`dataset/val_manifest.json`; otherwise the validation share would be computed
over names that no longer exist, and the acceptance gate would silently stop
checking anything. The perceptual hash of the deleted frame, on the other hand,
stays: without it auto-collection would save an almost identical frame within a
minute, and what you threw out would come back by itself.

The copies in `dataset/images/train` and `dataset/images/val` are not touched:
every training run rebuilds them, and a run in progress reads its files from
exactly there. So deleting frames during fine-tuning is fine: it does not
affect the current run.

**"Home"** returns to the live video and the camera activity feed; "Exit" (or
`Esc`) just closes the frame and leaves you in the queue.

### The labelling page: `/label`

The same work on a page of its own, `http://127.0.0.1:8099/label` (the
"labelling" link in the dashboard header, or "Open labelling page" in the
"Dataset" tab). The frame takes the full width instead of sharing the video's
box, and the frame list stays beside it, so a long session of reviews does not
mean scrolling between the queue and the frame.

* the list on the left has the same order as the "Dataset" tab ("suspicious" or
  "newest") plus a filter: not reviewed yet, no bowl box, dog only, empty;
* **"Capture from camera"** (`C`) freezes the detector's last frames, as the
  button under the video does; `←`/`→` above the frame step through them, the
  model's boxes on that frame are the starting point, and "Save" puts it into
  `dataset/`;
* boxes: drag to draw, drag a box to move it, its corners to resize; `1`…`9`
  sets the class (of the selected box too), `Tab` cycles boxes, arrows nudge the
  selected one (`Shift` for 10 px), `Del` deletes, `Ctrl+Z` undoes, `H` hides the
  boxes to look at the picture underneath;
* `Enter` saves and opens the next frame in the list. Leaving a frame with
  unsaved boxes takes the same action twice: the first one says what would be
  lost.

The rules are the same as in the dashboard, because the requests are the same:
a class that disappears from a frame needs a second "Save", and "Delete frame"
needs a second press. The class buttons come from `pawsight/labels.py`
(`DATASET_CLASSES`), so the page never offers a class the detector would
refuse to write.

### Once a week

**Step 1. See what has accumulated**

```bash
./.venv/bin/python -m pawsight dataset
```

You need at least 50 frames. That is the only requirement: the "Start
fine-tuning" button on the dashboard becomes active as soon as you have them.

**Step 2. Review the labels**

Frames are in `dataset/images/`, labels in `dataset/labels/`. The labels have
already been set automatically; your job is to fix the mistakes: remove boxes
where there is no dog, fix the ones that slipped, and throw out bad frames.

The easiest way is the **"Dataset"** tab on the dashboard (above): it puts
suspicious frames first by itself. Any YOLO label editor works too, for example
X-AnyLabeling.

> **Important.** The labels have three classes: `dog`, `bowl`, `person`.
> **Do not delete the bowl boxes.** All the zones depend on them. If you train
> the model on the dog alone, the "bowl" class disappears from the model
> entirely and the zones stop working, and silently at that: the dog will be
> found as if nothing happened, while bowl visits stop being counted.
> For the same reason, label people if they are in the frame.

**Step 3. Start training**

With the **"Start fine-tuning"** button on the "Model" tab, or from the
terminal:

```bash
./.venv/bin/python -m pawsight train
```

It takes about an hour for 80 epochs at the current dataset size; the dashboard
shows a measured estimate and the current epoch, not a round number. It runs on
the same Metal the detector uses, so recognition will be slower. If the frame
rate halves, you will see it on the "Model" tab: it means the stream is
breaking up and events are not being recorded during that time. The
confirmation dialog has a "pause detection during training" checkbox.

Training survives a detector restart: it runs as a separate process, and
`./stop.sh` asks about it separately.

> The first training on 40 frames was rejected by the gate: bowl mAP50 = 0.60.
> The reason: automatic labelling called cups and vases in living-room frames
> "bowls", and the model learned that. Before training, review the `bowl` boxes
> specifically and delete those that are not on the water or food bowl.

**Step 4. Read the verdict**

At the end the system decides by itself whether the new model is good enough:

```
=== per-class metrics ===
  dog      mAP50=0.912  recall=0.885  precision=0.901
  bowl     mAP50=0.844  recall=0.870  precision=0.902

  ✓ ACCEPTED: bowl mAP50=0.844, dog mAP50=0.912
    weights: ~/pawsight/yolo26s_finetuned.pt
```

If the bowls dropped below the threshold, the model is **not accepted**, and you
will be told so plainly. The old one keeps working.

**Step 5. Enable the new model**

Change this line in `.env` and restart the detector:

```
PAWSIGHT_MODEL=yolo26s_finetuned.pt
```

---

## 7. Telegram notifications

1. Create a bot with [@BotFather](https://t.me/BotFather) and get a token.
2. Find out your chat_id with [@userinfobot](https://t.me/userinfobot).
3. **Message your bot `/start`.** Telegram does not let a bot write to a person
   first: without this step sending fails with `chat not found`, and the logs
   only show a terse "Telegram answered 400 to recipient #N".
4. Enter both values on the setup page (`http://127.0.0.1:8099/setup`, step 3):
   it checks the token and sends each recipient a test message, naming exactly
   who did not get it and why. Or by hand in `.env`:

```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

5. Restart the brain; how exactly depends on `PAWSIGHT_STACK` in `.env`.

   With `PAWSIGHT_STACK=native` (the default) a general restart is enough:

```bash
./stop.sh && ./start.sh
```

   With `PAWSIGHT_STACK=docker` the container has to be **recreated**:

```bash
docker compose up -d --force-recreate brain
```

> Exactly `up -d --force-recreate`, **not** `restart`. Variables from
> `env_file` are baked into the container when it is created, and
> `docker compose restart` brings up the same container with the old (empty)
> values; it looks as if the token does not work.

> If you updated the **code** along with `.env`, add `--build`:
> `docker compose up -d --build brain`. The code is baked into the image
> (`Dockerfile`, `COPY pawsight/`) and is not mounted as a volume; only
> `storage` and `zones.yaml` are mounted. Without `--build` the container is
> recreated from the previous image, and you get the old behaviour with the new
> settings. To see what is really inside:
> `docker compose exec brain grep -c parse_chats /app/pawsight/notify.py`

6. Check: `.venv/bin/python -m pawsight selftest`; the Telegram line should say
   "configured, recipients: N".

### Several recipients

`TELEGRAM_CHAT_ID` takes a comma-separated list:

```
TELEGRAM_CHAT_ID=11111111,22222222
```

All recipients see the same stream of notifications. Each of them must press
Start in the bot themselves, otherwise messages will not reach that particular
person while the others still get them: a refusal for one does not cancel the
delivery to the rest.

The picture is uploaded to Telegram **only once**: it goes to the first
recipient as a file, the `file_id` is taken from the response, and that is what
gets sent to the others.

Notifications come with a picture. The system does not send more than one
message per 5 minutes per zone.

### A frame on request: "what is going on right now"

You can message the same bot and it will show you the camera without opening
the dashboard. Nothing needs to be configured separately: the commands are
enabled together with notifications, with the same token and the same list of
recipients.

| Command | What it does |
|---|---|
| `/snap` | a fresh frame from the camera |
| `/frame`, `/photo`, "snap", "frame", "photo", "snapshot" | the same |
| `/help` | short help |

What is worth knowing about this frame:

- **The bot replies only to you.** The other recipients are subscribed to the
  dog's events, and your request is your business; it does not land in their
  feed.
- **The 5-minute limit does not apply to a frame on request.** That limit is
  about events the system comes up with by itself.
- **Press it ten times and you get one frame.** A fresh one, not ten identical
  ones.
- **The resolution is the one the detector watches in**
  (`PAWSIGHT_DETECT_RTSP`). For Xiaomi cameras that is SD 640×360, and it is
  done on purpose: no second connection to the 2K stream is opened for a
  snapshot, otherwise the video starts falling apart for everyone, recognition
  included.
- **If the camera stream has frozen**, the frame comes with a note saying how
  many seconds old it is. The system will not silently pass off an old frame
  as "now".
- **If there is no frame at all**, the bot says why (the detector is not
  running, the stream did not open) rather than staying silent.

**The bot does not answer strangers.** Commands are only executed for chats in
`TELEGRAM_CHAT_ID`; everyone else gets silence, not even "access denied": such
an answer would confirm to someone who typed the bot's name by chance that the
bot is alive and there is somebody's home behind it. While `TELEGRAM_CHAT_ID`
is empty, the commands do not work for anyone at all.

Giving yourself or your household access = adding a `chat_id` to
`TELEGRAM_CHAT_ID` after a comma and restarting the brain (see step 5 above).
A new recipient still has to press Start in the bot themselves.

---

## 8. Settings

Everything is in the `.env` file.

| Parameter | Default | What it does |
|---|---|---|
| `PAWSIGHT_DETECT_FPS` | 5 | frames per second to process |
| `PAWSIGHT_CONF` | 0.35 | confidence threshold. Lower means more false positives, higher means more misses |
| `PAWSIGHT_INFER_W/H` | 1280×720 | frame size for recognition |
| `PAWSIGHT_IMGSZ` | 960 | the model's internal size. **Do not lower it**: at 640 confidence in the dog drops from 0.70 to 0.57 |
| `PAWSIGHT_RATE_LIMIT_SEC` | 300 | no more than one notification per N seconds per zone |
| `PAWSIGHT_COLLECT_DATASET` | 1 | whether to collect frames for fine-tuning |
| `PAWSIGHT_WEB_PORT` | 8099 | dashboard port |
| `PAWSIGHT_WEB_HOST` | 127.0.0.1 | listen address. **Do not change it**: the camera password is entered on the setup page, and it has no business on the local network. In the container it is overridden to 0.0.0.0, but the port is published as `127.0.0.1:8099` |
| `PAWSIGHT_FOLLOW_DOG` | 1 | follow the dog by panning |
| `PAWSIGHT_TILT_HOLD` | 1 | hold the height by the reference |
| `PAWSIGHT_TILT_ACT` | 1 | 0 means only measure the offset, do not move the camera |
| `PAWSIGHT_SEARCH_SEC` | 10 | one dog search step every N seconds; 0 means do not search |
| `PAWSIGHT_LOG_BUFFER` | 3000 | how many lines to keep in the "Logs" tab |
| `PAWSIGHT_TRAIN_FROM_DASHBOARD` | 1 | 0 removes the start fine-tuning button |
| `PAWSIGHT_CLEAN_DAYS` | 14 | what cleanup considers old |
| `PAWSIGHT_CLEAN_KEEP_RUNS` | 1 | how many training runs to keep |

In docker mode, also set `TZ` (for example `TZ=Europe/Berlin`) in the
environment you start the stack from: the brain container defaults to UTC, and
without it notification and report times are off by your UTC offset.

Zones are configured in `zones.yaml`. Each zone is a polygon on the floor
(`shape`), drawn by the owner and tied to a detected bowl: the vertices are
given as offsets from the point where the bowl touches the floor, and they move
with it when the camera pans. The only thing the setup page edits there is
which bowl is the water bowl (`select: left`/`right` on the `water` and `food`
zones, and `shape` along with it: the shape belongs to the bowl's position);
everything else is edited by hand.

---

## 9. If something goes wrong

**The setup page opened instead of the dashboard**

That is by design: the camera address is empty (`streams.dog_cam` in
`go2rtc/go2rtc.yaml`), and it is easiest to set it right there. The dashboard
has not gone anywhere: `http://127.0.0.1:8099/?dashboard=1`.

**The dashboard or the setup page answers "foreign Host"**

You opened them via something other than `127.0.0.1` or `localhost`, for
example by the machine's name or from a phone. That is not a fault: a local
port is reachable by any foreign page open in the browser, and this is where
the camera password is entered.

**The setup page says it cannot write files**

The brain runs in a container (`PAWSIGHT_STACK=docker`), while `.env` and
`go2rtc.yaml` live on the host and are not mounted into the container. Run the
wizard on the Mac itself: `.venv/bin/python -m pawsight setup`.

**The dashboard does not open**

```bash
docker compose logs brain --tail 20
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/
```

**The video is black, but the boxes are drawn**

So the dashboard is alive and go2rtc is not. The player and the stream go
through the dashboard (that way go2rtc stays closed to foreign pages), so check
go2rtc specifically:
`curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1984`

**No video**

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1984
```

Not `200` means go2rtc is not running. If it is running but there is no
picture, open `http://127.0.0.1:1984` and check that the Xiaomi account is still
there: the token expires from time to time, and then you need to log in again.

**The dog is not found**

```bash
./.venv/bin/python -m pawsight selftest
```

Or just open the **"Logs"** tab on the dashboard: it is the same
`storage/detect.log`, live, with a level filter and search.

Check the lighting and whether the view is blocked. Lower the `PAWSIGHT_CONF`
threshold only after you have made sure that is not the problem.

**Notifications do not arrive**

```bash
docker compose logs brain --tail 30
```

The line "Telegram not configured" means the token in `.env` is empty.

`Telegram answered 400 to recipient #N` means that recipient has not pressed
Start in the bot. `403` means they blocked the bot. To check that the brain sees
the token at all:

```bash
docker inspect $(docker compose ps -q brain) --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep TELEGRAM_ | sed 's/=.*/=<set>/'
```

The `sed` is not there for looks: without it the command prints the token and
chat_id **values** to the terminal, exactly at the moment when a person is stuck
and quite likely to copy the output into a chat or an issue. The values are not
needed to answer "does the brain see the token at all?".

Empty even though `.env` is filled in means the container was brought up with
`restart`. Recreate it: `docker compose up -d --force-recreate brain`.

`delivered to 0 of N recipients` means the message went to nobody, but the
5-minute rate limit window is already taken: the next event of the same type
will be skipped. Check a fix no sooner than 5 minutes later.

**The bot does not answer `/snap`**

First check whether the commands came up at all:

```bash
grep "bot accepts commands" storage/brain.log     # or docker compose logs brain
```

| What is in the log | What it means |
|---|---|
| `bot commands disabled` | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_ID` is empty |
| `message from an unknown chat ignored` | the writer is not the `chat_id` in `.env`; check it with @userinfobot |
| `Telegram answered 409 to an updates poll` | two processes poll the same token. This happens if a `pawsight live` was left running: `./stop.sh` and then `./start.sh` again |
| none of the above | polling works, the problem is the frame; look at the bot's reply, it names the cause |

`No frame: the detector did not answer` means `pawsight detect` is not running
or the broker did not come up:

```bash
pgrep -f "pawsight detect" >/dev/null && echo "detector alive" || echo "no detector"
nc -z 127.0.0.1 1883 && echo "broker listening"
tail -20 storage/detect.log
```

`No frame: no frames: the stream is silent` means the detector is
alive but the video does not reach it; it is the same problem as "the dashboard
is empty", and you debug it starting from the go2rtc stream.

**The brain does not respond**

```bash
docker compose ps
docker compose restart brain
```

**The camera does not pan**

```bash
./.venv/bin/python -m pawsight ptz status
```

If it says there is no `CAMERA_TOKEN`, the device token has expired. Refresh
it:

```bash
./.venv/bin/python scripts/get_device_token.py
```

The script does not ask for a password: it uses go2rtc's already saved login.

---

## 10. Security

- All services listen on `127.0.0.1` only. Nothing is reachable from the local
  network or from the internet.
- Secrets live in `.env` and `go2rtc/go2rtc.yaml`; both files are excluded from
  git. They are not printed to the logs: the `httpx` logger is muted, and
  everything that leaves the process for the browser goes through secret
  scrubbing.

> **If the system was running before September 2026, change the bot token.**
> The `httpx` logger printed the full URL of each Telegram request, so the
> token ended up in `docker logs` in plain text. The code put a stop to that,
> but it cannot remove what has already been written to the container's
> history. Revoke the token with [@BotFather](https://t.me/BotFather) using
> `/revoke`, put the new one into `.env` and recreate the container:
> `docker compose down && docker compose up -d`.

- The camera has not been reflashed.
- Video does not leave the home. The only outbound request is fetching
  encryption keys from Xiaomi when connecting to the camera; the video itself
  goes directly over the local network. Plus what you send yourself:
  notification frames and the `/snap` frame go to Telegram, otherwise there
  would be nowhere to send them.
- The bot **polls** Telegram itself, over an outgoing connection. A webhook
  would need a port open to the internet, so there is none here.
- Bot commands are only executed for chats in `TELEGRAM_CHAT_ID`. The bot does
  not answer strangers at all: silence does not confirm to someone who typed
  its name by chance that the bot is alive and there is somebody's home behind
  it.
- The setup page and live video only accept requests from their own tab:
  `Host`, `Origin`, `Sec-Fetch-Site` and the `X-Pawsight` header are checked.
  An entered camera password or bot token is never shown back, neither on the
  page nor in API responses.

### Passwords for go2rtc and the broker

`127.0.0.1` closes the system off from the network, but not from the browser:
any open tab can reach loopback, and through DNS rebinding a foreign site could
read go2rtc's `/api/config`, which holds the camera password and the Mi account
token. An anonymous MQTT broker, in turn, lets in any process on this machine,
which could then pan the camera, delete dataset frames and read frames from the
camera.

An installation made by `install.sh` after November 2026 gets both passwords
by itself. An older one keeps working without them (nothing breaks) and is
migrated once like this:

```bash
cd ~/pawsight
./stop.sh
.venv/bin/python -m pawsight.authsetup --go2rtc --mqtt
./start.sh
```

What the middle command does (values are not printed to the screen; running it
again changes nothing):

- in `go2rtc/go2rtc.yaml`, section `api`: `username`, a random `password` and
  `local_auth: true` (without this key go2rtc lets `127.0.0.1` in without a
  password); the same values go into `.env` as `GO2RTC_USER`/`GO2RTC_PASS`,
  which the brain in the container uses;
- `mosquitto/passwd` (the password hash) and `mosquitto/native.conf`, and in
  `.env`, `PAWSIGHT_MQTT_USER`/`PAWSIGHT_MQTT_PASS`.

After the restart:

- docker mode: `start.sh` rebuilds and recreates the containers; mosquitto in
  the container picks `mosquitto/auth.conf` by itself now that
  `mosquitto/passwd` exists. If the containers were not recreated, run
  `docker compose up -d --force-recreate`.
- native mode: `start.sh` starts its own mosquitto with
  `mosquitto/native.conf` instead of the Homebrew service. The Homebrew service
  is anonymous and holds the same port, so stop it before `./start.sh`:
  `brew services stop mosquitto`.
- go2rtc's WebUI (`http://127.0.0.1:1984`) will now ask for a login: it is in
  `go2rtc/go2rtc.yaml`, section `api`.

Check: `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:1984/api/config`
should answer `401`, the dashboard should show video, and
`docker compose logs brain` (or `storage/brain.log`) should not contain "broker
refused". Rollback: delete `mosquitto/passwd`, the `username`/`password`/
`local_auth` lines from the `api` section and the four keys from `.env`, then
`./stop.sh && ./start.sh`.
