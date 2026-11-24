"""Bot commands: who is allowed, what gets collapsed, what the bot stays silent about.

There is no network here at all: the whole Telegram layer (a subclass with
stubs instead of HTTP) and the frame source are replaced. This checks the loop
logic, not the transport; the transport is covered in test_notify.py.
"""
from __future__ import annotations

import unittest

from pawsight.bot import HELP_TEXT, CommandBot
from pawsight.notify import Telegram

OWNER = "111"
STRANGER = "999"
JPEG = b"\xff\xd8kadr"


class FakeTelegram(Telegram):
    """The real Telegram, minus HTTP: subclassing catches signature drift."""

    def __init__(self, chats: str = OWNER, batches: list | None = None) -> None:
        super().__init__("SECRET-TOKEN", chats)
        self.batches = list(batches or [])
        self.sent: list[tuple[str, str, bytes | None]] = []
        self.actions: list[tuple[str, str]] = []
        self.polls: list[tuple[int | None, int]] = []

    def get_updates(self, offset, timeout: int = 25) -> list[dict]:
        self.polls.append((offset, timeout))
        return self.batches.pop(0) if self.batches else []

    def reply(self, chat: str, text: str, photo: bytes | None = None) -> bool:
        self.sent.append((chat, text, photo))
        return True

    def action(self, chat: str, name: str = "upload_photo") -> None:
        self.actions.append((chat, name))


def msg(uid: int, text: str, chat: str = OWNER) -> dict:
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


def bot(tg: FakeTelegram, snapshot=None) -> CommandBot:
    return CommandBot(tg, snapshot or (lambda: (JPEG, None)))


class Access(unittest.TestCase):
    def test_stranger_gets_no_frame_and_no_answer(self):
        """Silence, not "access denied": an answer would confirm to a stranger
        that the bot is alive and someone's home is behind it."""
        taken = []
        tg = FakeTelegram(batches=[[msg(1, "/snap", chat=STRANGER)]])
        bot(tg, lambda: taken.append(1) or (JPEG, None)).poll_once()
        self.assertEqual(taken, [])
        self.assertEqual(tg.sent, [])

    def test_owner_gets_the_frame(self):
        tg = FakeTelegram(batches=[[msg(1, "/snap")]])
        bot(tg).poll_once()
        self.assertEqual(len(tg.sent), 1)
        chat, _, photo = tg.sent[0]
        self.assertEqual(chat, OWNER)
        self.assertEqual(photo, JPEG)

    def test_answer_goes_only_to_the_asker(self):
        """The second recipient subscribed to the dog's events, not to others' requests."""
        tg = FakeTelegram(chats="111,222", batches=[[msg(1, "/snap", chat="222")]])
        bot(tg).poll_once()
        self.assertEqual([c for c, _, _ in tg.sent], ["222"])

    def test_no_recipients_means_bot_does_not_start(self):
        """An empty TELEGRAM_CHAT_ID means "nobody allowed", not "let everyone in"."""
        self.assertFalse(bot(FakeTelegram(chats="")).start())

    def test_no_token_means_bot_does_not_start(self):
        tg = FakeTelegram()
        tg._token = ""
        self.assertFalse(bot(tg).start())


class Commands(unittest.TestCase):
    def test_aliases_all_give_a_frame(self):
        for text in ("/snap", "/frame", "/photo", "frame", "Photo", "  snapshot  "):
            tg = FakeTelegram(batches=[[msg(1, text)]])
            bot(tg).poll_once()
            self.assertEqual([p for _, _, p in tg.sent], [JPEG], text)

    def test_group_form_with_bot_name(self):
        """In a group Telegram rewrites the command as /snap@bot_name."""
        tg = FakeTelegram(batches=[[msg(1, "/snap@dog_cam_bot")]])
        bot(tg).poll_once()
        self.assertEqual([p for _, _, p in tg.sent], [JPEG])

    def test_help_does_not_touch_the_camera(self):
        taken = []
        tg = FakeTelegram(batches=[[msg(1, "/help")]])
        bot(tg, lambda: taken.append(1) or (JPEG, None)).poll_once()
        self.assertEqual(taken, [])
        self.assertEqual(tg.sent, [(OWNER, HELP_TEXT, None)])

    def test_unknown_command_gets_a_hint(self):
        tg = FakeTelegram(batches=[[msg(1, "/walkies")]])
        bot(tg).poll_once()
        self.assertEqual(len(tg.sent), 1)
        self.assertIn("/snap", tg.sent[0][1])

    def test_plain_talk_is_ignored(self):
        """People may write anything to the bot; answering everything is spam."""
        tg = FakeTelegram(batches=[[msg(1, "good night"), msg(2, "")]])
        bot(tg).poll_once()
        self.assertEqual(tg.sent, [])

    def test_shows_upload_indicator_before_the_frame(self):
        tg = FakeTelegram(batches=[[msg(1, "/snap")]])
        bot(tg).poll_once()
        self.assertEqual(tg.actions, [(OWNER, "upload_photo")])


class Batch(unittest.TestCase):
    def test_offset_moves_past_the_last_update(self):
        """Without moving the offset Telegram returns the same messages again,
        and the bot answers them over and over."""
        tg = FakeTelegram(batches=[[msg(7, "/snap"), msg(8, "/help")], []])
        b = bot(tg)
        b.poll_once()
        b.poll_once()
        self.assertEqual(tg.polls[1][0], 9)

    def test_offset_moves_even_for_ignored_messages(self):
        """Otherwise a stranger's message gets stuck in the queue forever."""
        tg = FakeTelegram(batches=[[msg(5, "/snap", chat=STRANGER)], []])
        b = bot(tg)
        b.poll_once()
        b.poll_once()
        self.assertEqual(tg.polls[1][0], 6)

    def test_ten_snaps_in_one_batch_give_one_frame(self):
        """Pressed ten times (or the bot was down for half an hour): the person
        wants one fresh frame, not ten identical ones in a row."""
        tg = FakeTelegram(batches=[[msg(i, "/snap") for i in range(10)]])
        self.assertEqual(bot(tg).poll_once(), 1)
        self.assertEqual(len(tg.sent), 1)

    def test_different_commands_in_one_batch_all_answered(self):
        """Repeats are collapsed, different requests are not."""
        tg = FakeTelegram(batches=[[msg(1, "/help"), msg(2, "/snap")]])
        self.assertEqual(bot(tg).poll_once(), 2)
        self.assertEqual(len(tg.sent), 2)

    def test_same_command_from_two_chats_answered_twice(self):
        tg = FakeTelegram(chats="111,222",
                          batches=[[msg(1, "/snap", "111"), msg(2, "/snap", "222")]])
        bot(tg).poll_once()
        self.assertEqual(sorted(c for c, _, _ in tg.sent), ["111", "222"])

    def test_backlog_is_dropped_at_start(self):
        """A morning "/snap" must not get an evening frame: "right now" cannot
        be retroactive."""
        tg = FakeTelegram(batches=[[msg(41, "/snap")], []])
        b = bot(tg)
        b._drop_backlog()
        self.assertEqual(tg.polls[0], (-1, 0))
        self.assertEqual(tg.sent, [])
        b.poll_once()
        self.assertEqual(tg.polls[1][0], 42)

    def test_empty_backlog_leaves_offset_unset(self):
        tg = FakeTelegram()
        b = bot(tg)
        b._drop_backlog()
        self.assertIsNone(b._offset)

    def test_broken_backlog_does_not_stop_the_bot(self):
        """Stumbling at the start means not starting at all, silently."""
        tg = FakeTelegram(batches=[[{"no": "update_id"}], [msg(1, "/snap")]])
        b = bot(tg)
        with self.assertLogs("pawsight.bot", level="ERROR"):
            b._drop_backlog()
        b.poll_once()
        self.assertEqual([p for _, _, p in tg.sent], [JPEG])

    def test_broken_update_does_not_stop_the_batch(self):
        tg = FakeTelegram(batches=[[{"no": "update_id"}, msg(3, "/snap")]])
        bot(tg).poll_once()
        self.assertEqual([p for _, _, p in tg.sent], [JPEG])


class NoFrame(unittest.TestCase):
    def test_reason_is_told_not_swallowed(self):
        tg = FakeTelegram(batches=[[msg(1, "/snap")]])
        bot(tg, lambda: (None, "the detector did not answer within 6 s")).poll_once()
        chat, text, photo = tg.sent[0]
        self.assertIsNone(photo)
        self.assertIn("the detector did not answer", text)

    def test_stale_frame_comes_with_a_warning(self):
        """The frame is sent, but a stalling stream must not pass for "now"."""
        tg = FakeTelegram(batches=[[msg(1, "/snap")]])
        bot(tg, lambda: (JPEG, "frame is 9 s old: the camera stream is stalling")).poll_once()
        _, caption, photo = tg.sent[0]
        self.assertEqual(photo, JPEG)
        self.assertIn("9 s", caption)

    def test_source_blowing_up_does_not_kill_the_loop(self):
        def boom():
            raise RuntimeError("cv2 crashed")

        tg = FakeTelegram(batches=[[msg(1, "/snap")]])
        b = bot(tg, boom)
        with self.assertLogs("pawsight.bot", level="ERROR"):
            b.poll_once()          # must not raise
        self.assertEqual(tg.sent, [])

    def test_nothing_secret_in_logs_about_strangers(self):
        """The chat_id identifies a person: it never reaches the log, nor does the token."""

        tg = FakeTelegram(batches=[[msg(1, "/snap", chat=STRANGER)]])
        with self.assertLogs("pawsight.bot", level="INFO") as logs:
            bot(tg).poll_once()
        joined = "\n".join(logs.output)
        self.assertNotIn(STRANGER, joined)
        self.assertNotIn("SECRET-TOKEN", joined)


if __name__ == "__main__":
    unittest.main()
