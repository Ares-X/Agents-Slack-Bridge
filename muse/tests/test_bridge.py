"""Isolation tests for bridge event classification / filtering.

Run:  python -m unittest discover -s tests -v   (from muse/)
Covers: explicit message-subtype support, own-message filtering,
bot allowlist in both default-allow and whitelist modes.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge

ME = "U_ME"


def base_event(**kw):
    e = {"type": "message", "channel_type": "im", "channel": "D1",
         "user": "U_HUMAN", "text": "hello", "ts": "111.222"}
    e.update(kw)
    return e


class ClassifyTest(unittest.TestCase):
    def test_dm_plain_queued(self):
        action, payload = bridge.process_event(base_event(), ME)
        self.assertEqual(action, "queue")
        self.assertEqual(payload["kind"], "dm")
        self.assertEqual(payload["msg_id"], "D1:111.222")

    def test_dm_message_changed_ignored(self):
        e = base_event(subtype="message_changed",
                       message={"user": "U_HUMAN", "text": "edited"})
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")

    def test_dm_message_deleted_ignored(self):
        e = base_event(subtype="message_deleted")
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")

    def test_dm_channel_join_ignored(self):
        e = base_event(subtype="channel_join")
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")

    def test_dm_me_message_supported(self):
        e = base_event(subtype="me_message", text="/me waves")
        action, payload = bridge.process_event(e, ME)
        self.assertEqual(action, "queue")

    def test_own_message_filtered(self):
        e = base_event(user=ME)
        self.assertEqual(bridge.process_event(e, ME), ("ack_only", "own-message"))

    def test_own_bot_message_filtered(self):
        # own reply posted via chat.postMessage carries bot_id AND user==me
        e = base_event(user=ME, bot_id="B_SELF")
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")

    def test_channel_message_without_mention_ignored(self):
        e = {"type": "message", "channel_type": "channel", "channel": "C1",
             "user": "U_HUMAN", "text": "hi", "ts": "1.1"}
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")

    def test_app_mention_human_queued(self):
        e = {"type": "app_mention", "channel": "C1", "user": "U_HUMAN",
             "text": "<@UBOT> hi", "ts": "2.2"}
        action, payload = bridge.process_event(e, ME)
        self.assertEqual(action, "queue")
        self.assertEqual(payload["kind"], "mention")


class BotAllowlistTest(unittest.TestCase):
    def setUp(self):
        # default-allow mode (as shipped: no .env allowlist in test env)
        bridge.WHITELIST_MODE = False

    def tearDown(self):
        bridge.WHITELIST_MODE = False
        bridge.ALLOWED_BOT_USERS = set()
        bridge.ALLOWED_BOT_IDS = set()

    def bot_mention(self, user="U_BOT", bot_id="B_BOT"):
        return {"type": "app_mention", "channel": "C1", "user": user,
                "bot_id": bot_id, "text": "<@UBOT> hi", "ts": "3.3"}

    def test_default_allow_bot_mention(self):
        action, _ = bridge.process_event(self.bot_mention(), ME)
        self.assertEqual(action, "queue")

    def test_whitelist_mode_blocks_unlisted(self):
        bridge.WHITELIST_MODE = True
        bridge.ALLOWED_BOT_USERS = {"U_FRIEND"}
        bridge.ALLOWED_BOT_IDS = {"B_FRIEND"}
        action, _ = bridge.process_event(self.bot_mention(), ME)
        self.assertEqual(action, "ack_only")

    def test_whitelist_mode_allows_listed_user(self):
        bridge.WHITELIST_MODE = True
        bridge.ALLOWED_BOT_USERS = {"U_BOT"}
        action, _ = bridge.process_event(self.bot_mention(), ME)
        self.assertEqual(action, "queue")

    def test_whitelist_mode_allows_listed_bot_id(self):
        bridge.WHITELIST_MODE = True
        bridge.ALLOWED_BOT_IDS = {"B_BOT"}
        # user field missing (typical for bot events) -> match by bot_id
        e = self.bot_mention(user=None)
        action, _ = bridge.process_event(e, ME)
        self.assertEqual(action, "queue")

    def test_own_message_still_filtered_in_allow_mode(self):
        e = self.bot_mention(user=ME)
        self.assertEqual(bridge.process_event(e, ME)[0], "ack_only")


class RecordTest(unittest.TestCase):
    def test_minimal_record_no_slow_fields(self):
        # hot path must not depend on API-resolved display names
        _, payload = bridge.process_event(base_event(), ME)
        self.assertNotIn("user_name", payload)
        self.assertNotIn("channel_name", payload)
        self.assertEqual(payload["msg_id"], "D1:111.222")
        self.assertIn("thread_ts", payload)


if __name__ == "__main__":
    unittest.main()
