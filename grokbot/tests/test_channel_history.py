"""Channel/thread context, pagination, full bodies and credential-free failure."""
import contextlib
import io
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

import channel_history as history


class TestChannelHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = mock.patch.object(history, "BASE", self.temp.name)
        self.base.start()
        self.addCleanup(self.base.stop)
        self.client = mock.Mock()
        self.client.users_info.return_value = {"user": {"profile": {"display_name": "Peer"}}}

    def invoke(self, args, credentials=True):
        if credentials:
            with open(os.path.join(self.temp.name, ".env"), "w") as env:
                env.write("SLACK_BOT_TOKEN=synthetic-offline-fixture\n")
        constructor = mock.Mock(return_value=self.client)
        sdk = types.ModuleType("slack_sdk")
        web = types.ModuleType("slack_sdk.web")
        web.WebClient = constructor
        sdk.web = web
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"slack_sdk": sdk, "slack_sdk.web": web}), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = history.main(args)
        return rc, [json.loads(line) for line in stdout.getvalue().splitlines()], constructor

    def test_missing_credentials_needs_no_sdk_and_makes_no_call(self):
        stdout = io.StringIO()
        with mock.patch.dict(sys.modules, {"slack_sdk": None, "slack_sdk.web": None}), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(history.main(["C"]), 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"error": "missing SLACK_BOT_TOKEN"})
        self.client.conversations_history.assert_not_called()

    def test_legacy_channel_args_keep_chronology_and_full_text(self):
        text = "a" * 900 + "new task restriction after the old cutoff"
        self.client.conversations_history.return_value = {"messages": [
            {"user": "U1", "ts": "2", "thread_ts": "root", "text": text},
            {"user": "U1", "ts": "1", "text": "task"},
        ]}
        rc, rows, constructor = self.invoke(["C", "15"])
        self.assertEqual(rc, 0)
        self.assertEqual([row["ts"] for row in rows], ["1", "2"])
        self.assertEqual(rows[1]["text"], text)
        self.assertEqual(rows[1]["thread_ts"], "root")
        self.client.conversations_history.assert_called_once_with(channel="C", limit=15)
        self.client.conversations_replies.assert_not_called()
        self.assertEqual(constructor.call_count, 1)

    def test_thread_aliases_use_replies_and_keep_later_constraints(self):
        for flag in ("--thread-ts", "--thread"):
            with self.subTest(flag=flag):
                self.client.reset_mock()
                self.client.conversations_replies.side_effect = [
                    {"messages": [{"ts": "1", "text": "original task"},
                                  {"ts": "2", "text": "peer contribution"}],
                     "has_more": True, "response_metadata": {"next_cursor": "NEXT"}},
                    {"messages": [{"ts": "2", "text": "peer contribution"},
                                  {"ts": "3", "text": "later human constraint"}]},
                ]
                rc, rows, _ = self.invoke(["C", "2", flag, "1", "--all"])
                self.assertEqual(rc, 0)
                self.assertEqual([r["text"] for r in rows],
                                 ["original task", "peer contribution", "later human constraint"])
                self.assertTrue(all(r["thread_ts"] == "1" for r in rows))
                self.assertEqual(self.client.conversations_replies.call_args_list, [
                    mock.call(channel="C", limit=2, ts="1"),
                    mock.call(channel="C", limit=2, ts="1", cursor="NEXT"),
                ])
                self.client.conversations_history.assert_not_called()

    def test_single_page_exposes_cursor_and_cursor_can_resume(self):
        self.client.conversations_history.return_value = {
            "messages": [{"ts": "3", "text": "recent"}],
            "response_metadata": {"next_cursor": "OLDER"}, "has_more": True,
        }
        rc, rows, _ = self.invoke(["C", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(rows[-1]["pagination"]["next_cursor"], "OLDER")
        self.assertFalse(rows[-1]["pagination"]["complete"])
        self.client.conversations_history.return_value = {"messages": [{"ts": "2", "text": "older"}]}
        rc, rows, _ = self.invoke(["C", "1", "--cursor", "OLDER"])
        self.assertEqual(rows[0]["text"], "older")
        self.client.conversations_history.assert_called_with(channel="C", limit=1, cursor="OLDER")

    def test_all_channel_pages_are_chronological(self):
        self.client.conversations_history.side_effect = [
            {"messages": [{"ts": "4"}, {"ts": "3"}],
             "response_metadata": {"next_cursor": "OLD"}},
            {"messages": [{"ts": "2"}, {"ts": "1"}]},
        ]
        rc, rows, _ = self.invoke(["C", "2", "--all"])
        self.assertEqual(rc, 0)
        self.assertEqual([r["ts"] for r in rows], ["1", "2", "3", "4"])

    def test_later_page_failure_cannot_claim_complete_context(self):
        self.client.conversations_replies.side_effect = [
            {"messages": [{"ts": "1"}], "response_metadata": {"next_cursor": "NEXT"}},
        ] + [OSError("thread read failed")] * 5
        with mock.patch.object(history.time, "sleep"):
            rc, rows, _ = self.invoke(["C", "1", "--thread", "1", "--all"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(rows), 1)
        self.assertIn("thread read failed", rows[0]["error"])

    def test_invalid_pagination_fails_instead_of_looping_or_hiding_context(self):
        for pages in [
            [{"messages": [], "has_more": True}],
            [{"messages": [], "response_metadata": {"next_cursor": "REPEAT"}}] * 2,
        ]:
            with self.subTest(pages=pages):
                self.client.conversations_history.side_effect = pages
                rc, rows, _ = self.invoke(["C", "1", "--all"])
                self.assertEqual(rc, 1)
                self.assertIn("history", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
