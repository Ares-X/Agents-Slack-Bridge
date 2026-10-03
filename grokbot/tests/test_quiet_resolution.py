"""Reasoned quiet consumption, send/ACK races and durability (no live Slack)."""
import os
import sys
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import inbox_store as store
import pending_consume_once as pending
import reply_pipeline as pipeline


class TestQuietResolution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inbox = os.path.join(self.temp.name, "inbox.jsonl")

    def add(self, ts="1", channel="C", status=None, **extra):
        row = dict(channel=channel, ts=ts, user="U_PEER", text="original event",
                   delivered=False, reply_status=status, thread_ts="root")
        row.update(extra)
        store.append_record(self.inbox, row)
        return row

    def rows(self):
        return store.read_records_readonly(self.inbox)

    def quiet(self, ts="1", reason="notification already incorporated"):
        return pending.consume_one(self.inbox, "C", ts, "", no_reply=True, reason=reason)

    def test_reason_and_event_survive_quiet_resolution_and_retry(self):
        original = self.add()
        self.add(channel="OTHER")
        result = self.quiet()
        self.assertEqual(result["outcome"], "resolved_no_reply")
        resolved = self.rows()[0]
        self.assertTrue(resolved["delivered"])
        self.assertEqual(resolved["reply_status"], "no_reply")
        self.assertEqual(resolved["resolution_reason"], "notification already incorporated")
        self.assertGreater(resolved["resolved_at"], 0)
        for key in ("text", "user", "channel", "ts", "thread_ts"):
            self.assertEqual(resolved[key], original[key])
        self.assertEqual([r["channel"] for r in store.peek_undelivered(self.inbox)], ["OTHER"])
        self.quiet(reason="different later explanation")
        self.assertEqual(self.rows()[0], resolved)

    def test_notifications_then_current_task_send_once(self):
        self.add("1", text="approval notification")
        self.add("2", text="earlier charter superseded by task")
        current = self.add("3", text="current authorized task")
        calls = []
        def send(*args, **kwargs):
            calls.append(kwargs["input_text"])
            return types.SimpleNamespace(returncode=0, stdout="sent ok: True ts: 10", stderr="")
        self.quiet("1", reason="notification; no action requested")
        self.quiet("2", reason="incorporated into current authorized task at C:3")
        result = pending.consume_one(self.inbox, "C", "3", "substantive contribution", runner=send)
        self.assertEqual(result["outcome"], "sent_acked")
        # A stale event/snapshot cannot send again after either decision.
        retry = pipeline.process_one(self.inbox, current, "duplicate contribution", runner=send)
        self.assertEqual(retry["outcome"], "claim_lost")
        self.assertEqual(calls, ["substantive contribution"])
        self.assertEqual(store.peek_undelivered(self.inbox), [])

    def test_missing_reason_or_mixed_decision_cannot_consume(self):
        self.add()
        for text, quiet, reason in [("", True, " "), ("reply", True, "reason"),
                                    ("reply", False, "quiet reason")]:
            with self.subTest(text=text, quiet=quiet), self.assertRaises(ValueError):
                pending.consume_one(self.inbox, "C", "1", text, no_reply=quiet, reason=reason)
        self.assertFalse(self.rows()[0]["delivered"])

    def test_quiet_and_raw_ack_preserve_protected_states(self):
        for i, status in enumerate([None, "", "retryable", "sending", "sent", "uncertain",
                                    "rate_limited", "unknown"]):
            ts = str(i)
            self.add(ts, status=status, retry_after_until=0)
            if status not in store.STATUS_CLAIMABLE:
                self.assertEqual(self.quiet(ts)["outcome"], "not_claimable")
            if status != "sent":
                self.assertEqual(store.ack_keys(self.inbox, {("C", ts)}), (0, {("C", ts)}))
        self.assertTrue(all(not row["delivered"] for row in self.rows()))

    def test_send_claim_wins_quiet_during_actual_send(self):
        row = self.add()
        calls = []
        def send(*args, **kwargs):
            calls.append(1)
            self.assertEqual(self.quiet()["outcome"], "not_claimable")
            self.assertEqual(store.ack_keys(self.inbox, {("C", "1")}), (0, {("C", "1")}))
            self.assertFalse(self.rows()[0]["delivered"])
            return types.SimpleNamespace(returncode=0, stdout="sent ok: True ts: 10", stderr="")
        self.assertEqual(pipeline.process_one(self.inbox, row, "reply", runner=send)["outcome"],
                         "sent_acked")
        self.assertEqual(calls, [1])
        self.assertNotIn("resolution_reason", self.rows()[0])

    def test_quiet_wins_stale_send_snapshot(self):
        row = self.add()
        self.quiet()
        send = mock.Mock()
        self.assertEqual(pipeline.process_one(self.inbox, row, "reply", runner=send)["outcome"],
                         "claim_lost")
        send.assert_not_called()

    def test_simultaneous_send_and_quiet_have_only_one_winner(self):
        for i in range(12):
            ts = str(i)
            self.add(ts)
            barrier = threading.Barrier(2)
            def claim():
                barrier.wait(timeout=5)
                return store.claim_for_send(self.inbox, ("C", ts))
            def quiet():
                barrier.wait(timeout=5)
                return self.quiet(ts)["outcome"] == "resolved_no_reply"
            with ThreadPoolExecutor(max_workers=2) as executor:
                sent = executor.submit(claim)
                resolved = executor.submit(quiet)
                self.assertEqual(int(sent.result()) + int(resolved.result()), 1)
            stored = next(r for r in self.rows() if r["ts"] == ts)
            self.assertIn(stored["reply_status"], ("sending", "no_reply"))

    def test_quiet_write_failure_keeps_work_and_does_not_succeed(self):
        self.add()
        with mock.patch.object(store, "_atomic_rewrite", side_effect=store.DurabilityError("EIO")):
            with self.assertRaises(store.DurabilityError):
                self.quiet()
        self.assertFalse(self.rows()[0]["delivered"])
        self.assertNotIn("resolution_reason", self.rows()[0])

    def test_retry_after_rename_fsync_failure_reconfirms_durability(self):
        self.add()
        with mock.patch.object(store, "_fsync_dir", side_effect=OSError("directory EIO")):
            with self.assertRaises(store.DurabilityError):
                self.quiet()
        resolved = self.rows()[0]
        self.assertEqual(resolved["reply_status"], "no_reply")
        with mock.patch.object(store, "fsync_file_and_dir", side_effect=store.DurabilityError("EIO")):
            with self.assertRaises(store.DurabilityError):
                self.quiet()
        self.assertEqual(self.quiet()["outcome"], "resolved_no_reply")
        self.assertEqual(self.rows()[0], resolved)

    def test_cli_is_not_a_restart_and_list_does_not_change_sending(self):
        self.add(status="sending", claim_id="live-owner")
        with mock.patch.object(pending, "INBOX", self.inbox), mock.patch("builtins.print"):
            self.assertEqual(pending.main(["--list"]), 0)
            self.assertEqual(pending.main(["C", "1", "--no-reply", "--reason", "notification"]), 1)
        self.assertEqual(self.rows()[0]["reply_status"], "sending")
        self.assertEqual(self.rows()[0]["claim_id"], "live-owner")

    def test_confirmed_send_ack_only_still_works(self):
        self.add(status="sent")
        send = mock.Mock()
        self.assertEqual(pending.consume_one(self.inbox, "C", "1", "", runner=send)["outcome"], "acked")
        send.assert_not_called()

    def test_confirmed_send_resolves_intervening_uncertainty_but_keeps_cause(self):
        row = self.add()
        def send(*args, **kwargs):
            store.set_reply_status(self.inbox, {("C", "1")}, "uncertain",
                                   extra_fields={"uncertain_reason": "old_cli_restart_marker"})
            return types.SimpleNamespace(returncode=0, stdout="sent ok: True ts: 10", stderr="")
        self.assertEqual(pipeline.process_one(self.inbox, row, "reply", runner=send)["outcome"],
                         "sent_acked")
        resolved = self.rows()[0]
        self.assertEqual(resolved["reply_status"], "sent")
        self.assertNotIn("uncertain_reason", resolved)
        self.assertEqual(resolved["resolved_uncertain_reason"], "old_cli_restart_marker")


if __name__ == "__main__":
    unittest.main()
