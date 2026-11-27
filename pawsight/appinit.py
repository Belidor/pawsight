"""`python -m pawsight appinit`: the configuration half of install.sh, in Python.

The desktop app runs it after it has created .venv. It is idempotent like
install.sh: an existing file is left alone, only what is missing is created.

What it does:
  * .env, zones.yaml, go2rtc/go2rtc.yaml from the templates (owner-only);
  * PAWSIGHT_STACK=native: the app runs the brain natively, never in Docker;
  * on a NEW install, passwords on the go2rtc API and the MQTT broker, the
    same way install.sh does (pawsight.authsetup), values never printed;
  * with --models: the YOLO weights, so the first start does not stall.

What it never does (AGENTS.md, section 1): it asks for no passwords and
writes no camera address. The camera, Telegram and the water bowl are the
human's steps on the setup page.

Output is one line per step, `step: text`, and the desktop app shows it as
the installation progress.
"""
from __future__ import annotations

import argparse
import shutil
import socket
import sys
from pathlib import Path


def say(step: str, text: str) -> None:
    print(f"{step}: {text}", flush=True)


def _copy_template(src: Path, dst: Path) -> bool:
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    try:
        dst.chmod(0o600)
    except OSError:
        pass
    return True


def configure(root: Path) -> dict:
    """Create what is missing. Returns which files are new (for the tests)."""
    from . import authsetup, setup

    env = root / ".env"
    new_install = not env.exists()
    made = {
        "env": _copy_template(root / ".env.example", env),
        "zones": _copy_template(root / "zones.example.yaml", root / "zones.yaml"),
        "go2rtc": _copy_template(root / "go2rtc" / "go2rtc.example.yaml",
                                 root / "go2rtc" / "go2rtc.yaml"),
    }
    setup.set_env({"PAWSIGHT_STACK": "native"}, env, root / ".env.example")
    if made["go2rtc"]:
        authsetup.enable_go2rtc_auth(root / "go2rtc" / "go2rtc.yaml", env)
    if new_install:
        authsetup.enable_mqtt_auth(root / "mosquitto", env)
    (root / "storage").mkdir(exist_ok=True)
    made["new_install"] = new_install
    return made


def ollama_state(url: str = "http://127.0.0.1:11434", model: str = "") -> str:
    """'missing' (nothing on the port), 'no-model' or 'ready'."""
    import json
    import urllib.request

    host, _, port = url.split("://", 1)[-1].partition(":")
    try:
        with socket.create_connection((host, int(port or 11434)), timeout=0.5):
            pass
    except OSError:
        return "missing"
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=3) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception:
        return "no-model"
    base = model.split(":", 1)[0]
    return "ready" if any(n == model or n.split(":", 1)[0] == base for n in names) else "no-model"


def pull_vlm(url: str, model: str) -> bool:
    """`ollama pull` through Ollama's own HTTP API, with progress lines.

    Through the API, not the `ollama` command: the desktop app cannot count
    on the command being on PATH (Ollama.app on macOS does not add it until
    asked), while the API is always at 127.0.0.1:11434 when Ollama runs.
    """
    import json
    import urllib.request

    req = urllib.request.Request(url.rstrip("/") + "/api/pull",
                                 data=json.dumps({"model": model}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    last = -1
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if d.get("error"):
                    say("ollama", f"error: {d['error']}")
                    return False
                total, done = d.get("total") or 0, d.get("completed") or 0
                if total:
                    pct = int(done * 100 / total)
                    if pct // 5 != last // 5:          # every 5 %, not every chunk
                        last = pct
                        say("ollama", f"pulling {model}: {pct}% of {total / 1e9:.1f} GB")
                elif d.get("status"):
                    say("ollama", str(d["status"]))
    except OSError as e:
        say("ollama", f"error: Ollama did not answer ({e})")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pawsight appinit")
    ap.add_argument("--models", action="store_true", help="also download the YOLO weights")
    ap.add_argument("--pull-vlm", action="store_true",
                    help="only download the VLM into Ollama (several GB) and report its state")
    a = ap.parse_args(argv)

    from .config import ROOT

    if a.pull_vlm:
        from dotenv import dotenv_values

        vals = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
        url = vals.get("OLLAMA_URL") or "http://127.0.0.1:11434"
        vlm = vals.get("OLLAMA_MODEL") or "qwen3-vl:8b-instruct"
        ok = pull_vlm(url, vlm)
        say("ollama", ollama_state(url, vlm) + f" {vlm}")
        return 0 if ok else 1

    made = configure(ROOT)
    say("config", "new install: templates copied, passwords set" if made["new_install"]
        else "existing configuration kept")

    import os
    from dotenv import dotenv_values

    vals = dotenv_values(ROOT / ".env")
    if a.models:
        model = vals.get("PAWSIGHT_MODEL") or "yolo26s.pt"
        if (ROOT / model).exists():
            say("yolo", f"{model} is in place")
        else:
            say("yolo", f"downloading {model}...")
            try:
                os.chdir(ROOT)
                from ultralytics import YOLO

                YOLO(model)
                say("yolo", f"{model} downloaded")
            except Exception as e:
                say("yolo", f"not downloaded ({type(e).__name__}); it will be fetched on first run")
    try:
        from .config import detect_device

        dev = detect_device()
        say("device", {"mps": "Metal (Apple GPU)", "cpu": "CPU (no GPU available to torch)"}
            .get(dev, f"NVIDIA GPU ({dev})" if dev.startswith("cuda") else dev))
    except Exception as e:
        say("device", f"unknown ({type(e).__name__})")
    vlm = vals.get("OLLAMA_MODEL") or "qwen3-vl:8b-instruct"
    say("ollama", ollama_state(vals.get("OLLAMA_URL") or "http://127.0.0.1:11434", vlm) + f" {vlm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
