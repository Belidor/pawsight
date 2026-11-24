"""Behaviour description via a local Ollama.

Called ONLY on an event (state change or zone entry) and with a batch of
frames: a single frame does not convey motion, and the spec forbids running
the VLM on every frame.
"""
from __future__ import annotations

import base64
import logging

import httpx

log = logging.getLogger("pawsight.vlm")

NO_DOG = "NO DOG"

# The model happily invents an action even on a frame without a dog. Verified:
# on an empty room it confidently answered "lying". So the prompt explicitly
# allows a refusal, and the track heuristic stays the primary source anyway.
PROMPT = (
    "These are consecutive frames from a home camera. "
    f"If there is NO dog in the frames, answer exactly: {NO_DOG}. "
    "If there is a dog, describe in ONE short English phrase what it is doing: "
    "lying, sleeping, walking, running, eating, drinking, chewing something, "
    "standing at the door, playing. "
    "Do not describe the dog's appearance or the surroundings. Add no explanations."
)


BOUT_PROMPT = (
    "These are consecutive frames from a home camera. The dog has come to the bowls. "
    "Answer with EXACTLY one word from this list: "
    "DRINKING - if it is drinking water; "
    "EATING - if it is eating food; "
    "UNCLEAR - if it cannot be told from the frames or it is just nearby. "
    "No explanations, just one word."
)


class VLM:
    def __init__(self, url: str, model: str, timeout: float = 90.0) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=5)
            return r.status_code == 200 and any(
                m.get("name", "").startswith(self.model.split(":")[0])
                for m in r.json().get("models", [])
            )
        except Exception:
            return False

    def classify_bout(self, frames_jpeg: list[bytes]) -> tuple[str, str | None]:
        """Tells drinking from eating using the frames of a visit.

        Geometry cannot: the bowls stand side by side, and the floor contact
        point does not say which one the head is over. Returns (kind, raw_answer)
        where kind = drink | eat | unknown.
        """
        raw = self._generate(BOUT_PROMPT, frames_jpeg)
        if not raw:
            return "unknown", None
        u = raw.upper()
        if "DRINK" in u:
            return "drink", raw
        if "EAT" in u and "NOT" not in u:
            return "eat", raw
        return "unknown", raw

    def _generate(self, prompt: str, frames_jpeg: list[bytes]) -> str | None:
        if not frames_jpeg:
            return None
        images = [base64.b64encode(f).decode() for f in frames_jpeg]
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": images,
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": 8192},
        }
        try:
            r = httpx.post(f"{self.url}/api/generate", json=payload, timeout=self.timeout)
            r.raise_for_status()
            return (r.json().get("response") or "").strip() or None
        except Exception as e:
            log.warning("VLM unavailable or failed: %s", type(e).__name__)
            return None

    def look(self, frames_jpeg: list[bytes]) -> tuple[str, str | None]:
        """Three-way answer: ('dog', phrase) | ('no_dog', answer) | ('unknown', None).

        Telling "no dog" apart from "could not ask" is MANDATORY. Suppressing
        notifications on any VLM silence is not allowed: a crashed Ollama would
        then silently cancel every alert, and the owner would notice weeks later.
        """
        if not frames_jpeg:
            return "unknown", None      # nobody to ask: this is not a refusal
        raw = self._generate(PROMPT, frames_jpeg)
        if not raw:
            return "unknown", None
        line = raw.split("\n")[0][:200]
        if NO_DOG in line.upper():
            return "no_dog", line
        return "dog", line

    def describe(self, frames_jpeg: list[bytes]) -> str | None:
        """frames_jpeg: 3-4 frames of the track. Returns a phrase or None."""
        verdict, line = self.look(frames_jpeg)
        return line if verdict == "dog" else None
