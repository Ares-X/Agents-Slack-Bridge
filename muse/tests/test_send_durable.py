"""Isolated tests for send_durable.py (the real agent-chain send entry).

Covers: ok -> exit 0 + ack; duplicate delivery suppressed (claim held /
tombstone completed); 429 -> exit 75 with retry_at persisted and no resend
before the deadline; uncertain -> exit 2 and held; explicit failure ->
exit 1 with claim released. No Slack, no sleeps, no real inbox.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "consumer"))

import inbox_ack
import inbox_store
import poll_consumer as pc
import send
import send_durable
from send_state import SendState


class SlackApiError(Exception):
    def __init__(self, status=429, error="ratelimited", headers=None, **data):
        super().__init__(error)
        self.response = Response(status, headers, error=error, **data)


class Response(dict):
    def __init__(self, status, headers, **data):
        super().__init__(ok=False, **data)
        self.status_code = status
        self.headers = headers or {}


class DurableFlow:
    """Real send_durable.main with faked SDK, subprocess dispatch and clock."""

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.now = 1000.0
        self.posts = []

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.base / ".env").write_text("SLACK_BOT_TOKEN=<redacted>\n")
        flow = self

        class Client:
            def __init__(self, **kwargs):
                pass

            def chat_postMessage(self, **kwargs):
                flow.posts.append((flow.now, kwargs))
                result = flow.behaviors.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result

        web = types.ModuleType("slack_sdk.web")
        web.WebClient = Client
        sdk = types.ModuleType("slack_sdk")
        sdk.web = web

        def run_script(*args, input_text=None, timeout=None):
            scripts = {"send.py": send.main, "inbox_ack.py": inbox_ack.main}
            out, err = io.StringIO(), io.StringIO()
            with patch.object(sys, "argv", list(args[1:])), \
                 patch.object(sys, "stdin", io.StringIO(input_text or "")), \
                 contextlib.redirect_stdout(out), \
                 contextlib.redirect_stderr(err):
                try:
                    scripts[os.path.basename(args[1])]()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
            return types.SimpleNamespace(returncode=code, stdout=out.getvalue(),
                                         stderr=err.getvalue())

        for context in (
            patch.dict(sys.modules, {"slack_sdk": sdk, "slack_sdk.web": web}),
            patch.object(send, "BASE", str(self.base)),
            patch.object(inbox_store, "BASE", str(self.base)),
            patch.object(inbox_store, "INBOX_PATH",
                         str(self.base / "inbox.jsonl")),
            patch.object(inbox_store, "LOCK_PATH", str(self.base / "inbox.lock")),
            patch.dict(inbox_store._MIGRATED, {}, clear=True),
            patch.object(send_durable, "STATE_PATH",
                         str(self.base / "send_state.json")),
            patch.object(send_durable, "INBOX_PATH",
                         str(self.base / "inbox.jsonl")),
            patch.object(send_durable, "INBOX_LOCK_PATH",
                         str(self.base / "inbox.lock")),
            patch.object(send_durable, "resolve_bot_identity",
                         return_value=("B_TEST", "U_TEST")),
            patch("time.time", side_effect=lambda: self.now),
            patch("time.sleep",
                  side_effect=AssertionError("sender must not sleep")),
            patch.object(pc, "sh", side_effect=run_script),
            patch.object(pc, "channel_history", return_value=([], None)),
        ):
            self.stack.enter_context(context)
        inbox_store.append_record({"msg_id": "C_TEST:1000", "channel": "C_TEST",
                                   "ts": "1000", "text": "hi"})
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def run(self, text="agent reply"):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv",
                          ["send_durable.py", "C_TEST:1000"]), \
             patch.object(sys, "stdin", io.StringIO(text)), \
             contextlib.redirect_stdout(out), \
             contextlib.redirect_stderr(err):
            code = send_durable.main()
        return code, out.getvalue(), err.getvalue()

    def saved_state(self):
        return json.loads((self.base / "send_state.json").read_text())

    def inbox_text(self):
        return (self.base / "inbox.jsonl").read_text()


class SendDurableTest(unittest.TestCase):
    def test_ok_sends_posts_and_acks(self):
        with DurableFlow({"ok": True, "ts": "1001"}) as flow:
            code, out, _ = flow.run()
            self.assertEqual(code, 0)
            self.assertIn("replied", out)
            self.assertEqual(len(flow.posts), 1)
            # client_msg_id travels with the post for history verification
            self.assertIn("client_msg_id", flow.posts[0][1])
            self.assertIn("ack", flow.inbox_text())

    def test_second_delivery_after_tombstone_sends_nothing(self):
        with DurableFlow({"ok": True, "ts": "1001"}) as flow:
            self.assertEqual(flow.run()[0], 0)
            self.assertEqual(len(flow.posts), 1)
            # Same message delivered again (e.g. second hook wake): the
            # durable tombstone proves it is done; no second post.
            code, out, _ = flow.run("agent reply again")
            self.assertEqual(code, 0)
            self.assertIn("already-acked", out)
            self.assertEqual(len(flow.posts), 1)

    def test_concurrent_claim_is_not_resent(self):
        with DurableFlow({"ok": True, "ts": "1001"}) as flow:
            other = SendState(str(flow.base / "send_state.json"),
                              inbox_path=str(flow.base / "inbox.jsonl"),
                              inbox_lock_path=str(flow.base / "inbox.lock"))
            other.claim("C_TEST:1000", channel="C_TEST", thread_ts="",
                        text_hash="x" * 64, client_msg_id="other-attempt")
            # Another agent holds the claim: we must not send in parallel.
            code, out, _ = flow.run()
            self.assertEqual(code, 2)  # uncertain-held: verify only
            self.assertEqual(len(flow.posts), 0)

    def test_rate_limit_persists_deadline_and_defers(self):
        with DurableFlow(SlackApiError(headers={"Retry-After": "120"}),
                         {"ok": True, "ts": "1002"}) as flow:
            code, out, _ = flow.run()
            self.assertEqual(code, 75)
            self.assertIn("retry-deferred", out)
            saved = flow.saved_state()["sends"]["C_TEST:1000"]
            self.assertEqual(saved["status"], "retry_wait")
            self.assertEqual(saved["retry_at"], 1120.0)
            self.assertEqual(len(flow.posts), 1)  # the rejected attempt only
            # Before the deadline: no resend, even though a hook woke us.
            flow.now = 1119.0
            code, out, _ = flow.run()
            self.assertEqual(code, 75)
            self.assertIn("retry-deferred", out)
            self.assertEqual(len(flow.posts), 1)
            # After the deadline: exactly one more attempt, then acked.
            flow.now = 1120.0
            code, out, _ = flow.run()
            self.assertEqual(code, 0)
            self.assertIn("replied", out)
            self.assertEqual(len(flow.posts), 2)
            self.assertNotEqual(flow.posts[0][1]["client_msg_id"],
                                flow.posts[1][1]["client_msg_id"])

    def test_uncertain_is_held_not_acked(self):
        with DurableFlow(RuntimeError("connection reset")) as flow:
            code, out, _ = flow.run()
            self.assertEqual(code, 2)
            self.assertIn("uncertain-held", out)
            self.assertEqual(len(flow.posts), 1)
            saved = flow.saved_state()["sends"]["C_TEST:1000"]
            self.assertEqual(saved["status"], "uncertain")
            self.assertNotIn("ack", flow.inbox_text())

    def test_explicit_failure_releases_claim_without_ack(self):
        with DurableFlow({"ok": False, "error": "channel_not_found"}) as flow:
            code, out, _ = flow.run()
            self.assertEqual(code, 1)
            self.assertIn("send-failed", out)
            self.assertNotIn("C_TEST:1000",
                             flow.saved_state().get("sends", {}))
            self.assertNotIn("ack", flow.inbox_text())

    def test_empty_reply_refuses_to_send(self):
        with DurableFlow({"ok": True, "ts": "1001"}) as flow:
            code, _, err = flow.run("   ")
            self.assertEqual(code, 1)
            self.assertIn("empty reply", err)
            self.assertEqual(len(flow.posts), 0)


if __name__ == "__main__":
    unittest.main()
