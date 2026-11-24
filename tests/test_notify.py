"""Telegram broadcast: recipient list, file_id reuse.

HTTP is replaced entirely: the tests do not touch the network and need no token.
"""
from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from pawsight.notify import Telegram, check, parse_chats


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def photo_ok(file_id: str = "FID") -> FakeResponse:
    """Telegram's sendPhoto response: several sizes, the largest one last."""
    return FakeResponse(200, {"ok": True, "result": {"photo": [
        {"file_id": "small", "file_size": 100},
        {"file_id": file_id, "file_size": 9000},
    ]}})


class ParseChats(unittest.TestCase):
    def test_single_id_stays_single(self):
        self.assertEqual(parse_chats("12345"), ["12345"])

    def test_comma_separated(self):
        self.assertEqual(parse_chats("111,222"), ["111", "222"])

    def test_spaces_and_empties_dropped(self):
        self.assertEqual(parse_chats("  111 , ,222 ,"), ["111", "222"])

    def test_empty_string(self):
        self.assertEqual(parse_chats(""), [])
        self.assertEqual(parse_chats("   "), [])
        self.assertEqual(parse_chats(None), [])

    def test_accepts_list(self):
        self.assertEqual(parse_chats(["111", " 222 ", ""]), ["111", "222"])

    def test_negative_group_id_survives(self):
        self.assertEqual(parse_chats("-1001234567890"), ["-1001234567890"])


class Enabled(unittest.TestCase):
    def test_no_token(self):
        self.assertFalse(Telegram("", "111").enabled)

    def test_no_chats(self):
        self.assertFalse(Telegram("tok", "").enabled)

    def test_ready(self):
        self.assertTrue(Telegram("tok", "111,222").enabled)


class TextFanout(unittest.TestCase):
    def test_text_goes_to_every_recipient(self):
        tg = Telegram("tok", "111,222")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            self.assertTrue(tg.send("hello"))
        self.assertEqual(post.call_count, 2)
        chats = [c.kwargs["data"]["chat_id"] for c in post.call_args_list]
        self.assertEqual(chats, ["111", "222"])
        for c in post.call_args_list:
            self.assertTrue(c.args[0].endswith("/sendMessage"))


class PhotoFanout(unittest.TestCase):
    def test_photo_uploaded_once_then_reused_by_file_id(self):
        tg = Telegram("tok", "111,222,333")
        with patch("pawsight.notify.httpx.post", return_value=photo_ok("FID")) as post:
            self.assertTrue(tg.send("caption", photo=b"\xff\xd8jpegbytes"))
        self.assertEqual(post.call_count, 3)

        first, second, third = post.call_args_list
        # the first one gets the bytes
        self.assertIn("files", first.kwargs)
        self.assertNotIn("photo", first.kwargs["data"])
        # the rest get the file_id, no re-upload
        for call in (second, third):
            self.assertNotIn("files", call.kwargs)
            self.assertEqual(call.kwargs["data"]["photo"], "FID")

    def test_largest_size_is_taken(self):
        # The largest one is IN THE MIDDLE: otherwise max() and sizes[0] (or
        # sizes[-1]) give the same answer, and the test could not tell "took the
        # maximum" from "took whichever came first".
        tg = Telegram("tok", "111,222")
        resp = FakeResponse(200, {"result": {"photo": [
            {"file_id": "tiny", "file_size": 10},
            {"file_id": "big", "file_size": 9000},
            {"file_id": "mid", "file_size": 500},
        ]}})
        with patch("pawsight.notify.httpx.post", return_value=resp) as post:
            tg.send("x", photo=b"j")
        self.assertEqual(post.call_args_list[1].kwargs["data"]["photo"], "big")

    def test_empty_photo_falls_back_to_text(self):
        """An empty JPEG (imencode failed) must not be sent as a picture."""
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            self.assertTrue(tg.send("hello", photo=b""))
        self.assertTrue(post.call_args_list[0].args[0].endswith("/sendMessage"))

    def test_caption_trimmed_to_telegram_limit(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", return_value=photo_ok()) as post:
            tg.send("z" * 2000, photo=b"j")
        self.assertEqual(len(post.call_args_list[0].kwargs["data"]["caption"]), 1024)


    def test_file_id_does_not_survive_between_events(self):
        """The file_id cache lives for exactly one event.

        If it were moved onto the object "to avoid re-uploading", the second and
        all later events would go out with the FIRST one's picture: the dog came
        to the bowl once, and the same snapshot is broadcast for a month.
        """
        tg = Telegram("tok", "111,222", rate_limit_sec=0)
        with patch("pawsight.notify.httpx.post", side_effect=[
                photo_ok("FID-1"), photo_ok("FID-1"),      # event 1
                photo_ok("FID-2"), photo_ok("FID-2")]) as post:
            tg.send("event 1", photo=b"kadr-1", key="bowls")
            tg.send("event 2", photo=b"kadr-2", key="bowls")

        self.assertEqual(post.call_count, 4)
        first_of_second_event = post.call_args_list[2]
        # the second event must start by uploading ITS OWN bytes
        self.assertIn("files", first_of_second_event.kwargs)
        self.assertEqual(first_of_second_event.kwargs["files"]["photo"][1], b"kadr-2")
        # and broadcast its own file_id, not the previous one
        self.assertEqual(post.call_args_list[3].kwargs["data"]["photo"], "FID-2")


class PartialFailure(unittest.TestCase):
    def test_one_failure_does_not_stop_the_rest(self):
        tg = Telegram("tok", "111,222")
        with patch("pawsight.notify.httpx.post",
                   side_effect=[FakeResponse(400), FakeResponse(200)]) as post:
            self.assertTrue(tg.send("hello"))
        self.assertEqual(post.call_count, 2)

    def test_exception_on_one_recipient_does_not_stop_the_rest(self):
        tg = Telegram("tok", "111,222")
        with patch("pawsight.notify.httpx.post",
                   side_effect=[RuntimeError("network"), FakeResponse(200)]) as post:
            self.assertTrue(tg.send("hello"))
        self.assertEqual(post.call_count, 2)

    def test_all_failed_returns_false(self):
        tg = Telegram("tok", "111,222")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(403)):
            self.assertFalse(tg.send("hello"))

    def test_second_gets_bytes_when_first_upload_failed(self):
        """Without a successful first upload there is no file_id: send bytes again."""
        tg = Telegram("tok", "111,222")
        with patch("pawsight.notify.httpx.post",
                   side_effect=[FakeResponse(400), photo_ok("FID")]) as post:
            self.assertTrue(tg.send("caption", photo=b"jpeg"))
        self.assertIn("files", post.call_args_list[1].kwargs)

    def test_secrets_never_reach_logs_on_http_error(self):
        # Capture on the ROOT logger, not on pawsight.notify: a third-party
        # library whose records bubble up there could leak too.
        tg = Telegram("SECRET-TOKEN", "111,222")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(403)):
            with self.assertLogs(level="DEBUG") as logs:
                tg.send("hello")
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRET-TOKEN", joined)
        self.assertNotIn("111", joined)
        self.assertNotIn("222", joined)

    def test_secrets_never_reach_logs_on_exception(self):
        """The except branch: an httpx exception's text may contain the full URL."""
        boom = RuntimeError(
            "connect failed: https://api.telegram.org/botSECRET-TOKEN/sendMessage")
        tg = Telegram("SECRET-TOKEN", "111,222")
        with patch("pawsight.notify.httpx.post", side_effect=boom):
            with self.assertLogs(level="DEBUG") as logs:
                self.assertFalse(tg.send("hello"))
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRET-TOKEN", joined)
        self.assertNotIn("api.telegram.org", joined)
        self.assertNotIn("111", joined)


class RateLimit(unittest.TestCase):
    def test_second_send_within_window_makes_no_requests(self):
        tg = Telegram("tok", "111,222", rate_limit_sec=300)
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            tg.send("one", key="bowls")
            post.reset_mock()
            self.assertFalse(tg.send("two", key="bowls"))
        self.assertEqual(post.call_count, 0)

    def test_window_is_per_key_not_per_recipient(self):
        """The second recipient must not eat up the first one's window."""
        tg = Telegram("tok", "111,222", rate_limit_sec=300)
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            tg.send("one", key="bowls")
            tg.send("two", key="bout:drink")
        self.assertEqual(post.call_count, 4)   # two events x two recipients

    def test_window_is_consumed_even_when_nothing_was_delivered(self):
        """The mark is set BEFORE sending: documented behaviour.

        If every recipient failed, the window is still taken for 5 minutes: the
        next event of the same kind will be skipped. This is non-obvious and
        confusing when debugging, so it is pinned by a test, so that the
        behaviour cannot be changed silently.
        """
        tg = Telegram("tok", "111,222", rate_limit_sec=300)
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(403)) as post:
            self.assertFalse(tg.send("one", key="bowls"))
            post.reset_mock()
            self.assertFalse(tg.send("two", key="bowls"))
        self.assertEqual(post.call_count, 0)

    def test_disabled_sender_makes_no_requests(self):
        tg = Telegram("tok", "")
        with patch("pawsight.notify.httpx.post") as post:
            self.assertFalse(tg.send("hello"))
        self.assertEqual(post.call_count, 0)


if __name__ == "__main__":
    unittest.main()


class ReplyToOne(unittest.TestCase):
    """Reply to a user's request: addressed to one chat and without rate limit."""

    def test_goes_only_to_the_asker(self):
        tg = Telegram("tok", "111,222,333")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            self.assertTrue(tg.reply("222", "here is the frame"))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "222")

    def test_photo_goes_as_bytes(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", return_value=photo_ok()) as post:
            self.assertTrue(tg.reply("111", "caption", photo=b"jpeg"))
        self.assertTrue(post.call_args.args[0].endswith("/sendPhoto"))
        self.assertEqual(post.call_args.kwargs["files"]["photo"][1], b"jpeg")

    def test_rate_limit_does_not_apply(self):
        """The window is about the frequency of events the system came up with
        itself. A person asked twice, so answer twice, otherwise the bot looks
        broken exactly when someone is looking at it."""
        tg = Telegram("tok", "111", rate_limit_sec=300)
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            tg.reply("111", "one")
            tg.reply("111", "two")
        self.assertEqual(post.call_count, 2)

    def test_failure_is_reported_not_swallowed(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(400)):
            self.assertFalse(tg.reply("111", "hello"))

    def test_no_token_no_request(self):
        with patch("pawsight.notify.httpx.post") as post:
            self.assertFalse(Telegram("", "111").reply("111", "hello"))
        self.assertEqual(post.call_count, 0)

    def test_secrets_never_reach_logs(self):
        boom = RuntimeError(
            "connect failed: https://api.telegram.org/botSECRET-TOKEN/sendPhoto")
        tg = Telegram("SECRET-TOKEN", "111")
        with patch("pawsight.notify.httpx.post", side_effect=boom):
            with self.assertLogs(level="DEBUG") as logs:
                self.assertFalse(tg.reply("111", "hello"))
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRET-TOKEN", joined)
        self.assertNotIn("api.telegram.org", joined)
        self.assertNotIn("111", joined)


class ChatAction(unittest.TestCase):
    def test_action_is_sent(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            tg.action("111")
        self.assertTrue(post.call_args.args[0].endswith("/sendChatAction"))
        self.assertEqual(post.call_args.kwargs["data"]["action"], "upload_photo")

    def test_failure_is_harmless(self):
        """The indicator is cosmetic; a frame must not get lost because of it."""
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.post", side_effect=RuntimeError("network")):
            tg.action("111")          # silently, no exception


class GetUpdates(unittest.TestCase):
    """Receiving commands: long polling, not a webhook, so no port is opened outside."""

    def test_offset_and_long_poll_are_passed(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.get",
                   return_value=FakeResponse(200, {"result": [{"update_id": 5}]})) as get:
            self.assertEqual(tg.get_updates(4, 25), [{"update_id": 5}])
        params = get.call_args.kwargs["params"]
        self.assertEqual(params["offset"], 4)
        self.assertEqual(params["timeout"], 25)
        self.assertEqual(params["allowed_updates"], '["message"]')

    def test_client_timeout_outlives_the_long_poll(self):
        """An equal timeout would cut EVERY empty poll: Telegram holds the
        connection exactly as long as we asked."""
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.get", return_value=FakeResponse(200, {})) as get:
            tg.get_updates(None, 25)
        self.assertGreater(get.call_args.kwargs["timeout"], 25)

    def test_first_poll_goes_without_offset(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.get", return_value=FakeResponse(200, {})) as get:
            tg.get_updates(None)
        self.assertNotIn("offset", get.call_args.kwargs["params"])

    def test_network_failure_is_an_empty_batch_not_an_exception(self):
        """The bot loop must survive a network outage, not die with it."""
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.get", side_effect=RuntimeError("network")):
            self.assertEqual(tg.get_updates(1), [])

    def test_error_status_is_an_empty_batch(self):
        tg = Telegram("tok", "111")
        with patch("pawsight.notify.httpx.get", return_value=FakeResponse(409)):
            self.assertEqual(tg.get_updates(1), [])

    def test_unparsable_answer_is_an_empty_batch(self):
        tg = Telegram("tok", "111")
        broken = FakeResponse(200)
        broken.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        with patch("pawsight.notify.httpx.get", return_value=broken):
            self.assertEqual(tg.get_updates(1), [])

    def test_no_token_no_request(self):
        with patch("pawsight.notify.httpx.get") as get:
            self.assertEqual(Telegram("", "111").get_updates(None), [])
        self.assertEqual(get.call_count, 0)

    def test_secrets_never_reach_logs(self):
        boom = RuntimeError(
            "read timeout: https://api.telegram.org/botSECRET-TOKEN/getUpdates")
        tg = Telegram("SECRET-TOKEN", "111")
        with patch("pawsight.notify.httpx.get", side_effect=boom):
            with self.assertLogs(level="DEBUG") as logs:
                tg.get_updates(1)
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRET-TOKEN", joined)
        self.assertNotIn("api.telegram.org", joined)


class HttpxLoggingIsMuted(unittest.TestCase):
    """The bot token is in the URL path, and httpx at INFO prints the whole URL.

    The leak is real: it already happened in the container's json log. The
    httpx/httpcore logger level is raised in pawsight.__main__._log(); this test
    keeps it in place, because "remove noise from the logging setup" looks like
    a perfectly harmless edit.
    """

    def test_log_setup_silences_httpx(self):
        import logging as lg

        from pawsight.__main__ import _log

        root = lg.getLogger()
        saved = (root.level, list(root.handlers),
                 lg.getLogger("httpx").level, lg.getLogger("httpcore").level)
        try:
            _log()
            self.assertGreaterEqual(lg.getLogger("httpx").getEffectiveLevel(), lg.WARNING)
            self.assertGreaterEqual(lg.getLogger("httpcore").getEffectiveLevel(), lg.WARNING)
        finally:
            root.setLevel(saved[0])
            root.handlers[:] = saved[1]
            lg.getLogger("httpx").setLevel(saved[2])
            lg.getLogger("httpcore").setLevel(saved[3])


class Check(unittest.TestCase):
    """Settings check from the setup page.

    Separate from send() on purpose: send() answers with one bool for the whole
    broadcast, and "two of three never pressed Start" would look like full
    success there.
    """

    def me(self, name="pawsight_bot"):
        return FakeResponse(200, {"ok": True, "result": {"username": name}})

    def test_no_token_means_no_requests(self):
        with patch("pawsight.notify.httpx.get") as get, patch("pawsight.notify.httpx.post") as post:
            r = check("", "111")
        self.assertFalse(r["token_ok"])
        self.assertEqual((get.call_count, post.call_count), (0, 0))

    def test_bad_token_stops_before_sending(self):
        """No point sending messages with a rejected token."""
        with patch("pawsight.notify.httpx.get", return_value=FakeResponse(401)), \
             patch("pawsight.notify.httpx.post") as post:
            r = check("SECRET-TOKEN", "111,222")
        self.assertFalse(r["token_ok"])
        self.assertIn("401", r["error"])
        self.assertEqual(post.call_count, 0)

    def test_bot_name_is_reported(self):
        with patch("pawsight.notify.httpx.get", return_value=self.me()), \
             patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)):
            r = check("tok", "111")
        self.assertTrue(r["token_ok"])
        self.assertEqual(r["bot"], "@pawsight_bot")

    def test_each_recipient_is_reported_separately(self):
        not_found = FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})
        with patch("pawsight.notify.httpx.get", return_value=self.me()), \
             patch("pawsight.notify.httpx.post", side_effect=[FakeResponse(200), not_found]):
            r = check("tok", "111,222")
        self.assertEqual([x["ok"] for x in r["recipients"]], [True, False])
        self.assertIn("/start", r["recipients"][1]["reason"])

    def test_token_without_recipients_is_named_as_such(self):
        with patch("pawsight.notify.httpx.get", return_value=self.me()), \
             patch("pawsight.notify.httpx.post") as post:
            r = check("tok", "")
        self.assertTrue(r["token_ok"])
        self.assertIn("nobody", r["error"])
        self.assertEqual(post.call_count, 0)

    def test_no_rate_limit_between_two_checks(self):
        """send()'s window is taken even on failure: a second "Check" press
        within five minutes would silently send nothing."""

        with patch("pawsight.notify.httpx.get", return_value=self.me()), \
             patch("pawsight.notify.httpx.post", return_value=FakeResponse(200)) as post:
            check("tok", "111")
            check("tok", "111")
        self.assertEqual(post.call_count, 2)

    def test_network_failure_names_the_type_not_the_url(self):
        boom = RuntimeError("connect failed: https://api.telegram.org/botSECRET-TOKEN/getMe")
        with patch("pawsight.notify.httpx.get", side_effect=boom):
            with self.assertLogs(level="DEBUG") as logs:
                logging.getLogger("pawsight.notify").debug("marker")
                r = check("SECRET-TOKEN", "111")
        self.assertFalse(r["token_ok"])
        self.assertNotIn("SECRET-TOKEN", r["error"])
        self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))

    def test_secrets_never_reach_logs(self):
        with patch("pawsight.notify.httpx.get", return_value=self.me()), \
             patch("pawsight.notify.httpx.post", return_value=FakeResponse(403)):
            with self.assertLogs(level="DEBUG") as logs:
                r = check("SECRET-TOKEN", "111,222")
        joined = "\n".join(logs.output)
        self.assertNotIn("SECRET-TOKEN", joined)
        self.assertNotIn("111", joined)
        self.assertNotIn("SECRET-TOKEN", repr(r))
