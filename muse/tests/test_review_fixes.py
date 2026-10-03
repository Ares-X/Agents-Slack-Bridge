"""Regression tests for PR #11 review findings (Codex).

1. ACK consecutive failure must not report success: send ok + ack fail -> 3;
   second attempt ack still fails -> still 3 (not 0); recovery -> 0;
   no duplicate POST throughout.
2. Uncertain recovery target must not drift: verification uses the send
   attempt's persisted channel/thread_ts, never the current CLI args.
   Both directions: thread send recovered without --thread-ts, and
   top-level send recovered with a thread_ts arg.
"""
import contextlib
import io
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

import inbox_store
import poll_consumer as pc
import send
from send_state import SendState


class Harness:
    def __init__(self, *behaviors):
        self.behaviors = list(behaviors)
        self.now = 7000.0
        self.posts = []
        self.history = []  # faked channel_history messages

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
            # inbox_ack.py: real ack not needed; _ack_safe is patched per-test
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
            patch.object(pc, "channel_history",
                         side_effect=lambda *a, **k: (list(flow.history), None)),
        ):
            self.stack.enter_context(context)
        inbox_store.append_record({"msg_id": "C_R:7", "channel": "C_R",
                                   "ts": "7", "text": "ping"})
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def state(self):
        return SendState(str(self.base / "send_state.json"),
                         inbox_path=str(self.base / "inbox.jsonl"))

    def deliver(self, thread_ts=""):
        m = {"msg_id": "C_R:7", "channel": "C_R", "thread_ts": thread_ts}
        return pc.deliver_one(m, "reply text", [], self.state(), "B_T", "U_T")

    def entry(self):
        return self.state().get("C_R:7")


def ok_result():
    return {"ok": True, "channel": "C_R", "ts": "7777.1"}


def history_msg(thread_ts, text_hash, client_msg_id, claimed_at):
    return {"is_bot": True, "bot_id": "B_T", "user": "U_T",
            "channel": "C_R", "thread_ts": thread_ts,
            "ts": str(claimed_at + 5.0), "text_sha256": text_hash,
            "client_msg_id": client_msg_id}


class AckFailureTest(unittest.TestCase):
    def test_consecutive_ack_failure_stays_unacked_then_recovers(self):
        with Harness(ok_result()) as h:
            st = h.state()
            # simulate: send ok, ack failed -> unacked
            st.claim("C_R:7", channel="C_R", thread_ts="",
                     text_hash="h", client_msg_id="c")
            st.set_unacked("C_R:7")
            with patch.object(pc, "_ack_safe", return_value=False):
                # first retry: ack still fails -> must stay 3, not 0
                self.assertEqual(h.deliver(), "sent-unacked")
                self.assertEqual(h.state().get("C_R:7")["status"], "unacked")
                # second retry: ack still fails -> still 3
                self.assertEqual(h.deliver(), "sent-unacked")
                self.assertEqual(h.state().get("C_R:7")["status"], "unacked")
            self.assertEqual(len(h.posts), 0, "no POST on ack-only retries")
            with patch.object(pc, "_ack_safe", return_value=True):
                # recovery: ack finally succeeds -> 0
                self.assertEqual(h.deliver(), "acked-later")
            self.assertEqual(len(h.posts), 0, "no duplicate POST on recovery")
            self.assertIsNone(h.state().get("C_R:7"))


class UncertainDriftTest(unittest.TestCase):
    def test_thread_send_recovered_without_thread_arg(self):
        """First send --thread-ts 900, response lost; retry omits the flag:
        verification must still target thread 900 (persisted)."""
        with Harness(TimeoutError("lost")) as h:
            self.assertEqual(h.deliver(thread_ts="900"), "uncertain-held")
            e = h.entry()
            self.assertEqual(e["thread_ts"], "900")
            h.history.append(history_msg("900", e["text_hash"],
                                         e["client_msg_id"],
                                         e["claimed_at"]))
            with patch.object(pc, "_ack_safe", return_value=True):
                # CLI omits --thread-ts this time; must still verify vs 900
                self.assertEqual(h.deliver(thread_ts=""), "verified-acked")
            self.assertEqual(len(h.posts), 1, "no resend during verify")

    def test_toplevel_send_recovered_with_thread_arg(self):
        """First send top-level, response lost; poll recovers with an inbox
        thread_ts arg: verification must still target top-level (\"\")."""
        with Harness(TimeoutError("lost")) as h:
            self.assertEqual(h.deliver(thread_ts=""), "uncertain-held")
            e = h.entry()
            self.assertEqual(e["thread_ts"], "")
            h.history.append(history_msg("", e["text_hash"],
                                         e["client_msg_id"],
                                         e["claimed_at"]))
            # 注意：不放 thread 900 的 decoy——旧代码按 CLI 的 900 去核验
            # 会找不到回执（uncertain-held），新代码按持久的 "" 才能确认。
            with patch.object(pc, "_ack_safe", return_value=True):
                self.assertEqual(h.deliver(thread_ts="900"), "verified-acked")
            self.assertEqual(len(h.posts), 1, "no resend during verify")


if __name__ == "__main__":
    unittest.main()
