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
    def test_drop_edits_not_bot_message(self):
        # bot_message must NOT early-drop; self-filter + whitelist decide.
        self.assertFalse(bridge.should_drop_message_event({
            "subtype": "bot_message", "bot_id": "B1", "text": "x",
        }))
        self.assertTrue(bridge.should_drop_message_event({
            "subtype": "message_changed", "user": "U1",
        }))
        self.assertTrue(bridge.should_drop_message_event({
            "subtype": "message_deleted",
        }))

    def test_bot_message_not_in_drop_subtypes(self):
        self.assertNotIn("bot_message", bridge.DROP_SUBTYPES)

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
        # In-thread @mention (dual app_mention path) forces thread
        self.assertTrue(bridge.should_reply_in_thread(
            {"kind": "mention", "ts": "11.0", "thread_ts": "10.0"}, False))

    def test_consumer_helper(self):
        pc = _load_consumer()
        self.assertTrue(pc.should_reply_in_thread(
            {"kind": "thread_reply"}, False))
        self.assertFalse(pc.should_reply_in_thread(
            {"kind": "dm"}, False))
        self.assertTrue(pc.should_reply_in_thread(
            {"kind": "dm"}, True))
        self.assertTrue(pc.should_reply_in_thread(
            {"kind": "mention", "ts": "11.0", "thread_ts": "10.0"}, False))


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



class TestBotMessageWhitelistPath(unittest.TestCase):
    """bot_message goes through self-filter + whitelist (not early ACK-drop)."""

    def test_peer_bot_message_not_subtype_dropped(self):
        ev = {
            "subtype": "bot_message",
            "bot_id": "BPEER",
            "user": "UPEER",
            "text": "hello from peer",
            "ts": "1.0",
            "channel": "C1",
        }
        self.assertFalse(bridge.should_drop_message_event(ev))

    def test_self_user_still_identifiable_for_loop_guard(self):
        # Handler drops when event.user == me; subtype alone must not short-circuit.
        ev = {
            "subtype": "bot_message",
            "bot_id": "BOURS",
            "user": "UME",
            "text": "echo",
            "ts": "2.0",
        }
        self.assertFalse(bridge.should_drop_message_event(ev))
        self.assertEqual(ev.get("user"), "UME")


class TestDualAppMentionMessageReplyTarget(unittest.TestCase):
    """Same thread msg as app_mention + message: consistent target, one send."""

    def _run_order(self, first_kind, second_kind):
        import pending_consume_once as pco
        from inbox_store import append_record, peek_undelivered

        with tempfile.TemporaryDirectory() as td:
            inbox = os.path.join(td, "inbox.jsonl")
            # Shared Slack ts for the same physical message
            base = {
                "channel": "C1",
                "ts": "11.0",
                "thread_ts": "10.0",
                "user": "U1",
                "text": "<@UBOT> help",
                "delivered": False,
                "reply_status": None,
            }
            r1 = dict(base, kind=first_kind)
            r2 = dict(base, kind=second_kind)
            self.assertTrue(append_record(inbox, r1))
            self.assertFalse(append_record(inbox, r2))  # dedup
            rows = peek_undelivered(inbox)
            self.assertEqual(len(rows), 1)
            stored = rows[0]
            self.assertEqual(stored["kind"], first_kind)

            sends = []

            def runner(*cmd, input_text=None):
                sends.append({"cmd": cmd, "text": input_text})
                return type("R", (), {
                    "returncode": 0,
                    "stdout": "sent ok: True ts: 12.0",
                    "stderr": "",
                })()

            out = pco.consume_one(
                inbox, "C1", "11.0", "answer",
                reply_in_thread=False,  # env off — thread still forced
                root=ROOT, runner=runner,
            )
            self.assertIn(out.get("outcome"), ("sent_acked", "sent_ack_pending"))
            self.assertEqual(len(sends), 1)
            self.assertIn("--thread-ts", sends[0]["cmd"])
            idx = list(sends[0]["cmd"]).index("--thread-ts")
            self.assertEqual(sends[0]["cmd"][idx + 1], "10.0")
            # Second consume must not resend (already sent/acked)
            rows2 = peek_undelivered(inbox)
            # either empty or not claimable — consume_one on missing raises
            if rows2:
                out2 = pco.consume_one(
                    inbox, "C1", "11.0", "again",
                    reply_in_thread=False, root=ROOT, runner=runner,
                )
                # sent ack-only or skip — no second chat_postMessage body send
                self.assertNotEqual(out2.get("outcome"), "sent_acked")
            self.assertEqual(len(sends), 1)
            return stored

    def test_mention_then_message_replies_in_thread_once(self):
        self._run_order("mention", "thread_reply")

    def test_message_then_mention_replies_in_thread_once(self):
        self._run_order("thread_reply", "mention")

    def test_helpers_agree_both_kinds(self):
        pc = _load_consumer()
        mention = {
            "kind": "mention", "ts": "11.0", "thread_ts": "10.0",
        }
        treply = {
            "kind": "thread_reply", "ts": "11.0", "thread_ts": "10.0",
        }
        self.assertTrue(bridge.should_reply_in_thread(mention, False))
        self.assertTrue(bridge.should_reply_in_thread(treply, False))
        self.assertTrue(pc.should_reply_in_thread(mention, False))
        self.assertTrue(pc.should_reply_in_thread(treply, False))
        self.assertEqual(
            pc.thread_target(mention), pc.thread_target(treply)
        )



if __name__ == "__main__":
    unittest.main()
