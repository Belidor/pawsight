#!/usr/bin/env bash
# Starts the whole system: the native parts + the "brain".
#
# Why some parts are native: Metal is not available to a Linux guest in Docker
# or in Apple Container, and go2rtc reaches the camera over the local network.
# On Linux everything runs natively (PAWSIGHT_STACK=native, set by install.sh).
#
# PAWSIGHT_STACK in .env chooses where the broker and the brain live:
#   native — our own mosquitto + the brain in .venv (no Docker needed at all)
#   docker — mosquitto and the brain in containers (docker-compose.yml)
# No key in .env => docker, so that existing installs keep working unchanged.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '  %s\n' "$*"; }

# No zones of our own yet (a fresh clone without install.sh): take the template,
# otherwise the detector would silently run without a single zone.
[ -f zones.yaml ] || cp zones.example.yaml zones.yaml

# `|| true` is required: with set -o pipefail a failing grep (key not in .env)
# propagates through the pipeline, and set -e kills the script on the assignment
# itself, BEFORE the default below applies. The script exits silently, which
# looks like "./start.sh did nothing". That is exactly the case the header
# comment promises to support: existing installs do not have the key.
# DOGCAM_STACK is the key's former name: older installs still have it.
STACK="$(grep -E '^PAWSIGHT_STACK=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
[ -n "$STACK" ] || STACK="$(grep -E '^DOGCAM_STACK=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
STACK="${STACK:-docker}"
if [ "$(uname -s)" = "Linux" ] && [ "$STACK" = "docker" ]; then
  # On Linux, host.docker.internal is the docker bridge, not the host's
  # loopback where go2rtc and Ollama listen, so the brain container cannot reach them.
  say "Linux: running the brain natively (PAWSIGHT_STACK=docker is for macOS)"
  STACK=native
fi

# The setup wizard holds the same port as the brain. If it is left over from last
# time, kill it, otherwise the brain silently fails to start.
pkill -f "(pawsight|dogcam) setup" >/dev/null 2>&1 || true

# --- 1. camera stream ---
if pgrep -f "go2rtc -config" >/dev/null; then
  say "go2rtc is already running"
else
  # Rotate the log, do NOT truncate it: the stream is opened with `>`, without
  # O_APPEND, and the writer keeps its offset, so a truncated file instantly grows
  # back with megabytes of zeros. As a bonus, cleanup gets a .1 file it may delete.
  [ -f go2rtc/go2rtc.log ] && mv -f go2rtc/go2rtc.log go2rtc/go2rtc.log.1
  nohup ./bin/go2rtc -config go2rtc/go2rtc.yaml > go2rtc/go2rtc.log 2>&1 &
  sleep 3
  say "go2rtc started"
fi

# SD substream for the detector (stable; 2K HD has gaps when received over P2P).
# This is a Xiaomi camera trick (?subtype=sd); skipped for a regular RTSP camera,
# where the detector and the dashboard watch the same stream.
# IMPORTANT: it must be .venv/bin/python, not the system python3: the system one
# has no pyyaml, and this block silently failed on every start, always reporting "skipped".
./.venv/bin/python - <<'PYSD' 2>/dev/null && say "SD substream set up" || say "SD substream: not needed or skipped"
import urllib.parse,urllib.request,yaml,pathlib,sys
g=yaml.safe_load((pathlib.Path("go2rtc")/"go2rtc.yaml").read_text())
base=(g.get("streams") or {}).get("dog_cam")
if isinstance(base,list): base=base[0]
if not base or not base.startswith("xiaomi://"): sys.exit(1)
if "subtype" not in base:
    src=base+("&" if "?" in base else "?")+"subtype=sd"
    q=urllib.parse.urlencode({"name":"dog_cam_sd","src":src})
    req=urllib.request.Request(f"http://127.0.0.1:1984/api/streams?{q}",method="PUT")
    # go2rtc with a password (api.username/password, local_auth: true) answers 401 without it
    api=g.get("api") or {}
    if api.get("username"):
        import base64
        tok=base64.b64encode(f'{api["username"]}:{api.get("password") or ""}'.encode()).decode()
        req.add_header("Authorization","Basic "+tok)
    urllib.request.urlopen(req,timeout=20)
PYSD

# --- 2. broker and brain ---
mkdir -p storage
if [ "$STACK" = "native" ]; then
  if [ -f mosquitto/native.conf ]; then
    # Install with a broker password: our own mosquitto (loopback, password, ACL),
    # not the anonymous Homebrew service. The pid is in mosquitto/mosquitto.pid.
    if ! nc -z 127.0.0.1 1883 2>/dev/null; then
      # Debian/Ubuntu put it in /usr/sbin, which is not on a normal user's PATH.
      MOSQ="$(command -v mosquitto || { [ -x /usr/sbin/mosquitto ] && echo /usr/sbin/mosquitto; } \
              || echo "$(brew --prefix 2>/dev/null)/sbin/mosquitto")"
      "$MOSQ" -c mosquitto/native.conf -d || true
      sleep 1
    elif ! { [ -f mosquitto/mosquitto.pid ] && kill -0 "$(cat mosquitto/mosquitto.pid)" 2>/dev/null; }; then
      say "WARNING: port 1883 is taken by another broker (a system service?). It has no password,"
      say "so the detector and the brain cannot log in to it. Stop it:"
      say "  macOS: brew services stop mosquitto    Linux: sudo systemctl disable --now mosquitto"
    fi
    nc -z 127.0.0.1 1883 2>/dev/null && say "mosquitto (with password) is listening on 127.0.0.1:1883" \
      || say "mosquitto is NOT answering; see storage/mosquitto.log"
  else
    nc -z 127.0.0.1 1883 2>/dev/null || { command -v brew >/dev/null && brew services start mosquitto >/dev/null 2>&1; } || true
    sleep 1
    nc -z 127.0.0.1 1883 2>/dev/null && say "mosquitto is listening on 127.0.0.1:1883" \
      || say "mosquitto is NOT answering; check: brew services list"
  fi
  if pgrep -f "(pawsight|dogcam) brain" >/dev/null; then
    say "brain is already running"
  else
    [ -f storage/brain.log ] && mv -f storage/brain.log storage/brain.log.1
    nohup ./.venv/bin/python -m pawsight brain > storage/brain.log 2>&1 &
    sleep 2
    say "brain started (native)"
  fi
else
  # --build is required, not a nicety. The Dockerfile copies pawsight/ into the
  # image, so without a rebuild the host runs the new code while the brain in the
  # container runs the old one, and they diverge silently. Layers are cached: when
  # nothing changed, the step takes a second.
  docker compose up -d --build >/dev/null
  say "containers are up (image rebuilt if the code changed)"
fi

# --- 3. detector ---
if pgrep -f "(pawsight|dogcam) detect" >/dev/null; then
  say "detector is already running"
else
  # do not overwrite the log: the previous one goes to detect.log.1, otherwise incidents cannot be investigated
  [ -f storage/detect.log ] && mv -f storage/detect.log storage/detect.log.1
  nohup ./.venv/bin/python -m pawsight detect > storage/detect.log 2>&1 &
  sleep 5
  say "detector started"
fi

echo
# The self-test opens RTSP via ffmpeg and with an unconfigured camera it just
# hangs until the timeout: the user stares at a frozen terminal for tens of
# seconds instead of getting a hint. So first check whether a stream is set.
if ./.venv/bin/python -c "from pawsight.setup import camera_configured
raise SystemExit(0 if camera_configured() else 1)" 2>/dev/null; then
  ./.venv/bin/python -m pawsight selftest
else
  say "camera is not configured yet; skipping the self-test (it would hang until the timeout)."
  say "Enter the camera address here: http://127.0.0.1:8099/setup"
fi
