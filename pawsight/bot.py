"""Bot commands: "show me what's there right now".

The bot **polls** Telegram (getUpdates) instead of receiving a webhook. A
webhook would need a port open to the internet, and this project exposes
nothing to the outside, by rule rather than by accident (AGENTS.md, section 1).
Polling is an ordinary outgoing HTTPS connection.

We answer ONLY the chats in `TELEGRAM_CHAT_ID`. Knowing the bot's @name and
being able to message it is not the same as having the right to look into
someone's room, and the bot token lives in `.env` and may one day leak along
with it. The recipient list already exists, and it is the allow list.

Strangers get NO answer at all, not even "access denied": such an answer
confirms to whoever typed it by chance that the bot is alive and someone's
home is behind it. Silence confirms nothing.

Empty recipient list = the bot does not start. Otherwise "haven't set the
chat_id yet" would mean "accepting commands from anyone".
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .notify import Telegram
from .snapshot import Snapshot

log = logging.getLogger("pawsight.bot")

SNAP = ("/snap", "/frame", "/photo", "snap", "frame", "photo", "snapshot")
HELP = ("/start", "/help", "help")

HELP_TEXT = (
    "I show what the camera sees right now.\n\n"
    "/snap — a fresh frame\n"
    "/help — this help\n\n"
    "Events (visits to the bowls, behaviour changes) arrive on their own, "
    "no need to ask for them."
)


class CommandBot:
    """Telegram polling loop. A separate daemon thread: it does not block exit.

    `snapshot` is a no-argument function returning `(jpeg, note)`; where it
    gets the frame from is none of the bot's business (see pawsight/snapshot.py).
    """

    def __init__(self, tg: Telegram, snapshot: Callable[[], Snapshot],
                 poll_timeout: int = 25) -> None:
        self.tg = tg
        self.snapshot = snapshot
        self.poll_timeout = poll_timeout
        self._offset: int | None = None
        self._stop = threading.Event()

    # ---------- parsing ----------

    @staticmethod
    def _command(text: str) -> str | None:
        """The first word of the message, normalised.

        `/snap@dog_cam_bot` is how Telegram rewrites a command in a group;
        without stripping `@bot` the bot would not answer in a group at all.
        """
        word = (text or "").strip().split()
        if not word:
            return None
        cmd = word[0].split("@")[0].lower()
        return cmd if cmd in SNAP or cmd in HELP or cmd.startswith("/") else None

    def _allowed(self, chat: str) -> bool:
        return chat in self.tg.chats

    # ---------- replies ----------

    def _snap(self, chat: str) -> None:
        self.tg.action(chat, "upload_photo")
        jpeg, note = self.snapshot()
        if not jpeg:
            self.tg.reply(chat, f"⚠️ No frame: {note or 'the source is silent'}")
            return
        caption = f"📷 {time.strftime('%H:%M:%S')}"
        if note:
            caption += f"\n⚠️ {note}"
        self.tg.reply(chat, caption, jpeg)

    def _handle(self, chat: str, cmd: str) -> None:
        try:
            if cmd in SNAP:
                self._snap(chat)
            elif cmd in HELP:
                self.tg.reply(chat, HELP_TEXT)
            else:
                self.tg.reply(chat, f"Unknown command {cmd}.\n\n{HELP_TEXT}")
        except Exception:
            # A failed command must not bring down polling: the next one may be
            # the very one the person picked up their phone for.
            log.exception("command %s failed", cmd)

    # ---------- loop ----------

    def poll_once(self) -> int:
        """One getUpdates round. Returns the number of commands executed.

        Repeats within one batch are collapsed: if someone impatiently pressed
        /snap ten times (or the bot was down for half an hour), they want one
        fresh frame, not ten identical ones in a row.
        """
        todo: dict[tuple[str, str], None] = {}
        for upd in self.tg.get_updates(self._offset, self.poll_timeout):
            try:
                uid = int(upd["update_id"])
            except (KeyError, TypeError, ValueError):
                continue
            self._offset = max(self._offset or 0, uid + 1)

            msg = upd.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            if not chat:
                continue
            if not self._allowed(chat):
                # The chat_id is not printed: it identifies a person.
                log.info("message from an unknown chat ignored")
                continue
            cmd = self._command(msg.get("text", ""))
            if cmd:
                todo[(chat, cmd)] = None

        for chat, cmd in todo:
            self._handle(chat, cmd)
        return len(todo)

    def _drop_backlog(self) -> None:
        """Skip everything that piled up while the brain was not running.

        Otherwise a morning "/snap" gets answered in the evening when the brain
        comes up, with an evening frame. "Right now" cannot be retroactive.
        """
        try:
            updates = self.tg.get_updates(-1, 0)
            if updates:
                self._offset = int(updates[-1]["update_id"]) + 1
                log.info("messages that piled up before start were skipped")
        except Exception:
            # Do not stumble at the start: without an offset the first batch
            # will bring old messages, but the bot stays alive.
            log.exception("could not skip old messages")

    def run_forever(self) -> None:
        self._drop_backlog()
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("Telegram polling failed, continuing")
                self._stop.wait(5)

    def stop(self) -> None:
        self._stop.set()

    def start(self) -> bool:
        """Start the polling thread. False if there is nothing to start."""
        if not self.tg.enabled:
            log.info("bot commands disabled: TELEGRAM_BOT_TOKEN "
                     "or TELEGRAM_CHAT_ID is not set")
            return False
        threading.Thread(target=self.run_forever, name="telegram-bot",
                         daemon=True).start()
        log.info("bot accepts commands from %d recipients: /snap for a camera frame",

                 len(self.tg.chats))
        return True
