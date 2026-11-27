# pawsight desktop app

One installer per platform instead of `git clone && ./install.sh`:

| Platform | Installer | GPU for the detector |
|---|---|---|
| macOS 13+ on Apple Silicon | `.dmg` | Metal |
| Windows 10/11 x64 | `.exe` (NSIS, per-user, no admin) | NVIDIA CUDA, otherwise CPU |
| Linux x64 / arm64 | `.deb`, `.AppImage` | NVIDIA CUDA, otherwise CPU |

Intel Macs are not supported, for the same reason as the CLI install (no Metal,
no torch wheels worth having).

## How it works

The app is a thin [Tauri 2](https://tauri.app) shell (Rust, the system webview).
The program itself is the same Python code as the CLI install:

```
pawsight.app / pawsight.exe
 ├─ bundled: uv, go2rtc, the pawsight/ code and templates  (~60 MB)
 └─ on the first start, into the per-user data folder:
      .venv           Python 3.12 + torch, the right build for this machine (uv --torch-backend auto)
      .env, zones.yaml, go2rtc/go2rtc.yaml   created by `python -m pawsight appinit`
      storage/        logs, database, snapshots (the same layout as a git checkout)
```

Then it runs `python -m pawsight run --watch-stdin`
([pawsight/runner.py](../pawsight/runner.py)), the cross-platform replacement
for `start.sh`: go2rtc, an embedded MQTT broker ([pawsight/broker.py](../pawsight/broker.py),
pure Python, so no mosquitto on Windows), the brain and the detector, restarted
if they crash. The window shows the usual dashboard at `http://127.0.0.1:8099`;
on a new install that is the setup page, where the human enters the camera,
Telegram and confirms the water bowl. Nothing listens on anything but 127.0.0.1.

Closing the window keeps the camera watching (the app stays in the tray / menu
bar). Quitting from the tray stops everything. If the app is killed, the
supervisor notices its stdin closing and stops everything it started.

Data folder:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/cc.belidor.pawsight` |
| Windows | `%APPDATA%\cc.belidor.pawsight` |
| Linux | `~/.local/share/cc.belidor.pawsight` |

`storage/app.log` there is what the app itself did (install steps), `storage/runner.log`
the supervisor; the rest of the logs are the same as in the CLI install.

**Frame descriptions (VLM)** need [Ollama](https://ollama.com/download), which
the app does not bundle (it is a separate signed app with its own updates, and
the model is ~6 GB). The status page offers to install it and to pull the model
through Ollama's API, or to continue without descriptions.

## Building

Needs Rust, Node 18+ and Python 3. On Linux also the WebKitGTK packages listed
in [.github/workflows/desktop.yml](../.github/workflows/desktop.yml).

```bash
python desktop/fetch_sidecars.py        # uv + go2rtc for this machine into src-tauri/binaries/
cd desktop
npm ci
npx tauri dev                           # or: npx tauri build
```

CI builds all four installers on every change to `desktop/` or `pawsight/`
(workflow "desktop app", artifacts on the run page).

## Not done yet

- **Signing.** The installers are unsigned: macOS asks to confirm the first open
  (right click → Open, or System Settings → Privacy & Security → Open Anyway), and
  Windows SmartScreen shows "More info → Run anyway". macOS signing and
  notarization need an Apple Developer account ($99/year); for Windows,
  open-source projects can get free signing from SignPath Foundation.
- **Auto-update** (tauri-plugin-updater; needs signed releases first).
- **ffmpeg for webcams** is not bundled: the webcam mode needs `ffmpeg` on the
  system (`brew install ffmpeg`, `apt install ffmpeg`, or `ffmpeg.exe` on PATH on
  Windows). IP cameras over RTSP do not need it.
- **Xiaomi cs2 patch**: the app ships the release go2rtc. A patched build in
  `bin/go2rtc` inside the data folder takes precedence, as in the CLI install.
