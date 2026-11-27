"""Download the two binaries the desktop app bundles: uv and go2rtc.

    python desktop/fetch_sidecars.py                 # for this machine
    python desktop/fetch_sidecars.py --target x86_64-pc-windows-msvc

Tauri wants them as src-tauri/binaries/<name>-<target triple>[.exe]. They
are not committed (binaries/ is git-ignored); CI and anyone building the app
run this first. Versions are pinned: go2rtc the same as install.sh.
"""
from __future__ import annotations

import argparse
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

UV_VERSION = "0.12.24"
GO2RTC_VERSION = "v1.9.14"          # keep in step with install.sh (G2R_VER)

UV_URL = "https://github.com/astral-sh/uv/releases/download/{v}/uv-{t}.{ext}"
G2R_URL = "https://github.com/AlexxIT/go2rtc/releases/download/{v}/{asset}"

# target triple -> go2rtc release asset (zip on macOS/Windows, bare binary on Linux)
G2R_ASSET = {
    "aarch64-apple-darwin": "go2rtc_mac_arm64.zip",
    "x86_64-pc-windows-msvc": "go2rtc_win64.zip",
    "aarch64-pc-windows-msvc": "go2rtc_win_arm64.zip",
    "x86_64-unknown-linux-gnu": "go2rtc_linux_amd64",
    "aarch64-unknown-linux-gnu": "go2rtc_linux_arm64",
}

OUT = Path(__file__).resolve().parent / "src-tauri" / "binaries"


def host_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True).stdout
    return next(ln.split(": ", 1)[1] for ln in out.splitlines() if ln.startswith("host: "))


def get(url: str, attempts: int = 4) -> bytes:
    """GitHub release downloads time out now and then (seen on a Windows CI
    runner): retry with a growing pause instead of failing the whole build."""
    import time

    for i in range(1, attempts + 1):
        print(f"  downloading {url}" + (f" (attempt {i})" if i > 1 else ""), flush=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except OSError as e:
            if i == attempts:
                raise
            print(f"  {type(e).__name__}: {e}; retrying", flush=True)
            time.sleep(5 * i)
    raise AssertionError("unreachable")


def member(data: bytes, name: str, is_zip: bool) -> bytes:
    """The file called `name` anywhere inside the archive."""
    if is_zip:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in z.namelist():
                if n.rsplit("/", 1)[-1] == name:
                    return z.read(n)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for m in t.getmembers():
                if m.isfile() and m.name.rsplit("/", 1)[-1] == name:
                    f = t.extractfile(m)
                    assert f is not None
                    return f.read()
    raise SystemExit(f"{name} not found in the archive")


def write(name: str, triple: str, body: bytes) -> None:
    suffix = ".exe" if "windows" in triple else ""
    dst = OUT / f"{name}-{triple}{suffix}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(body)
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"  -> {dst.relative_to(OUT.parent.parent)} ({len(body) / 1e6:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="Rust target triple (default: this machine)")
    a = ap.parse_args()
    triple = a.target or host_triple()
    if triple not in G2R_ASSET:
        raise SystemExit(f"unsupported target {triple}; known: {', '.join(G2R_ASSET)}")
    win = "windows" in triple
    exe = ".exe" if win else ""

    print(f"uv {UV_VERSION} for {triple}")
    ext = "zip" if win else "tar.gz"
    data = get(UV_URL.format(v=UV_VERSION, t=triple, ext=ext))
    write("uv", triple, member(data, f"uv{exe}", is_zip=win))

    print(f"go2rtc {GO2RTC_VERSION} for {triple}")
    asset = G2R_ASSET[triple]
    data = get(G2R_URL.format(v=GO2RTC_VERSION, asset=asset))
    if asset.endswith(".zip"):
        data = member(data, f"go2rtc{exe}", is_zip=True)
    write("go2rtc", triple, data)
    if shutil.which("xattr") and sys.platform == "darwin":
        for f in OUT.iterdir():
            os.system(f"xattr -d com.apple.quarantine '{f}' 2>/dev/null")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
