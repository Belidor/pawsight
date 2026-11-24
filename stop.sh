#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")"
# Fine-tuning runs in a SEPARATE session so it survives a detector restart, and
# it does not match the "pawsight detect" pattern. Without this check the owner
# would see "detector stopped" and walk away, while the run kept holding the GPU
# and at the end SILENTLY replaced the weights.
if [ -f storage/train.lock ]; then
  PID=$(python3 -c 'import json;print(json.load(open("storage/train.lock"))["pid"])' 2>/dev/null || echo)
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    echo "  WARNING: fine-tuning is running (pid $PID)."
    printf "  Stop it too? [y/N] "
    read -r ans
    case "$ans" in
      [yY]*) kill -TERM "$PID" 2>/dev/null && rm -f storage/train.lock && echo "  fine-tuning stopped" ;;
      *) echo "  fine-tuning keeps running; when it finishes it may replace the weights" ;;
    esac
  else
    rm -f storage/train.lock
  fi
fi

pkill -f "(pawsight|dogcam) detect" 2>/dev/null && echo "  detector stopped" || echo "  detector was not running"
pkill -f "(pawsight|dogcam) brain"  2>/dev/null && echo "  brain stopped"        || true
# The setup wizard listens on the same port as the brain: leaving it running
# means the brain quietly gets "address already in use" in storage/brain.log.
pkill -f "(pawsight|dogcam) setup"  2>/dev/null && echo "  setup wizard stopped" || true
pkill -f "go2rtc -config" 2>/dev/null && echo "  go2rtc stopped" || echo "  go2rtc was not running"
docker compose down >/dev/null 2>&1 && echo "  containers stopped" || true
# The password-protected broker (mosquitto/native.conf, started by start.sh) is
# ours, so stop it. The Homebrew service is deliberately NOT touched: something
# other than this project may have started it. To stop it by hand: brew services stop mosquitto
if [ -f mosquitto/mosquitto.pid ] && kill "$(cat mosquitto/mosquitto.pid)" 2>/dev/null; then
  echo "  broker (with password) stopped"
fi
