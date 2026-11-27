# Guided install: a prompt for your AI agent

This file is a prompt. Give it to an AI agent that has a terminal on the
machine pawsight will run on (Claude Code, Codex, Cursor and the like), and it
installs pawsight and walks you through the whole setup: camera, water bowl,
Telegram, zones, autostart, and a final check that everything really works.

- **Claude Code inside a clone:** run `/install-pawsight`.
- **Any agent, nothing cloned yet:** paste this into it:

  ```text
  Install and set up pawsight on this computer for me. Clone
  https://github.com/Belidor/pawsight into ~/pawsight (or update it if it is
  already there), then open docs/agent-install.md in the clone and follow it
  step by step. Talk to me in my language.
  ```

Everything below the line is addressed to the agent.

---

## You are installing pawsight for a human

pawsight is local dog monitoring: camera → go2rtc → YOLO → zones at the bowls →
local VLM (Ollama) → SQLite, dashboard and Telegram. Everything runs on this
machine and listens on `127.0.0.1` only.

Your job: take the human from nothing to a working system they have seen with
their own eyes, doing every step you are allowed to do yourself and handing
them exactly the steps you are not allowed to do. Speak the human's language.
Keep messages short; one question or one action for them at a time, except in
step 1, where you ask everything up front.

### Ground rules (read before you run anything)

[AGENTS.md](../AGENTS.md) is the source of truth for this repository. Read it in
full right after cloning (step 2), before you run `install.sh`. Its hard rules
override anything here. The ones that matter most during setup:

1. **You never ask for, type or print a secret.** Not the camera password (it
   sits inside the RTSP URL), not the Telegram bot token, not the Mi account
   password. The human enters them on the setup page
   `http://127.0.0.1:8099/setup`, or runs the command with the secret in their
   own terminal. If the human pastes a secret into the chat anyway, do not
   repeat it, do not write it anywhere yourself, and point them to the setup page.
2. **Never print `.env` or `go2rtc/go2rtc.yaml`**, never `cat` them, never commit
   them. To learn the state of the setup use the secret-free endpoint
   `curl -s http://127.0.0.1:8099/api/setup/state` (flags and masked values only).
3. **Nothing listens on `0.0.0.0`.** If something does not connect, that is not
   a reason to change an address.
4. **You do not invent zones or decide which bowl is water.** The human confirms
   the water bowl and draws zones on a live frame.
5. **No network scans, no guessing camera passwords or paths by brute force.**
6. **System prompts are the human's.** Homebrew and `sudo` ask for the OS
   password themselves; the human types it. So is the macOS camera permission
   dialog: never touch `tccutil` or the TCC database.

Long steps are not hangs: `torch` takes several minutes, the VLM is several
gigabytes. Run them with a generous timeout (or in the background and poll),
tell the human what is running and roughly how long it takes, and never kill
them halfway. `install.sh` is idempotent: if something fails, fix the cause and
run it again.

---

## Step 0. Check the machine

```bash
uname -s -m     # Darwin arm64, or Linux x86_64 / aarch64
df -h ~         # ~15 GB free: Python deps ~5 GB, VLM ~6 GB, room for recordings
```

- macOS on Intel, Windows, or another architecture: stop and tell the human
  pawsight does not run there yet (Windows: WSL2 is not supported either, it
  cannot reach a webcam or Metal).
- Linux that is not Debian/Ubuntu: `install.sh` prints which packages to install
  by hand; relay that list.
- Less than ~15 GB free: say so before starting, not after a failed download.

## Step 1. Ask everything up front, in one message

Ask these together, with a short line on each so the human can answer in one go:

1. **Camera.** Which one will watch the bowls?
   - an IP camera with RTSP (which brand? Tapo, Reolink, Hikvision, Dahua,
     EZVIZ, Imou and most others work);
   - a Xiaomi pan/tilt camera (connects over P2P, needs a Mi account login);
   - the computer's own camera (MacBook) or a USB webcam;
   - not sure yet / later.
2. **Telegram notifications?** Yes needs a bot they create in a minute with
   @BotFather. No means events only go to the dashboard and the database.
3. **Start automatically** after a reboot or login? (recommended for an always-on
   machine)
4. **Where to install.** Default `~/pawsight`.

Do not ask for the camera password or the RTSP URL. If the human volunteers the
camera's IP address and brand, that is fine and helps in step 4.

## Step 2. Get the code and read the rules

If you are not already inside a pawsight clone:

```bash
git clone https://github.com/Belidor/pawsight.git ~/pawsight
cd ~/pawsight
```

If the folder already exists and is a clone: `git -C ~/pawsight pull --ff-only`.
If it exists and has a `.env`, this is an existing install: do not delete
anything, `install.sh` will only finish what is missing.

Now read `AGENTS.md` in full. Then skim `docs/getting-started.md`: it is what the
human will see on the setup page, with screenshots, and you will point them to
its sections.

## Step 3. Run the installer

Choose the flags from the camera answer:

| Camera | Command |
|---|---|
| IP camera (RTSP), or not sure yet | `./install.sh` (the address is entered on the setup page in step 4) |
| Xiaomi pan/tilt | `./install.sh --xiaomi` |
| MacBook / USB webcam | `./install.sh --webcam` |

Do not use `--rtsp` yourself: the URL contains the camera password. If the
human prefers the command line, they can run
`./install.sh --rtsp 'rtsp://LOGIN:PASSWORD@ADDRESS:554/PATH'` in their own
terminal instead.

Before you start, tell the human: "Homebrew (macOS) or sudo (Linux) may ask for
your computer password in the terminal; that prompt is from the system and only
you type into it." If you cannot pass a password prompt through to them, ask
them to run `./install.sh …` themselves in a terminal opened in the folder, and
wait for them to say it finished.

Watch the output. Lines starting with `!` are warnings; read each one and act
on it (for example, a system mosquitto holding port 1883). The script ends with
a self-test and a "What is left to do by hand" list; you will go through that
list in the steps below, so do not just paste it.

## Step 4. Start it and connect the camera

```bash
./start.sh
```

Then check the state (secret-free):

```bash
curl -s http://127.0.0.1:8099/api/setup/state
```

`camera.configured` tells you whether there is a stream; `go2rtc.ok` and
`detector.ok` whether the pipeline is alive.

**IP camera.** Ask the human to open `http://127.0.0.1:8099/setup` on this
machine, step 1, pick their brand from the list, type the address, login and
password themselves and press the check button. The page probes the camera
before saving and explains a wrong path or password. You can help with the
path: the presets per brand are in `go2rtc/go2rtc.example.yaml`; also remind
them that many cameras need RTSP switched on in the vendor app first (Tapo:
a separate "camera account"; Reolink: RTSP in network settings).

**Webcam.** On macOS the first frame triggers the system camera prompt for the
terminal that ran `./start.sh`: tell the human to click Allow. Ask them which
camera sees the bowls; the setup page's Webcam tab lists the cameras with a
test frame (or `curl -s http://127.0.0.1:8099/api/setup/webcams`). To switch:
`./install.sh --webcam 1` (macOS: index or name; Linux: `/dev/videoN`).

**Xiaomi.** Follow AGENTS.md section 4, item 2: the human logs in to the Mi
account in the go2rtc WebUI; you then run
`.venv/bin/python scripts/get_device_token.py` for pan/tilt.

Poll `/api/setup/state` until `camera.configured` is true, then:

```bash
./stop.sh && ./start.sh
.venv/bin/python -m pawsight selftest
```

You need `RTSP …: frame received`. If not, go through AGENTS.md section 8 in
its order (go2rtc stream → detector → brain) before moving on; do not move on
with a dead camera.

Also tell the human now: the camera must see **both bowls**. Bowls are the
anchors for every zone; without them, visits are not counted.

## Step 5. The water bowl

Ask the human to open the setup page, step 2: it shows the live picture with
the bowls outlined, and they press "Water bowl is the left one" or "the right
one". The buttons only unlock when both bowls are in the frame; if they stay
grey, the camera needs to be pointed so both bowls are visible.

Check it was saved:

```bash
curl -s http://127.0.0.1:8099/api/setup/state   # bowls.confirmed has a date
```

If the human prefers to skip it for now, that is allowed (visits are still
counted and the VLM still tells drinking from eating), but say what is lost:
until it is confirmed, "left = water" is only the author's default.

## Step 6. Telegram (if they said yes)

Give the human these steps, in their language:

1. In Telegram, message **@BotFather**, send `/newbot`, follow the prompts. It
   gives a bot token.
2. Message **@userinfobot** to get your numeric ID (chat_id).
3. Send `/start` to your new bot: Telegram does not let a bot write first.
4. On the setup page, step 3, paste the token and the ID and press
   **Test and save**. You should get a test message.

Several people: comma-separated IDs, and each of them must send `/start` to the
bot. That list is also who may use bot commands, so you never add an ID on the
human's behalf.

Check (no values printed):

```bash
curl -s http://127.0.0.1:8099/api/setup/state   # telegram.telegram_token true, telegram_chats >= 1
./stop.sh && ./start.sh
grep "bot accepts commands" storage/brain.log
```

Then ask the human to send `/snap` to the bot: a frame from the camera should
come back within a few seconds. That is the end-to-end proof for Telegram.

## Step 7. Zones (optional, the human draws them)

The three built-in zones (at the bowls, water, food) already work. If the human
wants more (a sofa, a bed, the door), send them to
`http://127.0.0.1:8099/zones` and point to `docs/getting-started.md`, section
"Draw your zones": they click the corners where the dog's paws stand, name the
zone, choose whether it notifies in Telegram, and save. No restart needed.

You do not draw, propose coordinates or edit `zones.yaml` geometry yourself
(AGENTS.md section 6).

## Step 8. Autostart (if they said yes)

`start.sh` starts everything in the background and exits; `stop.sh` stops it.
Autostart just runs `start.sh` at login or boot. Use the absolute path of the
clone (below: `PAWSIGHT_DIR`). Show the human the file before you install it.

**macOS: a LaunchAgent** (`~/Library/LaunchAgents/pawsight.start.plist`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>pawsight.start</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>PAWSIGHT_DIR/start.sh</string></array>
  <key>WorkingDirectory</key><string>PAWSIGHT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- start.sh leaves go2rtc, the broker, the brain and the detector running -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>PAWSIGHT_DIR/storage/autostart.log</string>
  <key>StandardErrorPath</key><string>PAWSIGHT_DIR/storage/autostart.log</string>
</dict>
</plist>
```

```bash
plutil -lint ~/Library/LaunchAgents/pawsight.start.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/pawsight.start.plist
```

Ollama installed through Homebrew is already a login service
(`brew services list`); if it was installed as an app, the human adds
Ollama to Login Items. Two macOS caveats to say out loud:

- **Sleep.** A sleeping Mac sees nothing. Suggest System Settings → Battery (or
  Energy) → "Prevent automatic sleeping when the display is off" on power, and
  a MacBook must stay open or be used with an external display. This is the
  human's setting to change, not yours.
- **Webcam.** Camera permission was granted to the terminal, not to launchd.
  With a MacBook or USB camera, check the camera after the reboot test below;
  if `selftest` shows `RTSP: UNAVAILABLE` and `go2rtc/go2rtc.log` mentions
  authorization, replace the LaunchAgent with a Login Item that opens a
  `start.command` file (`#!/bin/bash` + `cd PAWSIGHT_DIR && ./start.sh`,
  `chmod +x`) in Terminal, which already has the permission.

**Linux: a systemd user service** (`~/.config/systemd/user/pawsight.service`):

```ini
[Unit]
Description=pawsight dog monitoring

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=PAWSIGHT_DIR
ExecStart=PAWSIGHT_DIR/start.sh
ExecStop=PAWSIGHT_DIR/stop.sh
TimeoutStartSec=300

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable pawsight.service
loginctl enable-linger "$USER"     # start at boot, without logging in (may ask for sudo)
```

Ollama's installer already set up a system service (`systemctl is-enabled ollama`).

**Test it for real:** ask the human whether a reboot is fine now. After it,
without running anything by hand, `selftest` must show a frame from the camera
and the dashboard must open. An autostart that was never rebooted is not
verified; say so if they postpone the reboot.

## Step 9. Final check and report

Run, and read every line:

```bash
.venv/bin/python -m pawsight selftest
curl -s http://127.0.0.1:8099/api/setup/state
tail -n 30 storage/detect.log storage/brain.log
```

`Telegram: NOT configured` is fine if they said no to Telegram; anything else
marked UNAVAILABLE, NO or "blockers found" is a problem to fix or to report.

Ask the human to open the dashboard `http://127.0.0.1:8099` and confirm they
see the live picture with the dog and bowls outlined. If the dog is not around,
the bowls outlined is enough for now.

Then report, briefly (AGENTS.md section 9):

- what works, quoting the `selftest` lines;
- what does not, and why;
- what is still on the human: skipped steps (water bowl, Telegram, zones,
  the reboot test);
- which files you created or changed outside the installer (the autostart file);
- where to go next: the dashboard, `/snap` in Telegram,
  `.venv/bin/python -m pawsight today`, and `docs/FINETUNE.md` once a few
  days of frames have accumulated.

Never put a token, password or RTSP URL in the report.
