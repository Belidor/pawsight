"""Telegram: snapshot + text, with deduplication and rate limiting.

This is also the only place where the token goes into an HTTP request: both
for sending events and for receiving the user's commands (`getUpdates`). It is
kept in one file on purpose, so that "never print the token" is checked in
one place rather than three.

The token comes from the environment. Our own log records contain neither it
nor the chat_id: the chat_id identifies a person, so error messages carry only
the recipient's ordinal number.

But the token sits IN the URL itself (.../bot<TOKEN>/sendPhoto), and httpx at
INFO level prints the full URL of every request. That is why the httpx and
httpcore loggers are muted in pawsight.__main__._log(); only touch that if you
understand that otherwise the token ends up in the process log.

There can be several recipients (TELEGRAM_CHAT_ID, comma-separated). The photo
is then uploaded ONCE: the file_id is taken from Telegram's response, and the
others get that instead of the same 100 KB JPEG all over again.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

log = logging.getLogger("pawsight.notify")

API = "https://api.telegram.org"


def parse_chats(value: str | Iterable[str] | None) -> list[str]:
    """'111, 222' -> ['111','222']. A single id keeps working as before."""
    if value is None:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    return [p.strip() for p in parts if p and p.strip()]


def _file_id(resp: httpx.Response) -> str | None:
    """file_id of the largest size in a sendPhoto response.

    Telegram returns several sizes of one picture; the largest must be
    forwarded to the other recipients, otherwise they would see a thumbnail.
    """
    try:
        sizes = (resp.json().get("result") or {}).get("photo") or []
    except Exception:
        return None
    if not sizes:
        return None
    return max(sizes, key=lambda p: p.get("file_size", 0)).get("file_id")


class Telegram:
    def __init__(self, token: str, chat_id: str | Iterable[str],
                 rate_limit_sec: int = 300) -> None:
        self._token = token
        self.chats = parse_chats(chat_id)
        self.rate_limit_sec = rate_limit_sec
        self._last_sent: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._token and self.chats)

    def _allowed(self, key: str) -> bool:
        now = time.time()
        last = self._last_sent.get(key, 0.0)
        if now - last < self.rate_limit_sec:
            return False
        self._last_sent[key] = now
        return True

    # ---------- sending to one recipient ----------

    def _post_text(self, chat: str, text: str) -> httpx.Response:
        return httpx.post(
            f"{API}/bot{self._token}/sendMessage",
            data={"chat_id": chat, "text": text[:4096]},
            timeout=20,
        )

    def _post_photo_bytes(self, chat: str, text: str, photo: bytes) -> httpx.Response:
        return httpx.post(
            f"{API}/bot{self._token}/sendPhoto",
            data={"chat_id": chat, "caption": text[:1024]},
            files={"photo": ("snap.jpg", photo, "image/jpeg")},
            timeout=30,
        )

    def _post_photo_id(self, chat: str, text: str, file_id: str) -> httpx.Response:
        return httpx.post(
            f"{API}/bot{self._token}/sendPhoto",
            data={"chat_id": chat, "caption": text[:1024], "photo": file_id},
            timeout=20,
        )

    # ---------- replying to a user's request ----------

    def reply(self, chat: str, text: str, photo: bytes | None = None) -> bool:
        """Reply to ONE chat: no rate limit and no broadcast to the others.

        The rate limit does not apply here: it is about the frequency of EVENTS
        the system came up with on its own. Here a person asked, and staying
        silent is not an option, or the bot looks broken.

        And only the asker gets the reply: the other recipients subscribed to
        the dog's events, not to someone else's frame requests.
        """
        if not self._token:
            return False
        try:
            r = (self._post_photo_bytes(chat, text, photo) if photo
                 else self._post_text(chat, text))
        except Exception as e:
            # Print NEITHER the URL (it holds the token) NOR the chat_id
            log.warning("Telegram: reply not sent (%s)", type(e).__name__)
            return False
        if r.status_code != 200:
            log.warning("Telegram answered %s to a chat reply", r.status_code)
            return False
        return True

    def action(self, chat: str, name: str = "upload_photo") -> None:
        """"sending photo..." in the chat header while we grab a frame.

        The frame arrives in a second or two, and without an indicator it looks
        as if the bot did not hear. Cosmetic: any error here is silently ignored.
        """
        if not self._token:
            return
        try:
            httpx.post(f"{API}/bot{self._token}/sendChatAction",
                       data={"chat_id": chat, "action": name}, timeout=10)
        except Exception as e:
            log.debug("Telegram: indicator not shown (%s)", type(e).__name__)

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        """Incoming bot messages, long polling.

        Polling, not a webhook: a webhook needs a port open to the outside, and
        this project exposes nothing to the outside. getUpdates is an ordinary
        OUTGOING connection, so the "listen only on 127.0.0.1" rule holds.

        A failure is an empty list, not an exception: the bot loop must survive
        a network outage, not die with it.
        """
        if not self._token:
            return []
        params: dict[str, object] = {"timeout": timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        try:
            # Our timeout is LONGER than the server's: Telegram holds the reply for
            # up to timeout seconds, and an equal client timeout would cut every
            # empty poll.
            r = httpx.get(f"{API}/bot{self._token}/getUpdates",
                          params=params, timeout=timeout + 10)
        except Exception as e:
            log.warning("Telegram: polling for updates failed (%s)", type(e).__name__)
            return []
        if r.status_code != 200:
            log.warning("Telegram answered %s to an updates poll", r.status_code)
            return []
        try:
            return list(r.json().get("result") or [])
        except Exception:
            log.warning("Telegram: could not parse the poll response")
            return []

    # ---------- broadcast ----------

    def send(self, text: str, photo: bytes | None = None, key: str = "default") -> bool:
        """True if the message reached AT LEAST one recipient.

        The rate limit is checked once per event, not per recipient: the window
        is about event frequency, and a second recipient must not eat up the
        first one's window. A failure for one recipient does not cancel the others.
        """
        if not self.enabled:
            log.info("Telegram not configured, event goes to the DB only: %s", text)
            return False
        if not self._allowed(key):
            log.debug("rate limit for '%s', skipping", key)
            return False

        file_id: str | None = None
        delivered = 0
        for n, chat in enumerate(self.chats, 1):
            try:
                if not photo:          # None or empty bytes: send as text
                    r = self._post_text(chat, text)
                elif file_id:
                    r = self._post_photo_id(chat, text, file_id)
                else:
                    r = self._post_photo_bytes(chat, text, photo)
            except Exception as e:
                # Print NEITHER the URL (it holds the token) NOR the chat_id
                log.warning("Telegram unreachable for recipient #%d: %s", n, type(e).__name__)
                continue
            if r.status_code != 200:
                log.warning("Telegram answered %s to recipient #%d", r.status_code, n)
                continue
            delivered += 1
            if photo and file_id is None:
                file_id = _file_id(r)

        if delivered < len(self.chats):
            log.warning("delivered to %d of %d recipients", delivered, len(self.chats))
        return delivered > 0


def _why(resp: httpx.Response) -> str:
    """Telegram's answer as a human-readable reason. description holds no secrets."""
    try:
        desc = (resp.json() or {}).get("description", "")
    except Exception:
        desc = ""
    low = desc.lower()
    if "chat not found" in low:
        return ("the recipient has not sent /start to the bot yet; Telegram does "
                "not let a bot write first")
    if "blocked" in low:
        return "the recipient has blocked the bot"
    if "not a member" in low or "kicked" in low:
        return "the bot is not a member of this group"
    if "chat_id is empty" in low or resp.status_code == 400:
        return f"Telegram rejected the recipient: {desc or 'invalid chat_id'}"
    return f"Telegram answered {resp.status_code}: {desc or 'no explanation'}"


def check(token: str, chat_id: str | Iterable[str], timeout: float = 20.0) -> dict:
    """Telegram check for the setup page.

    Why send() is not reused: it answers with one bool for the whole broadcast,
    so "works" with two recipients out of three would look the same as three
    out of three. Also its rate-limit window is taken even when nothing was
    delivered (see _allowed), so a second "Check" press within 5 minutes would
    silently send nothing.

    Only recipient ordinal numbers and reasons in words are returned: neither
    the token nor the chat_id appears here, just as in this module's logs.
    """
    out: dict = {"token_ok": False, "bot": None, "error": None, "recipients": []}
    chats = parse_chats(chat_id)
    if not token:
        out["error"] = "bot token is not set"
        return out
    try:
        r = httpx.get(f"{API}/bot{token}/getMe", timeout=timeout)
    except Exception as e:
        # The httpx exception text contains the URL, and the URL contains the token.
        out["error"] = f"Telegram unreachable ({type(e).__name__})"
        return out
    if r.status_code == 401:
        out["error"] = "Telegram rejected the token (401): it is incomplete or revoked"
        return out
    if r.status_code != 200:
        out["error"] = f"Telegram answered {r.status_code} to getMe"
        return out
    out["token_ok"] = True
    try:
        out["bot"] = "@" + (r.json().get("result") or {}).get("username", "")
    except Exception:
        out["bot"] = None
    if not chats:
        out["error"] = "no recipients set: nobody to send to"
        return out

    probe = Telegram(token, chats, rate_limit_sec=0)
    for n, chat in enumerate(chats, 1):
        item = {"n": n, "ok": False, "reason": ""}
        try:
            resp = probe._post_text(
                chat, "pawsight: connection test. If you can see this, notifications will arrive.")
        except Exception as e:
            item["reason"] = f"network unreachable ({type(e).__name__})"
            log.warning("Telegram check: recipient #%d unreachable: %s", n, type(e).__name__)
            out["recipients"].append(item)
            continue
        if resp.status_code == 200:
            item["ok"] = True
            item["reason"] = "delivered"
        else:
            item["reason"] = _why(resp)
            log.warning("Telegram check: not delivered to recipient #%d (%s)",

                        n, resp.status_code)
        out["recipients"].append(item)
    return out
