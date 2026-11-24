#!/usr/bin/env bash
# Builds go2rtc v1.9.14 with the cs2 patch (patches/go2rtc-v1.9.14-cs2-popbuf.diff)
# for this machine (macOS or Linux) and puts it in bin/go2rtc; the original release is kept
# as bin/go2rtc.release. Roll back with: cp bin/go2rtc.release bin/go2rtc
#
# Why the patch. go2rtc's cs2 media queue holds 100 chunks (~120 KB); a single
# 2K HEVC I-frame after a camera pan is 200-500 KB. The queue overflowed, and
# that was treated as fatal: go2rtc reconnected to the camera for 6-19 s after
# every pan. The patch: queue of 4096, and overflow is non-fatal.
# Measured: 9 pans in 90 s, 0 gaps, 0 reconnects (was 3-4 gaps).
set -uo pipefail
cd "$(dirname "$0")/.."
TAG=v1.9.14
SRC="${TMPDIR:-/tmp}/go2rtc-src-$TAG"
command -v go >/dev/null || { echo "Go is required (macOS: brew install go; Linux: https://go.dev/dl/)"; exit 1; }
[ -d "$SRC/.git" ] || git clone -q --depth 1 --branch "$TAG" https://github.com/AlexxIT/go2rtc.git "$SRC" || exit 1
cd "$SRC" && git checkout -q -- . && git apply "$OLDPWD/patches/go2rtc-$TAG-cs2-popbuf.diff" || { echo "patch did not apply"; exit 1; }
export GOTOOLCHAIN=auto GOFLAGS=-mod=mod
CGO_ENABLED=0 go build -ldflags="-s -w" -o "$OLDPWD/bin/go2rtc.patched" . || exit 1
cd "$OLDPWD"
[ -f bin/go2rtc.release ] || cp bin/go2rtc bin/go2rtc.release
mv bin/go2rtc.patched bin/go2rtc && chmod +x bin/go2rtc && { xattr -d com.apple.quarantine bin/go2rtc 2>/dev/null || true; }
echo "done: $(./bin/go2rtc --version 2>&1 | head -1)"
echo "restart: pkill -f 'go2rtc -config'; ./start.sh"
