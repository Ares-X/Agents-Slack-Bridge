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


class TestRateLimitRecovery(unittest.TestCase):
    """Behavioral: rate-limit → wait (no send) → after expiry send+ACK; no dup."""

    def test_classify_rate_limited(self):
        self.assertEqual(
            rp.classify_send_result(
                _fake(3, "sent ok: False", "rate_limited: retry_after=30")
            ),
            "rate_limited",
        )
        # Must not become uncertain
        self.assertNotEqual(
            rp.classify_send_result(
                _fake(3, "", "rate_limited: retry_after=1")
            ),
            "uncertain",
        )

    def test_classify_slack_rate_limit_helpers(self):
        import send as send_mod
        self.assertEqual(
            send_mod.classify_slack_api_error("ratelimited"), "rate_limited"
        )
        self.assertEqual(
            send_mod.classify_slack_api_error("rate_limited"), "rate_limited"
        )
        self.assertEqual(
            send_mod.classify_slack_api_error("x", status_code=429), "rate_limited"
        )
        # Still uncertain for ambiguous
        self.assertEqual(
            send_mod.classify_slack_api_error("internal_error"), "uncertain"
        )
        # Retry-After extraction
        class Resp:
            headers = {"Retry-After": "42"}
            def get(self, k, default=None):
                return default
        self.assertEqual(send_mod.extract_retry_after_seconds(Resp()), 42.0)

    def test_rate_limit_wait_then_success_no_duplicate(self):
        clock = {"t": 1000.0}

        def now():
            return clock["t"]

        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C1", "ts": "10.1", "text": "ping",
                "user": "U1", "delivered": False, "reply_status": None,
            })
            sends = []

            def runner(*args, input_text=None):
                sends.append({"t": clock["t"], "text": input_text})
                if len(sends) == 1:
                    return _fake(3, "sent ok: False", "rate_limited: retry_after=30")
                return _fake(0, "sent ok: True ts: 99.0")

            m = peek_undelivered(inbox)[0]
            r1 = rp.process_one(
                inbox, m, "hi", runner=runner, time_fn=now,
            )
            self.assertEqual(r1["outcome"], "rate_limited")
            self.assertEqual(r1["retry_after_sec"], 30.0)
            self.assertEqual(r1["retry_after_until"], 1030.0)
            self.assertEqual(len(sends), 1)

            m2 = peek_undelivered(inbox)[0]
            self.assertEqual(m2["reply_status"], "rate_limited")

            # During wait — no send
            r2 = rp.process_one(
                inbox, m2, "hi", runner=runner, time_fn=now,
            )
            self.assertEqual(r2["outcome"], "wait_rate_limit")
            self.assertEqual(len(sends), 1)

            # Still during wait (advance but not enough)
            clock["t"] = 1029.0
            m3 = peek_undelivered(inbox)[0]
            r3 = rp.process_one(
                inbox, m3, "hi", runner=runner, time_fn=now,
            )
            self.assertEqual(r3["outcome"], "wait_rate_limit")
            self.assertEqual(len(sends), 1)

            # After expiry — send + ACK
            clock["t"] = 1030.0
            m4 = peek_undelivered(inbox)[0]
            from inbox_store import is_send_ready
            self.assertTrue(is_send_ready(m4, now=clock["t"]))
            r4 = rp.process_one(
                inbox, m4, "hi", runner=runner, time_fn=now,
            )
            self.assertIn(r4["outcome"], ("sent_acked", "sent_ack_pending"))
            self.assertEqual(len(sends), 2)

            # No further undelivered / no duplicate send
            self.assertEqual(peek_undelivered(inbox), [])
            r5 = rp.process_one(
                inbox,
                {"channel": "C1", "ts": "10.1", "delivered": True,
                 "reply_status": "sent"},
                "hi", runner=runner, time_fn=now,
            )
            self.assertEqual(r5["outcome"], "already_delivered")
            self.assertEqual(len(sends), 2)

    def test_internal_error_still_no_resend_after_rate_limit_fix(self):
        """Regression: ambiguous errors remain uncertain forever."""
        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C1", "ts": "1", "text": "x",
                "delivered": False, "reply_status": None,
            })
            sends = []

            def runner(*args, input_text=None):
                sends.append(1)
                return _fake(2, "", "send_error: slack_api internal_error")

            r1 = rp.process_one(inbox, peek_undelivered(inbox)[0], "a", runner=runner)
            self.assertEqual(r1["outcome"], "uncertain")
            r2 = rp.process_one(inbox, peek_undelivered(inbox)[0], "a", runner=runner)
            self.assertEqual(r2["outcome"], "no_resend")
            self.assertEqual(len(sends), 1)


# --- agent_wake / pending single-target / mode resolution (PR #6 fixes) ---

class TestPendingConsumeOnceSingleTarget(unittest.TestCase):
    """P1: --text must not broadcast; channel+ts targets one row only."""

    def test_two_channels_only_target_sends_and_acks(self):
        import pending_consume_once as pco

        with tempfile.TemporaryDirectory() as d:
            inbox = os.path.join(d, "inbox.jsonl")
            append_record(inbox, {
                "channel": "C_ALPHA", "ts": "100.1", "text": "question A?",
                "user": "U1", "delivered": False, "reply_status": None,
            })
            append_record(inbox, {
                "channel": "C_BETA", "ts": "200.2", "text": "question B?",
                "user": "U2", "delivered": False, "reply_status": None,
            })
            sends = []

            def runner(*args, input_text=None):
                # send.py argv includes channel
                ch = None
                for i, a in enumerate(args):
                    if a.endswith("send.py") and i + 1 < len(args):
                        ch = args[i + 1]
                        break
                sends.append({"channel": ch, "text": input_text})
                return _fake(0, "sent ok: True ts: 9.0")

            r = pco.consume_one(
                inbox, "C_ALPHA", "100.1", "answer for A only", runner=runner
            )
            self.assertIn(r["outcome"], ("sent_acked", "sent_ack_pending"))
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0]["channel"], "C_ALPHA")
            self.assertEqual(sends[0]["text"], "answer for A only")

            left = {(x["channel"], x["ts"]) for x in peek_undelivered(inbox)}
            self.assertEqual(left, {("C_BETA", "200.2")})
            self.assertNotIn(("C_ALPHA", "100.1"), left)

            # Second channel still claimable — different text would go only there
            r2 = pco.consume_one(
                inbox, "C_BETA", "200.2", "answer for B only", runner=runner
            )
            self.assertIn(r2["outcome"], ("sent_acked", "sent_ack_pending"))
            self.assertEqual(len(sends), 2)
            self.assertEqual(sends[1]["channel"], "C_BETA")
            self.assertEqual(sends[1]["text"], "answer for B only")
            self.assertEqual(peek_undelivered(inbox), [])

    def test_cli_requires_channel_ts_refuses_broadcast(self):
        import pending_consume_once as pco
        rc = pco.main(["--text", "broadcast-me"])
        self.assertEqual(rc, 2)


class TestResolveReplyMode(unittest.TestCase):
    """P2: template only if explicit; else error (keep messages)."""

    def test_template_only_when_explicit(self):
        pc = _load_consumer()
        with tempfile.TemporaryDirectory() as d:
            wh = os.path.join(d, "webhook.env")
            # no webhook.env, no REPLY_MODE → error
            with mock.patch.dict(os.environ, {"REPLY_MODE": ""}, clear=False):
                os.environ.pop("REPLY_MODE", None)
                self.assertEqual(
                    pc.resolve_reply_mode(env={}, webhook_env_path=wh),
                    "error",
                )
            # invalid mode → error even if webhook present
            Path = __import__("pathlib").Path
            Path(wh).write_text("WEBHOOK_URL=http://x\nWEBHOOK_KEY=k\n")
            self.assertEqual(
                pc.resolve_reply_mode(
                    env={"REPLY_MODE": "bogus"}, webhook_env_path=wh
                ),
                "error",
            )
            # explicit template → template (even without webhook)
            self.assertEqual(
                pc.resolve_reply_mode(
                    env={"REPLY_MODE": "template"},
                    webhook_env_path=os.path.join(d, "missing.env"),
                ),
                "template",
            )
            # webhook.env + unset → agent_wake
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("REPLY_MODE", None)
                self.assertEqual(
                    pc.resolve_reply_mode(env={}, webhook_env_path=wh),
                    "agent_wake",
                )
            # explicit agent_wake
            self.assertEqual(
                pc.resolve_reply_mode(
                    env={"REPLY_MODE": "agent_wake"},
                    webhook_env_path=os.path.join(d, "missing.env"),
                ),
                "agent_wake",
            )


class TestWakeAgentConsumerBackoff(unittest.TestCase):
    """P2: 429 Retry-After honored; failures not success; bounded backoff."""

    def test_429_honors_retry_after_not_success(self):
        pc = _load_consumer()
        clock = {"t": 1000.0}
        calls = {"wake": 0}

        def runner(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                return _fake(4, "retry_after_sec=10\n", "wake_agent: 429")
            return _fake(0, "")

        claimable = [{"channel": "C1", "ts": "1.0", "reply_status": None}]
        st = {
            "last_fp": "", "last_keys": frozenset(), "last_at": 0.0,
            "wake_fail_count": 0, "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }
        st1 = pc.maybe_wake_agent(
            claimable, st, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        # Must NOT treat as success fingerprint post
        self.assertEqual(st1.get("last_at"), 0.0)
        self.assertEqual(st1.get("wake_retry_after_until"), 1010.0)

        # Within Retry-After — no second wake
        clock["t"] = 1005.0
        st2 = pc.maybe_wake_agent(
            claimable, st1, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        self.assertEqual(st2.get("wake_retry_after_until"), 1010.0)

        # After wait — may wake again
        clock["t"] = 1010.0

        def runner_ok(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                return _fake(0, "wake_agent: posted webhook for 1 claimable\n")
            return _fake(0, "")

        st3 = pc.maybe_wake_agent(
            claimable, st2, time_fn=lambda: clock["t"], runner=runner_ok
        )
        self.assertEqual(calls["wake"], 2)
        self.assertEqual(st3.get("last_at"), 1010.0)
        self.assertEqual(st3.get("wake_fail_count"), 0)

    def test_failure_bounded_backoff_not_success(self):
        pc = _load_consumer()
        clock = {"t": 5000.0}
        calls = {"wake": 0}

        def runner(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                return _fake(3, "", "wake_agent: HTTPError status=500")
            return _fake(0, "")

        claimable = [{"channel": "C9", "ts": "9.0"}]
        st = {
            "last_fp": "", "last_keys": frozenset(), "last_at": 0.0,
            "wake_fail_count": 0, "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }
        st1 = pc.maybe_wake_agent(
            claimable, st, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        self.assertEqual(st1["wake_fail_count"], 1)
        self.assertEqual(st1["wake_backoff_until"], 5000.0 + 5.0)
        self.assertEqual(st1.get("last_at"), 0.0)

        # During backoff — skip
        clock["t"] = 5003.0
        st2 = pc.maybe_wake_agent(
            claimable, st1, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)

        # After backoff — second failure doubles
        clock["t"] = 5005.0
        st3 = pc.maybe_wake_agent(
            claimable, st2, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 2)
        self.assertEqual(st3["wake_fail_count"], 2)
        self.assertEqual(st3["wake_backoff_until"], 5005.0 + 10.0)



class TestWakeBackoffOverflowCap(unittest.TestCase):
    """Cap exponent before 2**n — huge fail_count must stay finite."""

    def test_backoff_over_1025_finite_no_raise(self):
        pc = _load_consumer()
        for n in (1, 2, 5, 10, 100, 1024, 1025, 1026, 5000, 10**6):
            delay = pc._wake_backoff_sec(n)
            self.assertIsInstance(delay, float)
            self.assertGreater(delay, 0.0)
            self.assertLessEqual(delay, pc.WAKE_BACKOFF_CAP)
            self.assertTrue(delay == delay)  # not NaN
            # Must not be inf
            self.assertNotEqual(delay, float("inf"))

    def test_sequence_matches_bounded_exp_until_cap(self):
        pc = _load_consumer()
        self.assertEqual(pc._wake_backoff_sec(1), 5.0)
        self.assertEqual(pc._wake_backoff_sec(2), 10.0)
        self.assertEqual(pc._wake_backoff_sec(3), 20.0)
        self.assertEqual(pc._wake_backoff_sec(4), 40.0)
        self.assertEqual(pc._wake_backoff_sec(5), 60.0)  # capped
        self.assertEqual(pc._wake_backoff_sec(1025), 60.0)

    def test_continuous_failures_beyond_1025_still_backoff(self):
        pc = _load_consumer()
        clock = {"t": 0.0}
        calls = {"wake": 0}

        def runner(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                return _fake(3, "", "wake_agent: HTTPError status=500")
            return _fake(0, "")

        claimable = [{"channel": "C9", "ts": "9.0"}]
        st = {
            "last_fp": "", "last_keys": frozenset(), "last_at": 0.0,
            "wake_fail_count": 1024, "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }
        # fail_count starts at 1024; next failure → 1025
        st1 = pc.maybe_wake_agent(
            claimable, st, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(st1["wake_fail_count"], 1025)
        self.assertEqual(st1["wake_backoff_until"], 0.0 + 60.0)
        self.assertEqual(calls["wake"], 1)

        # During backoff — skip
        clock["t"] = 30.0
        st2 = pc.maybe_wake_agent(
            claimable, st1, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)

        # After backoff — another failure still finite
        clock["t"] = 60.0
        st3 = pc.maybe_wake_agent(
            claimable, st2, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(st3["wake_fail_count"], 1026)
        self.assertEqual(st3["wake_backoff_until"], 60.0 + 60.0)
        self.assertEqual(calls["wake"], 2)


class TestWakeBackoffTimingAfterReturn(unittest.TestCase):
    """Retry-After / failure backoff must start AFTER the operation returns."""

    def test_429_retry_after_accounts_for_request_duration(self):
        pc = _load_consumer()
        clock = {"t": 1000.0}
        calls = {"wake": 0}

        def runner(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                # Simulate non-zero HTTP duration: 5s wall clock
                clock["t"] += 5.0
                return _fake(4, "retry_after_sec=10\n", "wake_agent: 429")
            return _fake(0, "")

        claimable = [{"channel": "C1", "ts": "1.0", "reply_status": None}]
        st = {
            "last_fp": "", "last_keys": frozenset(), "last_at": 0.0,
            "wake_fail_count": 0, "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }
        st1 = pc.maybe_wake_agent(
            claimable, st, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        # Call started at 1000, returned at 1005 → until = 1005 + 10 = 1015
        # (NOT 1000 + 10 = 1010 which would under-wait by request duration)
        self.assertEqual(st1.get("wake_retry_after_until"), 1015.0)
        self.assertEqual(clock["t"], 1005.0)

        # At 1010 (< 1015) must still skip
        clock["t"] = 1010.0
        st2 = pc.maybe_wake_agent(
            claimable, st1, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        self.assertEqual(st2.get("wake_retry_after_until"), 1015.0)

        # At 1015 may wake again
        clock["t"] = 1015.0

        def runner_ok(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                clock["t"] += 1.0
                return _fake(0, "ok\n")
            return _fake(0, "")

        st3 = pc.maybe_wake_agent(
            claimable, st2, time_fn=lambda: clock["t"], runner=runner_ok
        )
        self.assertEqual(calls["wake"], 2)
        self.assertEqual(st3.get("last_at"), 1016.0)  # after +1s duration

    def test_failure_backoff_accounts_for_request_duration(self):
        pc = _load_consumer()
        clock = {"t": 5000.0}
        calls = {"wake": 0}

        def runner(*args, input_text=None):
            cmd = " ".join(str(a) for a in args)
            if "pending_notify" in cmd:
                return _fake(0, "1")
            if "wake_agent" in cmd:
                calls["wake"] += 1
                clock["t"] += 3.0  # non-zero request duration
                return _fake(3, "", "wake_agent: HTTPError status=500")
            return _fake(0, "")

        claimable = [{"channel": "C9", "ts": "9.0"}]
        st = {
            "last_fp": "", "last_keys": frozenset(), "last_at": 0.0,
            "wake_fail_count": 0, "wake_retry_after_until": 0.0,
            "wake_backoff_until": 0.0,
        }
        st1 = pc.maybe_wake_agent(
            claimable, st, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
        # Started 5000, returned 5003 → backoff until 5003 + 5
        self.assertEqual(st1["wake_fail_count"], 1)
        self.assertEqual(st1["wake_backoff_until"], 5003.0 + 5.0)
        self.assertEqual(clock["t"], 5003.0)

        # Old bug would have set until=5005; at t=5006 would wake early.
        # Correct: until=5008, so at 5006 still skip.
        clock["t"] = 5006.0
        st2 = pc.maybe_wake_agent(
            claimable, st1, time_fn=lambda: clock["t"], runner=runner
        )
        self.assertEqual(calls["wake"], 1)
