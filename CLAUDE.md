# CLAUDE.md

The agent instructions live in **[AGENTS.md](AGENTS.md)**: read it in full
before your first action.

Five rules from it that are broken most often:

1. We do not ask for or enter anyone's passwords: not Mi, not Telegram, not
   the camera's.
2. We do not commit `.env` or `go2rtc/go2rtc.yaml`, and do not print them to
   output.
3. Everything listens on `127.0.0.1` only; nothing is exposed to the outside.
4. We do not invent zones for the human (`AGENTS.md`, section 6).
5. Fine-tuning always keeps `dog`/`bowl`/`person` as classes 0-2 (extras only
   after them, via `PAWSIGHT_CLASSES`); do not bypass the
   acceptance gate in `pawsight/train.py` (`AGENTS.md`, section 7).

Install: `./install.sh --rtsp '<url>'`. Run: `./start.sh`.
Check: `.venv/bin/python -m pawsight selftest`.
