"""Consumer send/ack split, reply placement, history degrade — no live Slack."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Import bridge helpers
import bridge  # noqa: E402

# Import consumer module under a controlled path
CONSUMER = os.path.join(ROOT, "consumer", "poll_consumer.py")


def _load_consumer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("poll_consumer", CONSUMER)
    mod = importlib.util.module_from_spec(spec)
    # Ensure ROOT on path before exec
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(mod)
    return mod


class TestDMSubtypes(unittest.TestCase):
    def test_drop_message_changed_and_deleted(self):
        self.assertTrue(bridge.should_drop_dm_event({
            "subtype": "message_changed",
            "message": {"text": "x", "user": "U1"},
        }))
        self.assertTrue(bridge.should_drop_dm_event({
            "subtype": "message_deleted",
            "deleted_ts": "1.0",
        }))

    def test_drop_missing_user_and_bot(self):
        self.assertTrue(bridge.should_drop_dm_event({
            "text": "",
            "ts": "1.0",
        }))

    def test_keep_plain_dm(self):
        self.assertFalse(bridge.should_drop_dm_event({
            "user": "U1",
            "text": "hello",
            "ts": "1.0",
        }))

    def test_keep_bot_message_subtype(self):
        # bot_message is not in DM_DROP_SUBTYPES; whitelist handled elsewhere
        self.assertFalse(bridge.should_drop_dm_event({
            "subtype": "bot_message",
            "bot_id": "B1",
            "text": "hi",
        }))


class TestThreadTarget(unittest.TestCase):
    def test_uses_thread_ts_when_present(self):
        pc = _load_consumer()
        self.assertEqual(
            pc.thread_target({"thread_ts": "1.0", "ts": "2.0"}),
            "1.0",
        )

    def test_falls_back_to_message_ts(self):
        pc = _load_consumer()
        self.assertEqual(
            pc.thread_target({"thread_ts": "", "ts": "2.0"}),
            "2.0",
        )
        self.assertEqual(
            pc.thread_target({"ts": "3.0"}),
            "3.0",
        )


class TestClassifySend(unittest.TestCase):
    def test_ok_fail_uncertain(self):
        pc = _load_consumer()

        def fake(rc, stdout="", stderr=""):
            return types.SimpleNamespace(
                returncode=rc, stdout=stdout, stderr=stderr)

        self.assertEqual(
            pc.classify_send_result(fake(0, "sent ok: True ts: 1.0")), "ok")
        self.assertEqual(
            pc.classify_send_result(fake(1, "", "boom")), "fail")
        self.assertEqual(
            pc.classify_send_result(fake(0, "sent ok: False")), "fail")
        self.assertEqual(
            pc.classify_send_result(fake(1, "sent ok: True")), "uncertain")
        self.assertEqual(
            pc.classify_send_result(fake(0, "weird")), "uncertain")


class TestHandleOneSendAckSplit(unittest.TestCase):
    def setUp(self):
        self.pc = _load_consumer()
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.pc.INBOX_PATH = self.inbox
        from inbox_store import append_record
        append_record(self.inbox, {
            "channel": "C1",
            "ts": "10.1",
            "text": "ping",
            "user": "Uother",
            "user_name": "alice",
            "channel_name": "general",
            "delivered": False,
            "reply_status": None,
            "thread_ts": "",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_ack_failure_after_send_does_not_resend(self):
        pc = self.pc
        send_calls = []

        def fake_sh(*args, input_text=None):
            cmd = list(args)
            # send.py
            if "send.py" in cmd:
                send_calls.append(input_text)
                return types.SimpleNamespace(
                    returncode=0, stdout="sent ok: True ts: 99.0", stderr="")
            if "channel_history.py" in cmd:
                return types.SimpleNamespace(
                    returncode=0, stdout="", stderr="")
            if "inbox_peek.py" in cmd:
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        # First round: send ok, but ack fails
        with mock.patch.object(pc, "sh", side_effect=fake_sh), \
             mock.patch.object(pc, "channel_history", return_value=([], None)), \
             mock.patch.object(pc, "_ack_message", return_value=False), \
             mock.patch.object(pc, "ME", "Ume"):
            from inbox_store import peek_undelivered
            m = peek_undelivered(self.inbox)[0]
            sessions = {}
            pc.handle_one(m, sessions)
            self.assertEqual(len(send_calls), 1)
            m2 = peek_undelivered(self.inbox)[0]
            self.assertEqual(m2.get("reply_status"), "sent")
            self.assertFalse(m2.get("delivered"))

            # Second round: should NOT send again
            pc.handle_one(m2, sessions)
            self.assertEqual(len(send_calls), 1)  # still 1

    def test_uncertain_does_not_resend(self):
        pc = self.pc

        def fake_sh(*args, input_text=None):
            if "send.py" in args:
                return types.SimpleNamespace(
                    returncode=0, stdout="weird partial", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(pc, "sh", side_effect=fake_sh), \
             mock.patch.object(pc, "channel_history", return_value=([], None)), \
             mock.patch.object(pc, "ME", "Ume"):
            from inbox_store import peek_undelivered
            m = peek_undelivered(self.inbox)[0]
            pc.handle_one(m, {})
            m2 = peek_undelivered(self.inbox)[0]
            self.assertEqual(m2.get("reply_status"), "uncertain")
            # Second handle: still no send (sh send not called again meaningfully)
            send_count = {"n": 0}

            def fake_sh2(*args, input_text=None):
                if "send.py" in args:
                    send_count["n"] += 1
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(pc, "sh", side_effect=fake_sh2):
                pc.handle_one(m2, {})
            self.assertEqual(send_count["n"], 0)

    def test_reply_in_thread_uses_message_ts(self):
        pc = self.pc
        pc.REPLY_IN_THREAD = True
        seen = {}

        def fake_sh(*args, input_text=None):
            if "send.py" in args:
                seen["cmd"] = list(args)
                return types.SimpleNamespace(
                    returncode=0, stdout="sent ok: True ts: 1", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(pc, "sh", side_effect=fake_sh), \
             mock.patch.object(pc, "channel_history", return_value=([], None)), \
             mock.patch.object(pc, "_ack_message", return_value=True), \
             mock.patch.object(pc, "ME", "Ume"):
            from inbox_store import peek_undelivered
            m = peek_undelivered(self.inbox)[0]
            # no thread_ts → should use ts
            pc.handle_one(m, {})
            self.assertIn("--thread-ts", seen["cmd"])
            idx = seen["cmd"].index("--thread-ts")
            self.assertEqual(seen["cmd"][idx + 1], "10.1")

    def test_reply_top_level_default(self):
        pc = self.pc
        pc.REPLY_IN_THREAD = False
        seen = {}

        def fake_sh(*args, input_text=None):
            if "send.py" in args:
                seen["cmd"] = list(args)
                return types.SimpleNamespace(
                    returncode=0, stdout="sent ok: True ts: 1", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(pc, "sh", side_effect=fake_sh), \
             mock.patch.object(pc, "channel_history", return_value=([], None)), \
             mock.patch.object(pc, "_ack_message", return_value=True), \
             mock.patch.object(pc, "ME", "Ume"):
            from inbox_store import peek_undelivered
            m = peek_undelivered(self.inbox)[0]
            pc.handle_one(m, {})
            self.assertNotIn("--thread-ts", seen["cmd"])


class TestHistoryDegrade(unittest.TestCase):
    def test_channel_history_preserves_error(self):
        pc = _load_consumer()

        def fake_sh(*args, input_text=None):
            return types.SimpleNamespace(
                returncode=1,
                stdout=json.dumps({"error": "history fetch failed: boom"}),
                stderr="",
            )

        with mock.patch.object(pc, "sh", side_effect=fake_sh):
            msgs, err = pc.channel_history("C1")
        self.assertEqual(msgs, [])
        self.assertIsNotNone(err)
        self.assertIn("history", err.lower())

    def test_generate_reply_does_not_claim_context_on_error(self):
        pc = _load_consumer()
        reply = pc.generate_reply(
            "C1",
            {"text": "请结合聊天记录回答", "user_name": "a"},
            [],
            [],
            history_error="history fetch failed",
        )
        self.assertNotIn("我会在被 @ 时先拉本频道最近消息再回", reply)
        self.assertIn("失败", reply)

    def test_generate_reply_context_claim_only_when_ok(self):
        pc = _load_consumer()
        reply = pc.generate_reply(
            "C1",
            {"text": "hello world", "user_name": "a"},
            [],
            [{"user": "Ux", "user_name": "bob", "text": "prior note"}],
            history_error=None,
        )
        self.assertIn("看到了频道上下文", reply)


class TestBridgeAckOrderHelpers(unittest.TestCase):
    """Document durable-first: append_inbox is the enqueue primitive used before ack."""

    def test_append_inbox_delegates_to_store(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            old = bridge.INBOX_PATH
            bridge.INBOX_PATH = path
            try:
                ok = bridge.append_inbox({
                    "channel": "C", "ts": "1.0", "text": "x",
                    "delivered": False,
                })
                self.assertTrue(ok)
                ok2 = bridge.append_inbox({
                    "channel": "C", "ts": "1.0", "text": "dup",
                    "delivered": False,
                })
                self.assertFalse(ok2)
            finally:
                bridge.INBOX_PATH = old


if __name__ == "__main__":
    unittest.main()
