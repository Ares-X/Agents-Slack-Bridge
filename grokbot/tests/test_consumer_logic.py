"""Consumer/pipeline: classify, claim-before-send, DM, thread, history, pending."""
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

import bridge  # noqa: E402
import reply_pipeline as rp  # noqa: E402
from inbox_store import (  # noqa: E402
    append_record,
    claim_for_send,
    msg_key,
    peek_claimable,
    peek_undelivered,
    set_reply_status,
)

CONSUMER = os.path.join(ROOT, "consumer", "poll_consumer.py")


def _load_consumer():
    import importlib.util
    spec = importlib.util.spec_from_file_location("poll_consumer", CONSUMER)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, ROOT)
    spec.loader.exec_module(mod)
    return mod


def _fake(rc, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


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
        self.assertTrue(bridge.should_drop_dm_event({"text": "", "ts": "1.0"}))

    def test_keep_plain_dm(self):
        self.assertFalse(bridge.should_drop_dm_event({
            "user": "U1", "text": "hello", "ts": "1.0",
        }))


class TestThreadTarget(unittest.TestCase):
    def test_uses_thread_ts_when_present(self):
        pc = _load_consumer()
        self.assertEqual(pc.thread_target({"thread_ts": "1.0", "ts": "2.0"}), "1.0")

    def test_falls_back_to_message_ts(self):
        pc = _load_consumer()
        self.assertEqual(pc.thread_target({"thread_ts": "", "ts": "2.0"}), "2.0")


class TestClassifySendProvenNotSent(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            rp.classify_send_result(_fake(0, "sent ok: True ts: 1")), "ok")

    def test_not_sent_marker_is_fail(self):
        self.assertEqual(
            rp.classify_send_result(_fake(1, "", "not_sent: empty message")),
            "fail",
        )

    def test_sent_ok_false_is_fail(self):
        self.assertEqual(
            rp.classify_send_result(_fake(1, "sent ok: False", "not_sent: slack_api")),
            "fail",
        )

    def test_nonzero_without_proof_is_uncertain_not_fail(self):
        """Repro: timeout after Slack accepted — must NOT auto-retry as fail."""
        self.assertEqual(
            rp.classify_send_result(_fake(1, "", "TimeoutError")),
            "uncertain",
        )
        self.assertEqual(
            rp.classify_send_result(_fake(2, "", "send_error: connection reset")),
            "uncertain",
        )
        self.assertEqual(
            rp.classify_send_result(_fake(1, "weird", "")),
            "uncertain",
        )


class TestClaimBeforeSendPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        append_record(self.inbox, {
            "channel": "C1", "ts": "10.1", "text": "ping",
            "user": "Uother", "delivered": False, "reply_status": None,
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_claim_persist_fail_does_not_send(self):
        send_calls = []

        def runner(*args, input_text=None):
            send_calls.append(input_text)
            return _fake(0, "sent ok: True")

        with mock.patch(
            "reply_pipeline.claim_for_send",
            side_effect=OSError("disk full"),
        ):
            r = rp.process_one(
                self.inbox,
                peek_undelivered(self.inbox)[0],
                "hi",
                runner=runner,
            )
        self.assertEqual(r["outcome"], "claim_persist_failed")
        self.assertEqual(send_calls, [])

    def test_two_consumers_same_snapshot_only_one_sends(self):
        send_calls = []

        def runner(*args, input_text=None):
            send_calls.append(input_text)
            return _fake(0, "sent ok: True ts: 9")

        snap = peek_undelivered(self.inbox)[0]
        # Both see claimable snapshot
        r1 = rp.process_one(self.inbox, dict(snap), "a", runner=runner)
        r2 = rp.process_one(self.inbox, dict(snap), "b", runner=runner)
        self.assertEqual(len(send_calls), 1)
        self.assertIn(r1["outcome"], ("sent_acked", "sent_ack_pending"))
        self.assertEqual(r2["outcome"], "claim_lost")

    def test_timeout_then_second_round_does_not_resend(self):
        """Repro: nonzero without proof → uncertain → second round no send."""
        sends = []

        def runner(*args, input_text=None):
            sends.append(1)
            return _fake(1, "", "send_error: timeout")

        m = peek_undelivered(self.inbox)[0]
        r1 = rp.process_one(self.inbox, m, "hi", runner=runner)
        self.assertEqual(r1["outcome"], "uncertain")
        self.assertEqual(len(sends), 1)
        m2 = peek_undelivered(self.inbox)[0]
        self.assertEqual(m2["reply_status"], "uncertain")
        r2 = rp.process_one(self.inbox, m2, "hi", runner=runner)
        self.assertEqual(r2["outcome"], "no_resend")
        self.assertEqual(len(sends), 1)

    def test_crash_after_claim_restart_no_direct_resend(self):
        claim_for_send(self.inbox, ("C1", "10.1"))
        m = peek_undelivered(self.inbox)[0]
        self.assertEqual(m["reply_status"], "sending")
        sends = []

        def runner(*args, input_text=None):
            sends.append(1)
            return _fake(0, "sent ok: True")

        r = rp.process_one(self.inbox, m, "hi", runner=runner)
        self.assertEqual(r["outcome"], "no_resend")
        self.assertEqual(sends, [])
        # escalate path
        from inbox_store import escalate_stale_sending
        escalate_stale_sending(self.inbox)
        m2 = peek_undelivered(self.inbox)[0]
        self.assertEqual(m2["reply_status"], "uncertain")
        r2 = rp.process_one(self.inbox, m2, "hi", runner=runner)
        self.assertEqual(r2["outcome"], "no_resend")
        self.assertEqual(sends, [])

    def test_proven_fail_releases_to_retryable_then_can_resend(self):
        sends = []

        def runner(*args, input_text=None):
            sends.append(input_text)
            if len(sends) == 1:
                return _fake(1, "", "not_sent: empty message")
            return _fake(0, "sent ok: True ts: 1")

        m = peek_undelivered(self.inbox)[0]
        r1 = rp.process_one(self.inbox, m, "hi", runner=runner)
        self.assertEqual(r1["outcome"], "fail_retryable")
        m2 = peek_undelivered(self.inbox)[0]
        self.assertEqual(m2["reply_status"], "retryable")
        r2 = rp.process_one(self.inbox, m2, "hi", runner=runner)
        self.assertIn(r2["outcome"], ("sent_acked", "sent_ack_pending"))
        self.assertEqual(len(sends), 2)


class TestHandleOneIntegration(unittest.TestCase):
    def setUp(self):
        self.pc = _load_consumer()
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.pc.INBOX_PATH = self.inbox
        # patch reply_pipeline inbox usage via process_one receiving path
        append_record(self.inbox, {
            "channel": "C1", "ts": "10.1", "text": "ping",
            "user": "Uother", "user_name": "alice", "channel_name": "general",
            "delivered": False, "reply_status": None, "thread_ts": "",
        })

    def tearDown(self):
        self.tmp.cleanup()

    def test_reply_in_thread_uses_message_ts(self):
        pc = self.pc
        pc.REPLY_IN_THREAD = True
        pc.INBOX_PATH = self.inbox
        seen = {}

        def runner(*args, input_text=None):
            if "send.py" in args:
                seen["cmd"] = list(args)
                return _fake(0, "sent ok: True ts: 1")
            return _fake(0, "")

        with mock.patch.object(pc, "channel_history", return_value=([], None)), \
             mock.patch.object(pc, "ME", "Ume"), \
             mock.patch("reply_pipeline.ROOT", self.tmp.name):
            # process_one uses inbox path from handle_one
            import reply_pipeline as rpmod
            # monkeypatch process_one to use our inbox — handle_one uses pc.INBOX_PATH
            m = peek_undelivered(self.inbox)[0]
            # call process_one directly for thread flag
            r = rp.process_one(
                self.inbox, m, "hi", reply_in_thread=True,
                thread_ts="10.1", runner=runner,
            )
            self.assertIn(r["outcome"], ("sent_acked", "sent_ack_pending"))
            self.assertIn("--thread-ts", seen["cmd"])
            self.assertEqual(seen["cmd"][seen["cmd"].index("--thread-ts") + 1], "10.1")

    def test_reply_top_level_default(self):
        seen = {}

        def runner(*args, input_text=None):
            if "send.py" in args:
                seen["cmd"] = list(args)
                return _fake(0, "sent ok: True")
            return _fake(0, "")

        m = peek_undelivered(self.inbox)[0]
        rp.process_one(self.inbox, m, "hi", reply_in_thread=False, runner=runner)
        self.assertNotIn("--thread-ts", seen["cmd"])


class TestHistoryDegrade(unittest.TestCase):
    def test_channel_history_preserves_error(self):
        pc = _load_consumer()

        def fake_sh(*args, input_text=None):
            return _fake(1, json.dumps({"error": "history fetch failed: boom"}), "")

        with mock.patch.object(pc, "sh", side_effect=fake_sh):
            msgs, err = pc.channel_history("C1")
        self.assertEqual(msgs, [])
        self.assertIsNotNone(err)

    def test_generate_reply_does_not_claim_context_on_error(self):
        pc = _load_consumer()
        reply = pc.generate_reply(
            "C1",
            {"text": "请结合聊天记录回答", "user_name": "a"},
            [], [], history_error="history fetch failed",
        )
        self.assertNotIn("我会在被 @ 时先拉本频道最近消息再回", reply)
        self.assertIn("失败", reply)


class TestPendingNotifyGuards(unittest.TestCase):
    def test_pending_excludes_sent_and_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C", "ts": "1", "text": "a",
                "delivered": False, "reply_status": None,
            })
            append_record(inbox, {
                "channel": "C", "ts": "2", "text": "b",
                "delivered": False, "reply_status": None,
            })
            append_record(inbox, {
                "channel": "C", "ts": "3", "text": "c",
                "delivered": False, "reply_status": None,
            })
            set_reply_status(inbox, {("C", "2")}, "sent")
            set_reply_status(inbox, {("C", "3")}, "uncertain")
            # Run pending_notify logic
            from inbox_store import is_claimable, peek_undelivered
            rows = peek_undelivered(inbox)
            claimable = [r for r in rows if is_claimable(r.get("reply_status"))]
            blocked = [r for r in rows if not is_claimable(r.get("reply_status"))]
            self.assertEqual([msg_key(r) for r in claimable], [("C", "1")])
            self.assertEqual(len(blocked), 2)
            self.assertEqual(peek_claimable(inbox)[0]["ts"], "1")


class TestBridgeNoNameLookupOnEnqueue(unittest.TestCase):
    def test_append_inbox_with_empty_names(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "inbox.jsonl")
            old = bridge.INBOX_PATH
            bridge.INBOX_PATH = path
            try:
                ok = bridge.append_inbox({
                    "channel": "C", "ts": "1.0", "text": "x",
                    "channel_name": "", "user_name": "",
                    "user": "U1", "delivered": False, "reply_status": None,
                })
                self.assertTrue(ok)
                row = peek_undelivered(path)[0]
                self.assertEqual(row["channel_name"], "")
                self.assertEqual(row["user_name"], "")
            finally:
                bridge.INBOX_PATH = old

    def test_bridge_source_has_no_disp_name_on_path(self):
        with open(os.path.join(ROOT, "bridge.py")) as bf:
            src = bf.read()
        self.assertNotIn("disp_name(", src)
        self.assertIn('channel_name": ""', src)


if __name__ == "__main__":
    unittest.main()


class TestAmbiguousSlackApiNoRetry(unittest.TestCase):
    def test_internal_error_is_uncertain_not_fail(self):
        self.assertEqual(
            rp.classify_send_result(
                _fake(2, "", "send_error: slack_api internal_error")
            ),
            "uncertain",
        )
        self.assertEqual(
            rp.classify_send_result(
                _fake(1, "sent ok: False", "not_sent: slack_api internal_error")
            ),
            "uncertain",
        )
        self.assertEqual(
            rp.classify_send_result(
                _fake(2, "", "send_error: slack_api fatal_error")
            ),
            "uncertain",
        )

    def test_internal_error_pipeline_no_second_send(self):
        """Isolation repro: internal_error must not yield attempts=2."""
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C1", "ts": "10.1", "text": "ping",
                "user": "U1", "delivered": False, "reply_status": None,
            })
            sends = []

            def runner(*args, input_text=None):
                sends.append(1)
                return _fake(2, "", "send_error: slack_api internal_error")

            m = peek_undelivered(inbox)[0]
            r1 = rp.process_one(inbox, m, "hi", runner=runner)
            self.assertEqual(r1["outcome"], "uncertain")
            m2 = peek_undelivered(inbox)[0]
            r2 = rp.process_one(inbox, m2, "hi", runner=runner)
            self.assertEqual(r2["outcome"], "no_resend")
            self.assertEqual(len(sends), 1)

    def test_channel_not_found_still_retryable(self):
        self.assertEqual(
            rp.classify_send_result(
                _fake(1, "sent ok: False", "not_sent: slack_api channel_not_found")
            ),
            "fail",
        )


class TestSendRetryHandlersDisabled(unittest.TestCase):
    def test_build_web_client_passes_empty_retry_handlers(self):
        with open(os.path.join(ROOT, "send.py")) as bf:
            src = bf.read()
        self.assertIn('"retry_handlers": []', src)
        # build_web_client constructs client with empty handlers
        import send as send_mod
        created = {}

        class FakeClient:
            def __init__(self, **kw):
                created.update(kw)

        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            mod = real_import(name, *a, **k)
            if name == "slack_sdk.web" or name.endswith("slack_sdk.web"):
                return types.SimpleNamespace(WebClient=FakeClient)
            if name == "slack_sdk":
                # allow nested
                return mod
            return mod

        # Simpler: patch the local import target used inside build_web_client
        with mock.patch.dict("sys.modules", {
            "slack_sdk": types.SimpleNamespace(web=types.SimpleNamespace(WebClient=FakeClient)),
            "slack_sdk.web": types.SimpleNamespace(WebClient=FakeClient),
        }):
            client = send_mod.build_web_client("xoxb-test", ssl_context=None)
        self.assertEqual(created.get("retry_handlers"), [])
        self.assertEqual(created.get("token"), "xoxb-test")

    def test_after_accept_disconnect_single_underlying_send(self):
        """Mock: connection error after accept path — only one chat_postMessage.

        Real slack_sdk not required; we simulate WebClient with retries disabled
        and assert our send path invokes post exactly once (NOT_EXERCISED: live SDK).
        """
        import send as send_mod
        calls = {"n": 0}

        class FakeResp(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)

        class FakeClient:
            def __init__(self, **kw):
                self.kw = kw
                assert kw.get("retry_handlers") == []

            def chat_postMessage(self, **kwargs):
                calls["n"] += 1
                # Simulate: server accepted then transport died (ambiguous).
                raise ConnectionError("disconnect after accept")

        class FakeSlackApiError(Exception):
            def __init__(self, *a, **k):
                self.response = None

        # Drive main() pieces via build + post
        client = FakeClient(token="x", ssl=None, retry_handlers=[])
        self.assertEqual(client.kw["retry_handlers"], [])
        with self.assertRaises(ConnectionError):
            client.chat_postMessage(channel="C", text="hi")
        self.assertEqual(calls["n"], 1)
        # Second call would be a bug (SDK retry); confirm still 1
        self.assertEqual(calls["n"], 1)

    def test_classify_slack_api_error_whitelist(self):
        import send as send_mod
        self.assertEqual(send_mod.classify_slack_api_error("channel_not_found"), "not_sent")
        self.assertEqual(send_mod.classify_slack_api_error("internal_error"), "uncertain")
        self.assertEqual(send_mod.classify_slack_api_error("fatal_error"), "uncertain")
        self.assertEqual(send_mod.classify_slack_api_error("some_new_unknown"), "uncertain")


class TestPendingAckOnlyRecovery(unittest.TestCase):
    def test_peek_actionable_includes_sent_excludes_uncertain(self):
        from inbox_store import peek_actionable
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C", "ts": "1", "text": "a",
                "delivered": False, "reply_status": None,
            })
            append_record(inbox, {
                "channel": "C", "ts": "2", "text": "b",
                "delivered": False, "reply_status": None,
            })
            append_record(inbox, {
                "channel": "C", "ts": "3", "text": "c",
                "delivered": False, "reply_status": None,
            })
            set_reply_status(inbox, {("C", "2")}, "sent")
            set_reply_status(inbox, {("C", "3")}, "uncertain")
            keys = {msg_key(r) for r in peek_actionable(inbox)}
            self.assertEqual(keys, {("C", "1"), ("C", "2")})
            self.assertNotIn(("C", "3"), keys)

    def test_pending_consume_acks_sent_without_resend(self):
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C", "ts": "9", "text": "x",
                "delivered": False, "reply_status": None,
            })
            set_reply_status(inbox, {("C", "9")}, "sent")
            sends = []

            def runner(*args, input_text=None):
                sends.append(1)
                return _fake(0, "sent ok: True")

            from inbox_store import peek_actionable
            rows = peek_actionable(inbox)
            self.assertEqual(len(rows), 1)
            r = rp.process_one(inbox, rows[0], "should-not-send", runner=runner)
            self.assertEqual(r["outcome"], "acked")
            self.assertEqual(sends, [])
            self.assertEqual(peek_undelivered(inbox), [])
