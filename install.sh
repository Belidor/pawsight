#!/usr/bin/env bash
# Installs pawsight from scratch on a clean Mac (Apple Silicon) or Linux
# (Debian/Ubuntu; other distributions get a list of what to install).
# Idempotent: running it again breaks nothing and only finishes what is missing.
#
# What the script does NOT do and never will:
#   * it does not ask for or enter your passwords: neither Mi account nor Telegram;
#   * it exposes nothing to the outside: every service listens on 127.0.0.1 only.
# The only password prompt comes from the OS itself (Homebrew on macOS, sudo for
# apt on Linux), not from us.
#
#   ./install.sh --rtsp rtsp://user:pass@192.168.1.50:554/stream1
#   ./install.sh --xiaomi          # Xiaomi camera over P2P instead of RTSP
#
set -euo pipefail
cd "$(dirname "$0")"

RTSP_URL=""
MODE="rtsp"
SKIP_MODEL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rtsp)    RTSP_URL="${2:-}"; shift 2 ;;
    --xiaomi)  MODE="xiaomi"; shift ;;
    --no-model) SKIP_MODEL=1; shift ;;
    -h|--help)
      # only the header comment lines, up to the first executable line
      awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"
      exit 0 ;;
    *) echo "unknown argument: $1"; exit 2 ;;
  esac
done

# A new install is one that has no .env yet. Only a new install gets passwords on
# the broker and go2rtc: silently locking the broker on an existing install would
# cut off from the bus everything that already talks to it. The owner migrates an
# existing install by hand (DOCUMENTATION.md).
NEW_INSTALL=0
[ -f .env ] || NEW_INSTALL=1

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
say()  { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. hardware -------------------------------------------------------------
# On a Mac the project relies on Metal (MPS). Without it YOLO falls back to the
# CPU: measured 17.8 ms/frame vs 42.4 ms, and that is on a fast machine. On
# Linux an NVIDIA GPU (CUDA) is used when present, otherwise the CPU.
step "Checking hardware"
OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin)
    [ "$ARCH" = "arm64" ] || die "On macOS, Apple Silicon (M1+) is required. Found: $ARCH: Metal is unavailable, inference would run on the CPU 2.5x slower."
    say "macOS $(sw_vers -productVersion) on $ARCH: OK"
    PY_ARCH=arm64 ;;
  Linux)
    case "$ARCH" in
      x86_64|amd64) G2R_ASSET=go2rtc_linux_amd64; PY_ARCH=x86_64 ;;
      aarch64|arm64) G2R_ASSET=go2rtc_linux_arm64; PY_ARCH=aarch64 ;;
      *) die "unsupported CPU architecture: $ARCH (x86_64 or aarch64 needed)" ;;
    esac
    say "Linux ($(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")) on $ARCH: OK"
    if command -v nvidia-smi >/dev/null 2>&1; then
      say "NVIDIA GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
    else
      warn "no NVIDIA GPU found: the detector will run on the CPU (fine for one camera at 5 fps on a modern CPU)"
    fi ;;
  *) die "macOS or Linux is required. Found: $OS" ;;
esac

# Root (a container, a fresh server) has no sudo and needs none.
SUDO=sudo; [ "$(id -u)" = "0" ] && SUDO=""

# Portable in-place sed: BSD sed (macOS) wants an empty suffix argument, GNU sed does not.
sedi() { if [ "$OS" = "Darwin" ]; then sed -i '' "$@"; else sed -i "$@"; fi; }

# --- 1-2. system packages ----------------------------------------------------
# uv instead of pip: it fetches Python 3.12 by itself. The system python3 on
# many Macs is x86_64 under Rosetta, and torch ships no wheels for Rosetta.
if [ "$OS" = "Darwin" ]; then
  step "Homebrew"
  if command -v brew >/dev/null; then
    say "already installed: $(brew --version | head -1)"
  else
    warn "Homebrew not found. The official installer will install it now;"
    warn "it will ask for YOUR macOS password. This is the only place it is needed."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$p" ] && eval "$("$p" shellenv)" && break
    done
    command -v brew >/dev/null || die "Homebrew is installed but not on PATH. Open a new terminal and run ./install.sh again."
  fi

  step "Packages: uv, mosquitto, ollama"
  # command -v is checked FIRST: Ollama is often installed as an .app (cask) or by
  # hand; then brew list --formula does not see it and installing the formula conflicts.
  for pkg in uv mosquitto ollama; do
    if command -v "$pkg" >/dev/null 2>&1; then
      say "$pkg: already installed ($(command -v "$pkg"))"
    elif brew list --formula "$pkg" >/dev/null 2>&1; then
      say "$pkg: already installed (brew)"
    else
      say "installing $pkg..."
      brew install "$pkg" >/dev/null || die "failed to install $pkg"
    fi
  done
else
  step "Packages: mosquitto, netcat, curl, unzip, uv, ollama"
  NEED=""
  command -v mosquitto >/dev/null 2>&1 || [ -x /usr/sbin/mosquitto ] || NEED="$NEED mosquitto"
  command -v nc >/dev/null 2>&1 || NEED="$NEED netcat-openbsd"
  command -v curl >/dev/null 2>&1 || NEED="$NEED curl"
  command -v unzip >/dev/null 2>&1 || NEED="$NEED unzip"
  if [ -n "$NEED" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      warn "installing with apt:$NEED (sudo may ask for YOUR password)"
      $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $NEED >/dev/null \
        || die "apt could not install:$NEED"
    else
      die "please install these packages with your package manager, then run ./install.sh again:$NEED"
    fi
  fi
  # The distribution's package starts an ANONYMOUS broker on 1883 as a system
  # service. pawsight runs its own, with a password (start.sh), on the same port.
  if [ "$NEW_INSTALL" = "1" ] && command -v systemctl >/dev/null 2>&1 \
      && systemctl is-active --quiet mosquitto 2>/dev/null; then
    warn "stopping the system mosquitto service: it has no password and holds port 1883"
    $SUDO systemctl disable --now mosquitto >/dev/null 2>&1 \
      || warn "could not stop it; run: sudo systemctl disable --now mosquitto"
  fi
  if command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; then
    say "uv: already installed"
  else
    say "installing uv (into ~/.local/bin, no sudo)..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || die "failed to install uv"
  fi
  export PATH="$HOME/.local/bin:$PATH"
  if command -v ollama >/dev/null 2>&1; then
    say "ollama: already installed ($(command -v ollama))"
  else
    say "installing Ollama with its official script (it uses sudo and sets up a service)..."
    curl -fsSL https://ollama.com/install.sh | sh || warn "Ollama did not install; see https://ollama.com/download/linux"
  fi
fi

# The MQTT broker listens on loopback only.
if [ "$NEW_INSTALL" = "1" ]; then
  # A new install gets its own password-protected broker (mosquitto/native.conf),
  # started by start.sh. An anonymous system service would take port 1883.
  say "./start.sh will start the password-protected broker (not enabling a system service)"
  nc -z 127.0.0.1 1883 2>/dev/null && warn \
    "port 1883 is already taken by another broker; stop it (macOS: brew services stop mosquitto; Linux: sudo systemctl stop mosquitto)"
elif [ "$OS" = "Darwin" ]; then
  if ! nc -z 127.0.0.1 1883 2>/dev/null; then
    brew services start mosquitto >/dev/null 2>&1 || true
    sleep 2
  fi
  nc -z 127.0.0.1 1883 2>/dev/null && say "mosquitto is answering on 127.0.0.1:1883" \
    || warn "mosquitto is not answering; check: brew services list"
fi

# --- 3. Python environment ---------------------------------------------------
step "Python 3.12 and dependencies"
[ -d .venv ] || uv venv --python 3.12 .venv >/dev/null
PYBIN="$PWD/.venv/bin/python"
ARCH_OK=$("$PYBIN" -c 'import platform;print(platform.machine())')
[ "$ARCH_OK" = "$PY_ARCH" ] || die ".venv contains a $ARCH_OK Python, but $PY_ARCH is required. Delete .venv and try again."
say "interpreter: $("$PYBIN" -V) ($ARCH_OK)"

if [ "$OS" = "Linux" ] && ! command -v nvidia-smi >/dev/null 2>&1 \
    && ! "$PYBIN" -c 'import torch' >/dev/null 2>&1; then
  # Without an NVIDIA GPU the default Linux torch wheel would still pull ~3 GB
  # of CUDA libraries that can never be used. The CPU build is ~10x smaller.
  say "no NVIDIA GPU: installing the CPU build of torch..."
  VIRTUAL_ENV="$PWD/.venv" uv pip install --quiet torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu \
    || warn "the CPU build of torch did not install; falling back to the default wheel"
fi
say "installing dependencies (a few minutes the first time: torch is big)..."
VIRTUAL_ENV="$PWD/.venv" uv pip install --quiet -r requirements.txt \
  || die "dependencies failed to install; for the full output run: VIRTUAL_ENV=\$PWD/.venv uv pip install -r requirements.txt"

DEV=$("$PYBIN" -c 'from pawsight.config import detect_device;print(detect_device())' 2>/dev/null || echo cpu)
case "$DEV" in
  mps)  say "Metal (MPS) is available: inference will run on the GPU" ;;
  cuda*) say "CUDA is available: inference will run on the NVIDIA GPU" ;;
  *)    warn "no GPU available to torch: the detector will run on the CPU, noticeably slower" ;;
esac

# --- 4. go2rtc ---------------------------------------------------------------
# go2rtc must run on the host: it reaches the camera directly over the local
# network, while a container sees the LAN only through NAT.
step "go2rtc"
G2R_VER=v1.9.14
mkdir -p bin
if [ -x bin/go2rtc ]; then
  say "already built: $(./bin/go2rtc --version 2>&1 | head -1)"
else
  G2R_URL="https://github.com/AlexxIT/go2rtc/releases/download/$G2R_VER"
  if [ "$OS" = "Darwin" ]; then
    say "downloading go2rtc $G2R_VER (mac_arm64)..."
    TMP="$(mktemp -d)"
    curl -fsSL -o "$TMP/g.zip" "$G2R_URL/go2rtc_mac_arm64.zip" \
      || die "go2rtc download failed; check your internet connection"
    unzip -q -o "$TMP/g.zip" -d bin/
    xattr -d com.apple.quarantine bin/go2rtc 2>/dev/null || true
    rm -rf "$TMP"
  else
    # On Linux the release asset is the bare binary, not a zip.
    say "downloading go2rtc $G2R_VER ($G2R_ASSET)..."
    curl -fsSL -o bin/go2rtc "$G2R_URL/$G2R_ASSET" \
      || die "go2rtc download failed; check your internet connection"
  fi
  chmod +x bin/go2rtc
  say "ready: $(./bin/go2rtc --version 2>&1 | head -1)"
fi

# The patch is needed ONLY for Xiaomi over P2P: cs2 has a 100-chunk queue, while
# a single 2K HEVC I-frame after a pan is 200-500 KB, so the queue overflowed and
# go2rtc reconnected for 6-19 s. None of this affects a regular RTSP camera.
if [ "$MODE" = "xiaomi" ] && [ ! -f bin/go2rtc.release ]; then
  if command -v go >/dev/null; then
    say "Xiaomi camera: building go2rtc with the cs2 patch..."
    ./scripts/build_go2rtc.sh || warn "the patch did not build; keeping the release go2rtc (video may have gaps after pans)"
  else
    warn "the cs2 patch needs Go (macOS: brew install go; Linux: https://go.dev/dl/)."
    warn "Without it the video will freeze for 6-19 s after camera pans. Later:"
    warn "  ./scripts/build_go2rtc.sh"
  fi
fi

# --- 5. configuration -------------------------------------------------------
step "Configuration"
if [ -f .env ]; then
  say ".env already exists, leaving it alone"
else
  cp .env.example .env
  say ".env created from the template"
fi
# .env holds the Telegram bot token, and since the setup page exists the program
# also writes to it. There is no reason for this file to be world-readable.
chmod 600 .env
if [ -f zones.yaml ]; then
  say "zones.yaml already exists, leaving it alone"
else
  cp zones.example.yaml zones.yaml
  say "zones.yaml created from the template"
fi

set_env() {  # set_env KEY value
  local k="$1" v="$2"
  if grep -qE "^${k}=" .env; then
    # | as the delimiter: URLs contain slashes
    sedi "s|^${k}=.*|${k}=${v}|" .env
  else
    printf '%s=%s\n' "$k" "$v" >> .env
  fi
}

if [ "$MODE" = "rtsp" ]; then
  # A regular camera cannot pan: turn off tilt hold, following and search,
  # otherwise the detector would wait forever for a PTZ that does not exist.
  set_env PAWSIGHT_TILT_HOLD 0
  set_env PAWSIGHT_FOLLOW_DOG 0
  set_env PAWSIGHT_SEARCH_SEC 0
  # The SD substream is a Xiaomi trick (?subtype=sd). A regular camera has none,
  # so the detector watches the same stream as the dashboard.
  set_env PAWSIGHT_DETECT_RTSP "rtsp://127.0.0.1:8554/dog_cam"
  say "mode: regular RTSP camera (PTZ, following and tilt hold are off)"
fi

NEW_G2R=0
if [ ! -f go2rtc/go2rtc.yaml ]; then
  cp go2rtc/go2rtc.example.yaml go2rtc/go2rtc.yaml
  say "go2rtc/go2rtc.yaml created from the template"
  NEW_G2R=1
fi
# Permissions are set here, not only in the --rtsp branch below: the camera URL
# with its password may land in this file later, from the setup page or the go2rtc WebUI.
chmod 600 go2rtc/go2rtc.yaml

# A password on the go2rtc API and WebUI only for a new install: an existing one
# already has go2rtc.yaml, and silently locking its API would break the WebUI it
# uses. Steps for an existing install are in DOCUMENTATION.md.
# Why: 127.0.0.1:1984 is reachable from any page in the browser, and via DNS
# rebinding a foreign site could read /api/config (camera password, Mi token).
if [ "$NEW_G2R" = "1" ]; then
  # Username and password go to go2rtc.yaml (api section) and to .env (GO2RTC_USER/PASS:
  # go2rtc.yaml is not mounted into the brain's container). Never printed to the screen.
  "$PYBIN" -m pawsight.authsetup --go2rtc || warn "go2rtc password was not set"
fi
# Broker password and ACL: mosquitto/passwd (hash), mosquitto/native.conf and
# PAWSIGHT_MQTT_USER/PASS in .env. Natively, start.sh runs its own mosquitto with
# native.conf; docker compose picks mosquitto/auth.conf itself.
if [ "$NEW_INSTALL" = "1" ]; then
  "$PYBIN" -m pawsight.authsetup --mqtt || warn "broker password was not set"
fi

if [ -n "$RTSP_URL" ]; then
  # The camera password is often right in the URL, so the file is owner-only
  # and is in .gitignore.
  "$PYBIN" - "$RTSP_URL" <<'PY'
import sys, pathlib, yaml
url = sys.argv[1]
f = pathlib.Path("go2rtc/go2rtc.yaml")
cfg = yaml.safe_load(f.read_text()) or {}
cfg.setdefault("streams", {})["dog_cam"] = url
f.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
PY
  chmod 600 go2rtc/go2rtc.yaml
  say "dog_cam stream written to go2rtc.yaml (not printing the URL)"
elif [ "$MODE" = "rtsp" ]; then
  warn "The camera RTSP URL is not set. Add it yourself:"
  warn "  ./install.sh --rtsp rtsp://user:password@ADDRESS:554/stream"
fi

# --- 6. models ---------------------------------------------------------------
step "Models"
if [ "$SKIP_MODEL" = "1" ]; then
  say "skipped (--no-model)"
else
  # YOLO weights are downloaded on first use. Do it now so the first real
  # run does not stall on the download.
  say "YOLO weights..."
  "$PYBIN" -c "from ultralytics import YOLO; YOLO('yolo26s.pt')" >/dev/null 2>&1 \
    && say "yolo26s.pt is in place" || warn "YOLO weights did not download; they will be fetched on first run"

  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    if [ "$OS" = "Darwin" ]; then
      brew services start ollama >/dev/null 2>&1 || true
    else
      $SUDO systemctl start ollama >/dev/null 2>&1 || (nohup ollama serve >/dev/null 2>&1 &)
    fi
    for _ in $(seq 1 15); do
      curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  if curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    MODEL="$(grep -E '^OLLAMA_MODEL=' .env | cut -d= -f2)"
    MODEL="${MODEL:-qwen3-vl:8b-instruct}"
    if ollama list 2>/dev/null | grep -q "^${MODEL%%:*}"; then
      say "VLM $MODEL is already pulled"
    else
      say "pulling VLM $MODEL (several GB, this takes a while)..."
      ollama pull "$MODEL" || warn "the model did not download; later run: ollama pull $MODEL"
    fi
  else
    warn "Ollama is not answering. Start it one of these ways:"
    if [ "$OS" = "Darwin" ]; then
      warn "  brew services start ollama     (if installed as a formula)"
      warn "  open -a Ollama                 (if installed as an app)"
    else
      warn "  sudo systemctl start ollama    (or: ollama serve)"
    fi
    warn "then: ollama pull \$OLLAMA_MODEL"
  fi
fi

# --- 7. summary --------------------------------------------------------------
step "Self-test"
mkdir -p storage
"$PYBIN" -m pawsight selftest || true

cat <<EOF

$(printf '\033[1m--- What is left to do by hand ---\033[0m')

The script has no right to do these for you: they need your secrets.
All of it can be entered in the browser: run ./start.sh and open
http://127.0.0.1:8099/setup (the page is reachable only from this machine).

1. Telegram (otherwise events only pile up in the database, with no notifications)
     • message @BotFather → /newbot → it gives you a token
     • message @userinfobot → it gives you your chat_id
     • enter both on the setup page (step 3), or put them in .env
       yourself: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
     • every recipient must message the bot /start THEMSELVES: Telegram does
       not let a bot write first
EOF

if [ "$MODE" = "xiaomi" ]; then
cat <<'EOF'

2. Mi account login (Xiaomi camera only)
     • ./bin/go2rtc -config go2rtc/go2rtc.yaml
     • open http://127.0.0.1:1984 → Add → Xiaomi → sign in
       (the browser asks for the go2rtc login: it is in go2rtc/go2rtc.yaml, api section)
     • the password stays with go2rtc and never reaches the repository
     • then: .venv/bin/python scripts/get_device_token.py   (for pan/tilt)
     • the setup page (step 1, Xiaomi tab) checks whether the account has
       appeared and runs this script for you; it does not ask for a password
EOF
elif [ -z "$RTSP_URL" ]; then
cat <<'EOF'

2. Camera address
     • on the setup page: ./start.sh, then http://127.0.0.1:8099/setup
       (it is checked there too: does the camera answer, is the path right, the password)
     • or from the command line: ./install.sh --rtsp rtsp://user:password@ADDRESS:554/stream
EOF
fi

cat <<'EOF'

3. Confirm which bowl is the water bowl
     The template zones.yaml says "water bowl = LEFT": that is only the default.
     The setup page (step 2) shows live video with the bowls labelled, and
     the choice is one button. By hand: swap select: left /
     select: right in the water and food zones.

Start everything:      ./start.sh
Stop:                  ./stop.sh
Dashboard:             http://127.0.0.1:8099
Setup page:            http://127.0.0.1:8099/setup

EOF
