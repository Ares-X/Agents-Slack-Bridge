"""Extra isolated tests for the durable send pipeline (hook -> agent chain).

Covers: hook concurrency (parallel race, single post); crash after send
(unacked -> ACK-only, never resends text); explicit ACK-only path;
rate-limit wait (no send before retry_at, one send after); uncertain never
resends; thread_ts propagation; empty-env proxy/CA config precedence.
No Slack, no sleeps, no real inbox.
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "consumer"))

import inbox_store
import net_config
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
        self.retry_after = self.headers.get("Retry-After")


class Harness:
    """Direct deliver_one() driver with faked SDK/subprocess/clock."""

    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.now = 5000.0
        self.posts = []
        self.acks = []

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

        def fake_sh(*args, input_text=None, capture_output=False, timeout=None):
            # send.py 走真实 send.main（faked Slack SDK）；ack 走记录即可
            if os.path.basename(args[1]) == "send.py":
                out, err = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", list(args[1:])), \
                     patch.object(sys, "stdin",
                                  io.StringIO(input_text or "")), \
                     contextlib.redirect_stdout(out), \
                     contextlib.redirect_stderr(err):
                    try:
                        send.main()
                        code = 0
                    except SystemExit as exc:
                        code = exc.code
                return types.SimpleNamespace(returncode=code,
                                             stdout=out.getvalue(),
                                             stderr=err.getvalue())
            flow.acks.append(args)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        for context in (
            patch.dict(sys.modules, {"slack_sdk": sdk, "slack_sdk.web": web}),
            patch.object(send, "BASE", str(self.base)),
            patch.object(inbox_store, "BASE", str(self.base)),
            patch.object(inbox_store, "INBOX_PATH",
                         str(self.base / "inbox.jsonl")),
            patch.object(inbox_store, "LOCK_PATH", str(self.base / "inbox.lock")),
            patch.dict(inbox_store._MIGRATED, {}, clear=True),
            patch("time.time", side_effect=lambda: self.now),
            patch("time.sleep",
                  side_effect=AssertionError("sender must not sleep")),
            patch.object(pc, "sh", side_effect=fake_sh),
            patch.object(pc, "channel_history", return_value=([], None)),
        ):
            self.stack.enter_context(context)
        inbox_store.append_record({"msg_id": "C_T:42", "channel": "C_T",
                                   "ts": "42", "text": "ping"})
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def state(self):
        return SendState(str(self.base / "send_state.json"),
                         inbox_path=str(self.base / "inbox.jsonl"))

    def deliver(self, text="reply", thread_ts=""):
        m = {"msg_id": "C_T:42", "channel": "C_T", "thread_ts": thread_ts}
        return pc.deliver_one(m, text, [], self.state(), "B_T", "U_T")


def ok_result():
    return {"ok": True, "channel": "C_T", "ts": "4242.1"}


class PipelineExtraTest(unittest.TestCase):
    def test_hook_concurrent_race_single_post(self):
        """Two hook workers racing deliver_one: exactly one POST."""
        with Harness(ok_result(), ok_result()) as h:
            results = []
            def run():
                results.append(h.deliver())
            ts = [threading.Thread(target=run) for _ in range(2)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(len(h.posts), 1)
            self.assertIn("replied", results)

    def test_crash_after_send_never_resends_text(self):
        """Send OK then crash before ack -> next attempt is ACK-only."""
        with Harness(ok_result()) as h:
            self.assertEqual(h.deliver(), "replied")
            self.assertEqual(len(h.posts), 1)
            # simulate crash: rebuild the "sent but never acked" state
            st = h.state()
            st.claim("C_T:42", channel="C_T", thread_ts="",
                     text_hash="x", client_msg_id="y")
            st.set_unacked("C_T:42")
            # next attempt must only retry ack, never POST again
            self.assertEqual(h.deliver(), "acked-later")
            self.assertEqual(len(h.posts), 1)
            self.assertTrue(h.acks, "ack should have been retried")

    def test_ack_only_path(self):
        """Pre-existing unacked entry -> ack recovered, no send."""
        with Harness() as h:
            st = h.state()
            st.claim("C_T:42", channel="C_T", thread_ts="",
                     text_hash="x", client_msg_id="y")
            st.set_unacked("C_T:42")
            self.assertEqual(h.deliver(), "acked-later")
            self.assertEqual(len(h.posts), 0)

    def test_rate_limit_wait_no_send_before_deadline(self):
        """retry_wait: silent before retry_at, exactly one send after."""
        err = SlackApiError(429, headers={"Retry-After": "60"})
        with Harness(err, ok_result()) as h:
            self.assertEqual(h.deliver(), "retry-deferred")
            self.assertEqual(len(h.posts), 1)  # the 429 attempt posted once
            h.now += 30  # still inside the wait window
            self.assertEqual(h.deliver(), "retry-deferred")
            self.assertEqual(len(h.posts), 1, "must not resend before retry_at")
            h.now += 40  # past the 60s deadline
            self.assertEqual(h.deliver(), "replied")
            self.assertEqual(len(h.posts), 2)

    def test_uncertain_never_resends(self):
        """Uncertain (e.g. timeout) -> held, never blindly re-POSTed."""
        with Harness(TimeoutError("lost")) as h:
            first = h.deliver()
            self.assertEqual(first, "uncertain-held")
            n = len(h.posts)
            # history cannot verify -> stays uncertain, no resend
            for _ in range(3):
                self.assertEqual(h.deliver(), "uncertain-held")
            self.assertEqual(len(h.posts), n)

    def test_thread_ts_propagated_to_send(self):
        """thread reply: thread_ts reaches the Slack POST kwargs."""
        with Harness(ok_result()) as h:
            self.assertEqual(h.deliver(thread_ts="4242.0"), "replied")
            self.assertEqual(len(h.posts), 1)
            _, kwargs = h.posts[0]
            self.assertEqual(kwargs.get("thread_ts"), "4242.0")

    def test_empty_thread_ts_not_sent(self):
        """Top-level reply: thread_ts is None (not a thread)."""
        with Harness(ok_result()) as h:
            self.assertEqual(h.deliver(thread_ts=""), "replied")
            _, kwargs = h.posts[0]
            self.assertIsNone(kwargs.get("thread_ts"))


class NetConfigTest(unittest.TestCase):
    """Empty-env proxy/CA precedence: .env > std env vars > None."""

    def run_cfg(self, env_file, environ):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text(env_file)
            dct = {}
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    dct[k.strip()] = v.strip()
            with patch.dict(os.environ, environ, clear=True):
                return net_config.read_proxy_config(dct)

    def test_dotenv_wins_over_environ(self):
        proxy, ca = self.run_cfg(
            "PROXY_URL=http://from-dotenv:3128\nCA_BUNDLE=/x/ca.pem\n",
            {"https_proxy": "http://from-env:3128",
             "SSL_CERT_FILE": "/y/ca.pem"})
        self.assertEqual(proxy, "http://from-dotenv:3128")
        self.assertEqual(ca, "/x/ca.pem")

    def test_stripped_env_falls_back_to_std_vars(self):
        proxy, ca = self.run_cfg(
            "SLACK_BOT_TOKEN=<redacted>",
            {"https_proxy": "http://from-env:3128",
             "SSL_CERT_FILE": "/y/ca.pem"})
        self.assertEqual(proxy, "http://from-env:3128")
        self.assertEqual(ca, "/y/ca.pem")

    def test_fully_empty_means_direct(self):
        proxy, ca = self.run_cfg("SLACK_BOT_TOKEN=<redacted>", {})
        self.assertIsNone(proxy)
        self.assertIsNone(ca)

    def test_no_secrets_or_intranet_in_repo(self):
        src = (ROOT / "net_config.py").read_text()
        for bad in ("hatch-egress-proxy", "xoxb-", "xapp-"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
