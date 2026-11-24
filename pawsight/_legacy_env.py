"""Accept settings written under the project's former name.

The project was called dog-cam, and its settings used the DOGCAM_ prefix.
Existing installs keep a .env full of DOGCAM_* keys, and some of them are
secrets the owner typed in (MQTT password). Renaming them silently would leave
such an install without a broker login after an update, so old keys are still
honoured: every DOGCAM_X becomes PAWSIGHT_X unless PAWSIGHT_X is already set.
The new name always wins.

This runs on package import, before any module reads the environment.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

LEGACY = "DOGCAM_"
CURRENT = "PAWSIGHT_"

log = logging.getLogger("pawsight")


def adopt_legacy_env(environ: dict | None = None, env_file: Path | None = None) -> list[str]:
    """Copy DOGCAM_* to PAWSIGHT_* where the new key is missing.

    Returns the list of adopted keys (new names), for logging and tests.
    """
    env = os.environ if environ is None else environ
    if env_file is None and environ is None:
        try:
            from dotenv import load_dotenv
        except ImportError:  # the brain image always has it; be lenient anyway
            load_dotenv = None
        if load_dotenv is not None:
            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    elif env_file is not None:
        from dotenv import dotenv_values
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                env.setdefault(k, v)
    adopted = []
    for key in list(env):
        if key.startswith(LEGACY):
            new = CURRENT + key[len(LEGACY):]
            if new not in env:
                env[new] = env[key]
                adopted.append(new)
    if adopted:
        log.warning("settings with the old DOGCAM_ prefix are still read, but please "
                    "rename them to PAWSIGHT_ in .env: %s", ", ".join(sorted(adopted)))
    return adopted
