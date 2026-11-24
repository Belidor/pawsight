## What and why

<!-- What does this change, and what problem does it solve? -->

## How it was checked

- [ ] `python -m unittest discover -s tests` passes
- [ ] `ruff check .` passes
- [ ] Tried on real hardware (say which camera and platform), or explain why not needed

## Checklist

- [ ] No secrets, personal data, home photos or model weights in the diff
- [ ] Services still listen on `127.0.0.1` only
- [ ] No zone geometry invented for someone else's room (see AGENTS.md, section 6)
- [ ] Docs updated if behaviour or setup changed
