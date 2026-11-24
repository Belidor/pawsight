# Contributing to pawsight

Thanks for helping. pawsight is a small project run by one person in their
spare time, so clear, focused pull requests get merged fastest.

## Ground rules

These come from [AGENTS.md](AGENTS.md) and apply to people and AI agents alike:

1. **Never commit secrets or personal data.** No `.env`, no `go2rtc/go2rtc.yaml`,
   no RTSP URLs with passwords, no tokens, no photos of anyone's home. Frames
   in `dataset/` and `storage/` stay on your machine.
2. **Local only.** Every service listens on `127.0.0.1`. A change that opens a
   port to the network, or sends video or images to a cloud service, will not
   be merged.
3. **Do not invent zones.** Zone shapes describe a real room. Example zones in
   the repository are examples; do not "improve" them without a frame from
   that room (AGENTS.md, section 6).
4. **Fine-tuning keeps three classes**, `dog`, `bowl` and `person`, and the
   acceptance gate in `pawsight/train.py` stays (AGENTS.md, section 7).
5. **English** in code, comments, UI text and docs.

## Development setup

```bash
git clone https://github.com/Belidor/pawsight.git && cd pawsight
./install.sh --no-model     # macOS (Apple Silicon) or Linux
```

You do not need a camera to work on most of the code: the tests use fakes for
the camera, the bus, YOLO and the VLM.

## Before you open a pull request

```bash
.venv/bin/python -m unittest discover -s tests
ruff check .
```

Both run in CI on Ubuntu and macOS. Add a test for a bug fix or a new
behaviour; look at the existing ones in `tests/` for the style (a docstring that
says what real failure the test guards against).

Comments in this code base explain **why**, often with the measurement behind a
decision. Keep that habit: a number you measured is worth more than an
adjective. Big design decisions and their measurements are summarised in
[docs/architecture.md](docs/architecture.md); read the relevant part before
changing how the detector, zones or patrol work.

## Reporting bugs and ideas

Use the issue templates. For security problems, see [SECURITY.md](SECURITY.md)
and report privately.

## Licence

pawsight is licensed under the [GNU AGPL-3.0](LICENSE). By contributing you
agree that your contribution is licensed under the same terms.
