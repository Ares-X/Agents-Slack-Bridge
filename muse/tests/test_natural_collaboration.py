"""Complete context and deliberate quiet completion; no network or real state."""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "consumer"))
import channel_history as history
import inbox_store
import poll_consumer as pc
import send_durable
from send_state import SendState


class HistoryTest(unittest.TestCase):
    def test_channel_pagination_dedups_and_orders_complete_text(self):
        long_text = "完整原文" * 300
        client = Mock()
        client.conversations_history.side_effect = [
            {"messages": [{"ts": "10.0", "text": long_text, "user": "U1",
                           "bot_id": "B1", "client_msg_id": "attempt"},
                          {"ts": "9.0", "text": "second"}],
             "has_more": True, "response_metadata": {"next_cursor": "next"}},
            {"messages": [{"ts": "9.0", "text": "duplicate"},
                          {"ts": "2.0", "text": "first"}]}]
        client.users_info.return_value = {"user": {"profile": {"display_name": "Muse"}}}
        messages, cursor = history.fetch_messages(client, "C1", 2, all_pages=True)
        self.assertEqual([m["ts"] for m in messages], ["2.0", "9.0", "10.0"])
        self.assertEqual(cursor, "")
        self.assertEqual(client.conversations_history.call_args.kwargs["cursor"], "next")
        record = history.message_record(client, messages[-1], "C1", {})
        self.assertEqual(record["text"], long_text)
        self.assertEqual(record["text_sha256"], hashlib.sha256(long_text.encode()).hexdigest())
        self.assertEqual((record["channel"], record["user"], record["bot_id"],
                          record["client_msg_id"]), ("C1", "U1", "B1", "attempt"))

    def test_thread_pagination_preserves_card_and_reply_context(self):
        client = Mock()
        card = {"ts": "1.0", "blocks": [{"type": "actions"}],
                "text": "Approved once", "edited": {"ts": "2.0"}}
        client.conversations_replies.side_effect = [
            {"messages": [card], "response_metadata": {"next_cursor": "tail"}},
            {"messages": [{"ts": "3.0", "thread_ts": "1.0", "text": "reply"}]}]
        messages, _ = history.fetch_messages(client, "C1", 1, "1.0", True)
        client.conversations_history.assert_not_called()
        self.assertEqual(client.conversations_replies.call_args.kwargs["ts"], "1.0")
        self.assertEqual(history.message_record(client, messages[0], "C1", {})["blocks"],
                         card["blocks"])
        self.assertEqual(history.message_record(client, messages[1], "C1", {})["thread_ts"], "1.0")

    def test_bounded_history_returns_cursor_for_continuation(self):
        client = Mock()
        client.conversations_history.return_value = {
            "messages": [{"ts": "1.0"}], "response_metadata": {"next_cursor": "tail"}}
        messages, cursor = history.fetch_messages(client, "C1", 1, cursor="start")
        self.assertEqual(len(messages), 1)
        self.assertEqual(cursor, "tail")
        self.assertEqual(client.conversations_history.call_args.kwargs["cursor"], "start")

    def test_repeated_cursor_and_missing_cursor_fail(self):
        for response in (
            {"messages": [], "response_metadata": {"next_cursor": "same"}},
            {"messages": [], "has_more": True},
        ):
            with self.subTest(response=response):
                client = Mock()
                client.conversations_history.return_value = response
                with self.assertRaises(ValueError):
                    history.fetch_messages(client, "C1", all_pages=True, cursor="same")

    def test_later_page_failure_cli_emits_only_explicit_error(self):
        client = Mock()
        client.conversations_history.side_effect = [
            {"messages": [{"ts": "1.0", "text": "must not leak partial history"}],
             "response_metadata": {"next_cursor": "next"}},
            RuntimeError("unavailable"), RuntimeError("unavailable"), RuntimeError("unavailable")]
        web = types.ModuleType("slack_sdk.web")
        web.WebClient = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as tmp, patch.object(history, "BASE", tmp), \
             patch.dict(sys.modules, {"slack_sdk.web": web}), \
             patch.dict(os.environ, {"SLACK_BOT_TOKEN": "test"}), \
             patch.object(history.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as out:
            code = history.main(["C1", "--all"])
        self.assertEqual(code, 2)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertIn("unavailable", json.loads(out.getvalue())["reason"])
        self.assertNotIn("must not leak", out.getvalue())

    def test_thread_alias_uses_replies_and_legacy_positional_limit(self):
        client = Mock()
        client.conversations_replies.return_value = {"messages": []}
        web = types.ModuleType("slack_sdk.web")
        web.WebClient = Mock(return_value=client)
        with tempfile.TemporaryDirectory() as tmp, patch.object(history, "BASE", tmp), \
             patch.dict(sys.modules, {"slack_sdk.web": web}), \
             patch.dict(os.environ, {"SLACK_BOT_TOKEN": "test"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(history.main(["C1", "15", "--thread", "1.0"]), 0)
        client.conversations_replies.assert_called_once_with(channel="C1", limit=15, ts="1.0")

    def test_verification_queries_persisted_thread(self):
        with patch.object(pc, "channel_history", return_value=([], None)) as fetch:
            self.assertFalse(pc.verify_sent("C1", "hash", "1.0", client_msg_id="attempt"))
        fetch.assert_called_once_with("C1", limit=30, thread_ts="1.0", reconcile_after=0.0)


class QuietCompletionTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for target, name, value in (
            (inbox_store, "BASE", str(self.base)),
            (inbox_store, "INBOX_PATH", str(self.base / "inbox.jsonl")),
            (inbox_store, "LOCK_PATH", str(self.base / "inbox.lock")),
            (send_durable, "STATE_PATH", str(self.base / "send_state.json")),
            (send_durable, "INBOX_PATH", str(self.base / "inbox.jsonl")),
            (send_durable, "INBOX_LOCK_PATH", str(self.base / "inbox.lock")),
        ):
            self.stack.enter_context(patch.object(target, name, value))
        self.stack.enter_context(patch.dict(inbox_store._MIGRATED, {}, clear=True))
        self.identity = self.stack.enter_context(patch.object(send_durable, "resolve_bot_identity"))
        self.delivery = self.stack.enter_context(patch.object(send_durable, "deliver_one"))
        inbox_store.append_record({"msg_id": "C1:1.0", "channel": "C1", "ts": "1.0",
                                   "user": "U1", "thread_ts": "0.5", "text": "控制卡完整正文"})

    def state(self):
        return SendState(str(self.base / "send_state.json"),
                         inbox_path=inbox_store.INBOX_PATH,
                         inbox_lock_path=inbox_store.LOCK_PATH, recover_sending=False)

    def run_quiet(self, msg_id="C1:1.0", args=None):
        if args is None:
            args = ["--no-reply", "--reason", "control ACK: no action needed"]
        stdin = Mock()
        stdin.read.side_effect = AssertionError("quiet must not read stdin")
        with patch.object(sys, "argv", ["send_durable.py", msg_id] + args), \
             patch.object(sys, "stdin", stdin), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err:
            code = send_durable.main()
        self.identity.assert_not_called()
        self.delivery.assert_not_called()
        return code, out.getvalue(), err.getvalue()

    def records(self):
        return [json.loads(line) for line in (self.base / "inbox.jsonl").read_text().splitlines()]

    def test_quiet_is_durable_idempotent_preserves_evidence_and_compaction(self):
        self.assertEqual(self.run_quiet()[0], 0)
        record = next(r for r in self.records() if r.get("type") == "ack")
        self.assertEqual(record["disposition"], "no-reply")
        self.assertEqual(record["reason"], "control ACK: no action needed")
        self.assertEqual(record["source"]["text"], "控制卡完整正文")
        self.assertEqual(record["source"]["thread_ts"], "0.5")
        self.assertEqual(inbox_store.read_undelivered(), [])
        with patch.object(inbox_store, "_fsync_data_file", wraps=inbox_store._fsync_data_file) as sync:
            self.assertEqual(self.run_quiet(args=["--no-reply", "--reason", "new reason"])[0], 0)
            sync.assert_called_once()
        self.assertEqual(sum(r.get("type") == "ack" for r in self.records()), 1)
        inbox_store.compact(now=record["at"] + 1)
        self.assertIn(record, self.records())
        self.assertEqual(self.run_quiet()[0], 0)  # original body now compacted
        inbox_store.compact(now=record["at"] + inbox_store.TOMBSTONE_RETENTION_SECONDS + 1)
        self.assertNotIn(record, self.records())

    def test_all_existing_send_states_refuse_quiet_even_expired_retry(self):
        state = self.state()
        state.claim("C1:1.0", "C1", "", "hash", "attempt")
        for status, code in (("sending", 2), ("uncertain", 2), ("unacked", 3), ("retry_wait", 75)):
            with self.subTest(status=status):
                saved = json.loads((self.base / "send_state.json").read_text())
                saved["sends"]["C1:1.0"]["status"] = status
                if status == "retry_wait":
                    saved["sends"]["C1:1.0"]["retry_at"] = 0
                (self.base / "send_state.json").write_text(json.dumps(saved))
                before = (self.base / "send_state.json").read_bytes()
                self.assertEqual(self.run_quiet()[0], code)
                self.assertEqual((self.base / "send_state.json").read_bytes(), before)
                self.assertFalse(any(r.get("type") == "ack" for r in self.records()))

    def test_quiet_does_not_recover_unrelated_live_sending(self):
        state = self.state()
        state.claim("C1:other", "C1", "", "hash", "other-attempt")
        before = (self.base / "send_state.json").read_bytes()
        self.assertEqual(self.run_quiet()[0], 0)
        self.assertEqual((self.base / "send_state.json").read_bytes(), before)
        self.assertEqual(state.get("C1:other")["status"], "sending")

    def test_missing_unknown_corrupt_sources_never_complete(self):
        self.assertEqual(self.run_quiet("C1:unknown")[0], 4)
        self.assertEqual(len(inbox_store.read_undelivered()), 1)
        with open(inbox_store.INBOX_PATH, "a") as f:
            f.write("invalid JSON\n")
        self.assertEqual(self.run_quiet()[0], 4)
        self.assertFalse(any('"type": "ack"' in l for l in Path(inbox_store.INBOX_PATH).read_text().splitlines()))
        os.unlink(inbox_store.INBOX_PATH)
        self.assertEqual(self.run_quiet()[0], 4)

    def test_reason_and_send_options_validated(self):
        for args in (["--no-reply"], ["--no-reply", "--reason", "   "],
                     ["--no-reply", "--reason", "control", "--mention", "U1"],
                     ["--no-reply", "--reason", "control", "--thread-ts", "0.5"],
                     ["--reason", "control"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_quiet(args=args)[0], 1)
        self.assertEqual(len(inbox_store.read_undelivered()), 1)

    def test_source_identity_conflict_refuses_completion_and_preserves_record(self):
        for key, value in (("channel", "C_OTHER"), ("ts", "2.0")):
            with self.subTest(key=key):
                source = {"msg_id": "C1:1.0", "channel": "C1", "ts": "1.0",
                          "text": "source evidence"}
                source[key] = value
                raw = json.dumps(source, ensure_ascii=False) + "\n"
                Path(inbox_store.INBOX_PATH).write_text(raw)
                self.assertEqual(self.run_quiet()[0], 4)
                self.assertEqual(Path(inbox_store.INBOX_PATH).read_text(), raw)
                self.assertEqual(inbox_store.read_undelivered(), [source])

    def test_fsync_failure_never_reports_success_repeat_confirms(self):
        with patch.object(inbox_store, "_fsync_dir", side_effect=OSError("disk failure")):
            self.assertEqual(self.run_quiet()[0], 4)
        self.assertEqual(self.run_quiet()[0], 0)
        with patch.object(inbox_store, "_fsync_dir", side_effect=OSError("disk failure")):
            self.assertEqual(self.run_quiet()[0], 4)
        self.assertEqual(self.run_quiet()[0], 0)
        self.assertEqual(sum(r.get("type") == "ack" for r in self.records()), 1)

    def test_state_corruption_fail_closed(self):
        (self.base / "send_state.json").write_text('{"v":99}')
        self.assertEqual(self.run_quiet()[0], 4)
        self.assertEqual(len(inbox_store.read_undelivered()), 1)

    def test_file_fsync_failure_requires_successful_confirmation(self):
        with patch.object(inbox_store.os, "fsync", side_effect=OSError("file sync failed")):
            self.assertEqual(self.run_quiet()[0], 4)
        self.assertEqual(self.run_quiet()[0], 0)
        with patch.object(inbox_store.os, "fsync", side_effect=OSError("file sync failed")):
            self.assertEqual(self.run_quiet()[0], 4)
        self.assertEqual(self.run_quiet()[0], 0)
        self.assertEqual(sum(r.get("type") == "ack" for r in self.records()), 1)

    def test_existing_ack_is_confirmed_without_adding_quiet_reason(self):
        inbox_store.ack(["C1:1.0"])
        self.assertEqual(self.run_quiet()[0], 0)
        self.assertEqual(sum(r.get("type") == "ack" for r in self.records()), 1)
        self.assertFalse(any(r.get("disposition") for r in self.records()))

    def test_claim_and_quiet_are_mutually_exclusive(self):
        state = self.state()
        with ThreadPoolExecutor(max_workers=2) as pool:
            send = pool.submit(state.claim, "C1:1.0", "C1", "", "hash", "attempt")
            quiet = pool.submit(state.complete_no_reply, "C1:1.0", "control", inbox_store.complete_no_reply)
            outcome, disposition = send.result()[1], quiet.result()
        self.assertIn((outcome, disposition), (("claimed", "held-sending"), ("completed", "no-reply")))
        if outcome == "claimed":
            self.assertEqual(len(inbox_store.read_undelivered()), 1)
        else:
            self.assertEqual(inbox_store.read_undelivered(), [])


if __name__ == "__main__":
    unittest.main()
