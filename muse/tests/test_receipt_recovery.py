"""Attempt receipts after response loss; fake Slack, real send/durable/history code."""
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "consumer"))
import channel_history as history
import poll_consumer as pc
import send_durable
from send_receipt import EVENT_TYPE, attempt_metadata
from test_send_durable import DurableFlow

REAL_HISTORY = pc.channel_history


class ReceiptRecoveryTest(unittest.TestCase):
    def candidate(self, **changes):
        message = {"channel": "C_TEST", "ts": "1001", "is_bot": True,
                   "bot_id": "B_TEST", "user": "U_TEST", "thread_ts": "",
                   "text_sha256": pc.text_hash("reply"),
                   "metadata": attempt_metadata("attempt")}
        message.update(changes)
        return message

    def verify(self, message, thread=""):
        with patch.object(pc, "channel_history", return_value=([message], None)):
            return pc.verify_sent("C_TEST", pc.text_hash("reply"), thread,
                                  "B_TEST", "U_TEST", 1000, client_msg_id="attempt")

    def test_exact_metadata_without_client_id_and_legacy_id(self):
        self.assertTrue(self.verify(self.candidate()))
        self.assertTrue(self.verify(self.candidate(metadata={}, client_msg_id="attempt")))

    def test_wrong_or_missing_receipt_and_other_boundaries_hold(self):
        for changes in (
            {"metadata": {}},
            {"metadata": {"event_type": "other", "event_payload": {"attempt_id": "attempt"}}},
            {"metadata": attempt_metadata("different")},
            {"metadata": {"event_type": EVENT_TYPE, "event_payload": "attempt"}},
            {"bot_id": "B_OTHER", "user": "U_OTHER"},
            {"channel": "C_OTHER"},
            {"thread_ts": "900"}, {"text_sha256": pc.text_hash("different")},
            {"ts": "900"}, {"is_bot": False},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.verify(self.candidate(**changes)))

    def test_root_that_gains_replies_remains_top_level(self):
        root = self.candidate(thread_ts="1001")
        self.assertTrue(self.verify(root))
        self.assertFalse(self.verify(root, thread="900"))

    def test_old_claim_without_attempt_id_never_guessed_from_metadata(self):
        with patch.object(pc, "channel_history", return_value=([self.candidate()], None)):
            self.assertFalse(pc.verify_sent("C_TEST", pc.text_hash("reply"),
                                            bot_id="B_TEST", client_msg_id=None))

    def install_history(self, flow, include_receipt=True, fail_tail=False, receipt_ts="1001"):
        """Drive actual history CLI through the same fake SDK as send.main."""
        client_class = sys.modules["slack_sdk.web"].WebClient
        def fetch(_client, **kwargs):
            if fail_tail and kwargs.get("cursor"):
                raise RuntimeError("history failed")
            if not kwargs.get("cursor"):
                return {"ok": True, "messages": [{"ts": str(1040-i), "text": "other"}
                                                  for i in range(30)],
                        "response_metadata": {"next_cursor": "tail"}}
            post = flow.posts[0][1]
            msg = {"ts": receipt_ts, "text": post["text"], "bot_id": "B_TEST", "user": "U_TEST"}
            if include_receipt:
                msg["metadata"] = post["metadata"]
            within_bounds = float(kwargs["oldest"]) <= float(receipt_ts) <= float(kwargs["latest"])
            return {"ok": True, "messages": [msg] if within_bounds else []}
        client_class.conversations_history = fetch
        client_class.users_info = lambda _client, **kwargs: {"user": {"profile": {}}}
        original_dispatch = pc.sh.side_effect
        def dispatch(*args, input_text=None, timeout=None):
            if args[1] != "channel_history.py":
                return original_dispatch(*args, input_text=input_text, timeout=timeout)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = history.main(list(args[2:]))
            return type("Result", (), {"returncode": code, "stdout": out.getvalue(),
                                       "stderr": err.getvalue()})()
        return dispatch

    def run_receipt_flow(self, include_receipt=True, fail_tail=False, receipt_ts="1001"):
        with DurableFlow(RuntimeError("lost response BODY_SENTINEL xoxb-secret")) as flow:
            flow.now = 1045  # Fixed reconciliation upper bound includes the receipt.
            dispatch = self.install_history(flow, include_receipt, fail_tail, receipt_ts)
            with patch.object(pc, "channel_history", REAL_HISTORY), \
                 patch.object(pc, "sh", side_effect=dispatch), \
                 patch.object(history, "BASE", str(flow.base)), \
                 patch.object(history.time, "sleep"):
                first = flow.run("reply <@U9>\n```code```" + "long" * 1000)
                second = flow.run("must not send again")
            return first, second, len(flow.posts), flow.saved_state(), flow.inbox_text(), flow.posts

    def test_lost_response_receipt_after_first_30_acks_once(self):
        first, second, count, state, inbox, posts = self.run_receipt_flow()
        self.assertEqual((first[0], second[0], count), (0, 0, 1))
        self.assertIn("verified-acked", first[1])
        self.assertEqual(inbox.count('"type": "ack"'), 1)
        post = posts[0][1]
        self.assertEqual(post["metadata"], attempt_metadata(post["client_msg_id"]))
        self.assertRegex(post["client_msg_id"], r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
        self.assertNotIn("blocks", post)
        self.assertIn("<@U9>\n```code```", post["text"])

    def test_missing_receipt_or_page_failure_holds_and_keeps_safe_evidence(self):
        for include, fail in ((False, False), (True, True)):
            with self.subTest(include=include, fail=fail):
                first, second, count, state, inbox, _ = self.run_receipt_flow(include, fail)
                self.assertEqual((first[0], second[0], count), (2, 2, 1))
                entry = state["sends"]["C_TEST:1000"]
                self.assertEqual(entry["status"], "uncertain")
                self.assertEqual(entry["send_diagnostic"]["exception_type"], "RuntimeError")
                evidence = json.dumps(state) + first[2] + second[2]
                self.assertNotIn("BODY_SENTINEL", evidence)
                self.assertNotIn("xoxb-secret", evidence)
                self.assertNotIn('"type": "ack"', inbox)

    def test_slack_clock_ahead_still_reconciles_within_skew(self):
        first, second, count, _, _, _ = self.run_receipt_flow(receipt_ts="1075")
        self.assertEqual((first[0], second[0], count), (0, 0, 1))

    def test_arbitrary_child_output_never_reaches_diagnostic(self):
        raw = "BODY_SENTINEL xoxb-secret https://user:password@proxy.invalid"
        diagnostic = {"exception_type": "RuntimeError", "detail": raw,
                      "token": raw, "slack_error": raw, "warnings": [raw]}
        child = types.SimpleNamespace(returncode=2, stdout=raw,
                                      stderr="SEND_DIAGNOSTIC " + json.dumps(diagnostic) + "\n" + raw)
        saved = {}
        with patch.object(pc, "sh", return_value=child), \
             contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(pc.send_reply("C_TEST", raw, diagnostics=saved), "uncertain")
        evidence = json.dumps(saved) + output.getvalue()
        self.assertNotIn("BODY_SENTINEL", evidence)
        self.assertNotIn("xoxb-secret", evidence)
        self.assertNotIn("proxy.invalid", evidence)
        self.assertEqual(saved["exception_type"], "RuntimeError")
        self.assertEqual(saved["exit_code"], 2)

    def test_identity_fetch_failure_is_not_cached(self):
        client = Mock()
        client.auth_test.side_effect = [RuntimeError("BODY_SENTINEL xoxb-secret"),
                                        {"user_id": "U_TEST"}]
        web = types.ModuleType("slack_sdk.web")
        web.WebClient = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("SLACK_BOT_TOKEN=test\n")
            with patch.object(pc, "ROOT", tmp), patch.object(pc, "_bot_identity", None), \
                 patch.dict(sys.modules, {"slack_sdk.web": web}), \
                 contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(pc.resolve_bot_identity(), (None, None))
                self.assertEqual(pc.resolve_bot_identity(), (None, "U_TEST"))
                self.assertEqual(client.auth_test.call_count, 2)
            self.assertNotIn("BODY_SENTINEL", output.getvalue())

    def test_poll_loop_retries_startup_identity_before_later_send(self):
        messages = [{"msg_id": "C_TEST:1000", "channel": "C_TEST", "text": "hi"}]
        with patch.object(pc, "SendState", return_value=Mock()), \
             patch.object(pc, "resolve_bot_identity", side_effect=[(None, None), ("B_TEST", "U_TEST")]) as resolve, \
             patch.object(pc, "peek", return_value=messages), \
             patch.object(pc, "load_json", return_value={}), patch.object(pc, "save_json"), \
             patch.object(pc, "handle_one", return_value="replied") as handle, \
             patch.object(pc.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                pc.main()
        self.assertEqual(resolve.call_count, 2)
        self.assertIsNone(handle.call_args_list[0].kwargs["bot_id"])
        self.assertEqual(handle.call_args_list[1].kwargs["bot_id"], "B_TEST")

    def test_metadata_is_requested_and_preserved_in_bounded_pages(self):
        client = Mock()
        client.conversations_history.return_value = {"messages": [{"ts": "1001", "metadata": attempt_metadata("attempt")}]}
        messages, _ = history.fetch_messages(client, "C_TEST", all_pages=True,
                                             oldest="940", latest="1045", include_all_metadata=True)
        client.conversations_history.assert_called_once_with(channel="C_TEST", limit=10,
                                                              oldest="940", latest="1045", inclusive=True,
                                                              include_all_metadata=True)
        self.assertEqual(history.message_record(client, messages[0], "C_TEST", {})["metadata"],
                         attempt_metadata("attempt"))

    def test_identity_failure_before_claim_or_post(self):
        with DurableFlow({"ok": True, "ts": "1001"}) as flow, \
             patch.object(send_durable, "resolve_bot_identity", return_value=(None, None)):
            code, _, err = flow.run()
            self.assertEqual(code, 4)
            self.assertEqual(flow.posts, [])
            self.assertIn("identity unavailable", err)
            self.assertFalse((flow.base / "send_state.json").exists())


if __name__ == "__main__":
    unittest.main()
