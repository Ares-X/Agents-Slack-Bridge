"""Unit tests: thread_reply classify / drop / parent / reply-in-thread (no live Slack)."""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bridge  # noqa: E402
import bot_ts_cache  # noqa: E402


def _load_consumer():
    import importlib.util
    path = os.path.join(ROOT, "consumer", "poll_consumer.py")
    spec = importlib.util.spec_from_file_location("poll_consumer_tr", path)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(mod)
    return mod


class TestClassifyInboundKind(unittest.TestCase):
    def test_app_mention(self):
        self.assertEqual(
            bridge.classify_inbound_kind(
                {"type": "app_mention", "text": "hi", "ts": "1.0"},
                "events_api",
            ),
            "mention",
        )

    def test_dm(self):
        self.assertEqual(
            bridge.classify_inbound_kind(
                {
                    "type": "message",
                    "channel_type": "im",
                    "user": "U1",
                    "text": "hi",
                    "ts": "1.0",
                },
                "events_api",
            ),
            "dm",
        )

    def test_channel_thread_reply_candidate(self):
        self.assertEqual(
            bridge.classify_inbound_kind(
                {
                    "type": "message",
                    "channel_type": "channel",
                    "thread_ts": "10.0",
                    "ts": "11.0",
                    "user": "U1",
                    "text": "follow-up",
                },
                "events_api",
            ),
            "thread_reply",
        )

    def test_group_thread_reply_candidate(self):
        self.assertEqual(
            bridge.classify_inbound_kind(
                {
                    "type": "message",
                    "channel_type": "group",
                    "thread_ts": "10.0",
                    "ts": "11.0",
                    "user": "U1",
                    "text": "follow-up",
                },
                "events_api",
            ),
            "thread_reply",
        )

    def test_channel_top_level_ignored(self):
        self.assertIsNone(
            bridge.classify_inbound_kind(
                {
                    "type": "message",
                    "channel_type": "channel",
                    "ts": "10.0",
                    "user": "U1",
                    "text": "noise",
                },
                "events_api",
            )
        )

    def test_thread_ts_equals_ts_not_reply(self):
        # Some payloads set thread_ts == ts on parent; not a reply candidate.
        self.assertIsNone(
            bridge.classify_inbound_kind(
                {
                    "type": "message",
                    "channel_type": "channel",
                    "thread_ts": "10.0",
                    "ts": "10.0",
                    "user": "U1",
                    "text": "parent-ish",
                },
                "events_api",
            )
        )


class TestDropSubtypes(unittest.TestCase):
    def test_drop_bot_message_and_edits(self):
        self.assertTrue(bridge.should_drop_message_event({
            "subtype": "bot_message", "bot_id": "B1", "text": "x",
        }))
        self.assertTrue(bridge.should_drop_message_event({
            "subtype": "message_changed", "user": "U1",
        }))
        self.assertTrue(bridge.should_drop_message_event({
            "subtype": "message_deleted",
        }))

    def test_keep_plain_user(self):
        self.assertFalse(bridge.should_drop_message_event({
            "user": "U1", "text": "hi", "ts": "1.0",
        }))


class TestParentIsMe(unittest.TestCase):
    def test_parent_user_match(self):
        self.assertTrue(bridge.parent_message_is_me({"user": "UBOT"}, "UBOT"))
        self.assertFalse(bridge.parent_message_is_me({"user": "UOTHER"}, "UBOT"))
        self.assertFalse(bridge.parent_message_is_me(None, "UBOT"))
        self.assertFalse(bridge.parent_message_is_me({"user": "UBOT"}, ""))


class TestOurThreadParentCacheAndApi(unittest.TestCase):
    def test_cache_hit_no_api(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            bot_ts_cache.remember("C1", "10.0", path=path)
            web = mock.Mock()
            ok, err = bridge.is_our_thread_parent(
                web, "C1", "10.0", "UBOT", cache_path=path
            )
            self.assertTrue(ok)
            self.assertIsNone(err)
            web.conversations_replies.assert_not_called()

    def test_api_parent_is_me_caches(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            web = mock.Mock()
            web.conversations_replies.return_value = {
                "messages": [{"user": "UBOT", "ts": "10.0", "text": "hi"}]
            }
            ok, err = bridge.is_our_thread_parent(
                web, "C1", "10.0", "UBOT", cache_path=path
            )
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertTrue(bot_ts_cache.contains("C1", "10.0", path=path))

    def test_api_parent_not_me(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            web = mock.Mock()
            web.conversations_replies.return_value = {
                "messages": [{"user": "UOTHER", "ts": "10.0"}]
            }
            ok, err = bridge.is_our_thread_parent(
                web, "C1", "10.0", "UBOT", cache_path=path
            )
            self.assertFalse(ok)
            self.assertIsNone(err)
            self.assertFalse(bot_ts_cache.contains("C1", "10.0", path=path))

    def test_api_failure_returns_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "cache.json")
            web = mock.Mock()
            web.conversations_replies.side_effect = RuntimeError("network")
            ok, err = bridge.is_our_thread_parent(
                web, "C1", "10.0", "UBOT", cache_path=path
            )
            self.assertFalse(ok)
            self.assertIsInstance(err, RuntimeError)


class TestShouldReplyInThread(unittest.TestCase):
    def test_bridge_helper(self):
        self.assertTrue(bridge.should_reply_in_thread(
            {"kind": "thread_reply"}, False))
        self.assertFalse(bridge.should_reply_in_thread(
            {"kind": "mention"}, False))
        self.assertTrue(bridge.should_reply_in_thread(
            {"kind": "mention"}, True))

    def test_consumer_helper(self):
        pc = _load_consumer()
        self.assertTrue(pc.should_reply_in_thread(
            {"kind": "thread_reply"}, False))
        self.assertFalse(pc.should_reply_in_thread(
            {"kind": "dm"}, False))
        self.assertTrue(pc.should_reply_in_thread(
            {"kind": "dm"}, True))


class TestPendingConsumeForcesThread(unittest.TestCase):
    def test_thread_reply_forces_thread_ts(self):
        import pending_consume_once as pco
        from inbox_store import append_record, peek_undelivered

        with tempfile.TemporaryDirectory() as td:
            inbox = os.path.join(td, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C1",
                "ts": "11.0",
                "thread_ts": "10.0",
                "kind": "thread_reply",
                "user": "U1",
                "text": "follow",
                "delivered": False,
                "reply_status": None,
            })
            seen = {}

            def runner(*cmd, input_text=None):
                seen["cmd"] = cmd
                seen["text"] = input_text
                return type("R", (), {
                    "returncode": 0,
                    "stdout": "sent ok: True ts: 12.0",
                    "stderr": "",
                })()

            r = pco.consume_one(
                inbox, "C1", "11.0", "answer",
                reply_in_thread=False,  # env-style off
                root=ROOT, runner=runner,
            )
            self.assertIn(r.get("outcome"), ("sent_acked", "sent_ack_pending"))
            self.assertIn("--thread-ts", seen["cmd"])
            idx = list(seen["cmd"]).index("--thread-ts")
            self.assertEqual(seen["cmd"][idx + 1], "10.0")


if __name__ == "__main__":
    unittest.main()
