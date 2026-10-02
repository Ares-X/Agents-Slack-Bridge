"""Exercise send.main -> consumer -> durable state/ACK without Slack or sleeps."""
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


class Flow:
    """Real entry points and stores; only SDK, subprocess dispatch and clock fake."""
    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.now = 1000.0
        self.posts = []
        self.clients = []
        self.sessions = {}
        self.message = {"msg_id": "C_TEST:1000", "channel": "C_TEST",
                        "ts": "1000", "thread_ts": "", "text": "request"}

    def __enter__(self):
        self.stack = contextlib.ExitStack()
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (self.base / ".env").write_text("SLACK_BOT_TOKEN=fake-token\n")
        self.path = str(self.base / "send_state.json")
        flow = self

        class Client:
            def __init__(self, **kwargs):
                flow.clients.append(kwargs)

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
        for context in (
            patch.dict(sys.modules, {"slack_sdk": sdk, "slack_sdk.web": web}),
            patch.object(send, "BASE", str(self.base)),
            patch.object(inbox_store, "BASE", str(self.base)),
            patch.object(inbox_store, "INBOX_PATH", str(self.base / "inbox.jsonl")),
            patch.object(inbox_store, "LOCK_PATH", str(self.base / "inbox.lock")),
            patch.dict(inbox_store._MIGRATED, {}, clear=True),
            patch("time.time", side_effect=lambda: self.now),
            patch("time.sleep", side_effect=AssertionError("sender must not sleep")),
            patch.object(pc, "sh", side_effect=self.run_script),
        ):
            self.stack.enter_context(context)
        self.history = self.stack.enter_context(
            patch.object(pc, "channel_history", return_value=([], None)))
        self.stack.enter_context(
            patch.object(pc, "generate_reply", return_value="test reply"))
        inbox_store.append_record(dict(self.message))
        self.reopen()
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def reopen(self):
        self.state = SendState(self.path,
                               inbox_path=inbox_store.INBOX_PATH,
                               inbox_lock_path=inbox_store.LOCK_PATH)

    def run_script(self, *args, input_text=None, timeout=None):
        scripts = {"send.py": send.main, "inbox_ack.py": inbox_ack.main}
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", list(args[1:])), \
             patch.object(sys, "stdin", io.StringIO(input_text or "")), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                scripts[os.path.basename(args[1])]()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return types.SimpleNamespace(returncode=code, stdout=out.getvalue(),
                                     stderr=err.getvalue())

    def handle(self):
        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return pc.handle_one(self.message, self.sessions, self.state,
                                 bot_id="B_TEST", bot_user_id="U_TEST")

    def entry(self):
        return self.state.get(self.message["msg_id"])


class RateLimitRecoveryTest(unittest.TestCase):
    def test_long_wait_survives_restart_then_sends_and_acks_once(self):
        for delay in (120, 60):
            with self.subTest(delay=delay), Flow(
                    SlackApiError(headers={"Retry-After": str(delay)}),
                    {"ok": True, "ts": "1120"}) as flow:
                self.assertEqual(flow.handle(), "retry-deferred")
                deadline = 1000 + delay
                self.assertEqual(flow.entry()["retry_at"], deadline)
                saved = json.loads(Path(flow.path).read_text())
                self.assertEqual(saved["sends"]["C_TEST:1000"]["status"],
                                 "retry_wait")
                history_calls = flow.history.call_count
                flow.now = deadline - 1
                flow.reopen()
                self.assertEqual(flow.handle(), "retry-deferred")
                self.assertEqual(flow.history.call_count, history_calls)
                self.assertEqual(len(flow.posts), 1)
                flow.now = deadline
                flow.reopen()
                self.assertEqual(flow.handle(), "replied")
                self.assertEqual([t for t, _ in flow.posts], [1000, deadline])
                self.assertNotEqual(flow.posts[0][1]["client_msg_id"],
                                    flow.posts[1][1]["client_msg_id"])
                self.assertEqual(inbox_store.read_undelivered(), [])
                self.assertIsNone(flow.entry())
                # A stale poll snapshot after a restart cannot send again.
                flow.reopen()
                self.assertEqual(flow.handle(), "already-acked")
                self.assertEqual(len(flow.posts), 2)
                self.assertTrue(all(c["retry_handlers"] == [] for c in flow.clients))

    def test_repeated_limits_keep_each_full_deadline(self):
        with Flow(SlackApiError(headers={"Retry-After": "120"}),
                  SlackApiError(headers={"Retry-After": "60"}),
                  {"ok": True, "ts": "1180"}) as flow:
            for start, deadline in ((1000, 1120), (1120, 1180)):
                flow.now = start
                flow.reopen()
                self.assertEqual(flow.handle(), "retry-deferred")
                self.assertEqual(flow.entry()["retry_at"], deadline)
                flow.now = deadline - 1
                flow.reopen()
                self.assertEqual(flow.handle(), "retry-deferred")
            flow.now = 1180
            flow.reopen()
            self.assertEqual(flow.handle(), "replied")
            self.assertEqual([t for t, _ in flow.posts], [1000, 1120, 1180])

    def test_rate_error_aliases_and_header_forms(self):
        cases = [
            (SlackApiError(429, "other", {"Retry-After": "120"}), 120),
            (SlackApiError(200, "ratelimited", {"retry-after": "60"}), 60),
            (SlackApiError(200, "rate_limited", {"Retry-After": ["90"]}), 90),
            (SlackApiError(200, "rate_limited", retry_after=75), 75),
            (SlackApiError(headers={"Retry-After": "0"}), 0),
            (SlackApiError(), 60),
            (SlackApiError(headers={"Retry-After": "invalid"}), 60),
            (SlackApiError(headers={"Retry-After": "nan"}), 60),
            (SlackApiError(headers={"Retry-After": "inf"}), 60),
            (SlackApiError(headers={"Retry-After": "-1"}), 60),
        ]
        for error, delay in cases:
            with self.subTest(error=str(error), headers=error.response.headers), \
                 Flow(error) as flow:
                self.assertEqual(flow.handle(), "retry-deferred")
                self.assertEqual(flow.entry()["retry_at"], 1000 + delay)
                self.assertEqual(len(flow.posts), 1)

    def test_unknown_errors_never_retry_after_restart(self):
        errors = [SlackApiError(200, "internal_error"),
                  SlackApiError(200, "fatal_error"),
                  SlackApiError(503, "new_unknown_error"),
                  ConnectionError("reset"), TimeoutError("response lost")]
        for error in errors:
            with self.subTest(error=str(error)), Flow(error) as flow:
                self.assertEqual(flow.handle(), "uncertain-held")
                flow.now += 120
                flow.reopen()
                self.assertEqual(flow.handle(), "uncertain-held")
                self.assertEqual(len(flow.posts), 1)
                self.assertEqual(flow.clients[0]["retry_handlers"], [])
                self.assertEqual(len(inbox_store.read_undelivered()), 1)

    def test_retry_can_become_uncertain_without_third_send(self):
        with Flow(SlackApiError(headers={"Retry-After": "60"}),
                  ConnectionError("lost response")) as flow:
            self.assertEqual(flow.handle(), "retry-deferred")
            flow.now = 1060
            flow.reopen()
            self.assertEqual(flow.handle(), "uncertain-held")
            flow.now = 1120
            flow.reopen()
            self.assertEqual(flow.handle(), "uncertain-held")
            self.assertEqual(len(flow.posts), 2)

    def test_failed_defer_write_keeps_claim_unretryable(self):
        with Flow(SlackApiError(headers={"Retry-After": "120"})) as flow:
            with patch.object(flow.state, "defer_retry", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    flow.handle()
            flow.now += 120
            flow.reopen()
            self.assertEqual(flow.handle(), "uncertain-held")
            self.assertEqual(len(flow.posts), 1)

    def test_concurrent_defer_between_get_and_claim_stays_deferred(self):
        with Flow(SlackApiError(headers={"Retry-After": "120"})) as flow:
            self.assertEqual(flow.handle(), "retry-deferred")
            current = flow.entry()
            # The first read predates another consumer's claim + deferred result.
            with patch.object(flow.state, "get", side_effect=[None, current]):
                self.assertEqual(flow.handle(), "retry-deferred")
            self.assertEqual(flow.entry()["status"], "retry_wait")
            self.assertEqual(len(flow.posts), 1)

    def test_malformed_defer_output_is_uncertain(self):
        for out in ('{"result":"retry_wait","retry_at":NaN}',
                    '{"result":"retry_wait","retry_at":true}',
                    '{"result":"retry_wait","retry_at":-1}',
                    '[]', 'truncated'):
            with self.subTest(out=out), patch.object(pc, "sh", return_value=
                    types.SimpleNamespace(returncode=75, stdout=out, stderr="")):
                self.assertEqual(pc.send_reply("C_TEST", "reply"), "uncertain")


if __name__ == "__main__":
    unittest.main()
