"""Handler-level tests for the thread-reply wake path.

Covers the full socket handler (not just the pure classifier):
  * parent check failure / empty result -> no ACK, redelivery retries
  * append / fsync failure -> no ACK, redelivery retries after recovery
  * crash before ACK is confirmed -> redelivery dedups, still one record
  * confirmed non-mine parent -> ACK + drop, redelivery also clean
  * bot thread replies never reach the parent check (no API call)

Run:  python -m unittest discover -s tests   (from muse/)
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import inbox_store
import bridge  # direct resolve_thread_reply tests; no I/O at import

ME = "U_SELF"
HUMAN = "U_HUMAN"


def thread_event(ts="2", **kw):
    e = {"type": "message", "channel": "C1", "user": HUMAN,
         "text": "reply", "ts": ts, "thread_ts": "1"}
    e.update(kw)
    return e


def parent_msg(user=ME):
    return {"messages": [{"ts": "1", "user": user, "text": "parent"}]}


class ResolveThreadReplyTest(unittest.TestCase):
    """Direct tests of resolve_thread_reply with stubbed web/append."""

    def resolve(self, event, web=None, append=None, me=ME, bot_id=None):
        if web is None:
            web = mock.Mock()
            web.conversations_replies.return_value = parent_msg()
        if append is None:
            append = mock.Mock()
        out = bridge.resolve_thread_reply(event, me, bot_id, web, append)
        return out, web, append

    def test_parent_is_mine_queues(self):
        (outcome, rec), _, append = self.resolve(thread_event())
        self.assertEqual(outcome, "queued")
        self.assertEqual(rec["kind"], "thread_reply")
        self.assertEqual(rec["thread_ts"], "1")
        self.assertEqual(rec["msg_id"], "C1:2")
        append.assert_called_once()

    def test_parent_matched_by_bot_id(self):
        web = mock.Mock()
        web.conversations_replies.return_value = {
            "messages": [{"ts": "1", "bot_id": "B_SELF"}]}
        (outcome, _), _, _ = self.resolve(thread_event(), web=web,
                                           bot_id="B_SELF")
        self.assertEqual(outcome, "queued")

    def test_parent_not_mine_drops(self):
        web = mock.Mock()
        web.conversations_replies.return_value = parent_msg(user="U_OTHER")
        (outcome, rec), _, append = self.resolve(thread_event(), web=web)
        self.assertEqual(outcome, "drop")
        self.assertIsNone(rec)
        append.assert_not_called()

    def test_api_failure_retries(self):
        web = mock.Mock()
        web.conversations_replies.side_effect = TimeoutError("slow")
        (outcome, _), _, append = self.resolve(thread_event(), web=web)
        self.assertEqual(outcome, "retry")
        append.assert_not_called()

    def test_empty_parent_result_retries(self):
        web = mock.Mock()
        web.conversations_replies.return_value = {"messages": []}
        (outcome, _), _, append = self.resolve(thread_event(), web=web)
        self.assertEqual(outcome, "retry")
        append.assert_not_called()

    def test_append_failure_retries(self):
        def boom(record):
            raise OSError("disk gone")
        (outcome, _), _, _ = self.resolve(thread_event(), append=boom)
        self.assertEqual(outcome, "retry")

    def test_duplicate_append_still_queues(self):
        # append returns False on duplicate (after confirming fsync):
        # still safe to ACK, never double-enqueues downstream.
        (outcome, rec), _, _ = self.resolve(thread_event(),
                                            append=lambda r: False)
        self.assertEqual(outcome, "queued")
        self.assertEqual(rec["msg_id"], "C1:2")


class ThreadReplyHandlerTest(unittest.TestCase):
    """Drive the real bridge handler with a mocked socket + web client."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="thread_reply_")
        self.addCleanup(temporary.cleanup)
        paths = mock.patch.multiple(
            inbox_store,
            BASE=temporary.name,
            INBOX_PATH=os.path.join(temporary.name, "inbox.jsonl"),
            LOCK_PATH=os.path.join(temporary.name, "inbox.lock"),
            _MIGRATED={},
        )
        paths.start()
        self.addCleanup(paths.stop)

    def load_handler(self):
        spec = importlib.util.spec_from_file_location(
            "thread_reply_test_bridge", os.path.join(ROOT, "bridge.py"))
        bmod = importlib.util.module_from_spec(spec)
        env_path = os.path.join(ROOT, ".env")
        exists = os.path.exists
        with mock.patch("logging.basicConfig"), mock.patch(
            "os.path.exists",
            side_effect=lambda p: p != env_path and exists(p)):
            spec.loader.exec_module(bmod)
        bmod._ENV = {"SLACK_BOT_TOKEN": "test-only",
                     "SLACK_APP_TOKEN": "test-only",
                     "SLACK_BOT_USER_ID": ME}
        socket = types.SimpleNamespace(
            socket_mode_request_listeners=[],
            connect=mock.Mock(), close=mock.Mock(),
            send_socket_mode_response=mock.Mock())
        web_mock = mock.Mock()
        module_attrs = {
            "slack_sdk": {},
            "slack_sdk.web": {"WebClient": mock.Mock(return_value=web_mock)},
            "slack_sdk.socket_mode": {
                "SocketModeClient": lambda **kw: socket},
            "slack_sdk.socket_mode.request": {"SocketModeRequest": object},
            "slack_sdk.socket_mode.response": {
                "SocketModeResponse": types.SimpleNamespace},
        }
        modules = {}
        for name, attrs in module_attrs.items():
            m = types.ModuleType(name)
            m.__dict__.update(attrs)
            modules[name] = m
        with mock.patch.dict(sys.modules, modules), \
             mock.patch.object(bmod, "_ssl_ctx", return_value=None), \
             mock.patch.object(bmod, "_start_final_deadline_watchdog"), \
             mock.patch.object(bmod.time, "sleep",
                               side_effect=KeyboardInterrupt):
            bmod.main()
        handler = socket.socket_mode_request_listeners[0]
        return handler, socket, web_mock

    @staticmethod
    def request(ts, **event_kw):
        e = thread_event(ts=ts, **event_kw)
        return types.SimpleNamespace(
            type="events_api", envelope_id="env-" + ts,
            payload={"event": e})

    @staticmethod
    def acked_envelopes(socket):
        return [c.args[0].envelope_id
                for c in socket.send_socket_mode_response.call_args_list]

    @staticmethod
    def undelivered():
        return [r["msg_id"] for r in inbox_store.read_undelivered()]

    def test_mine_queues_and_acks(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.return_value = parent_msg()
        handler(socket, self.request("2"))
        self.assertEqual(self.acked_envelopes(socket), ["env-2"])
        self.assertEqual(self.undelivered(), ["C1:2"])
        rec = inbox_store.read_undelivered()[0]
        self.assertEqual(rec["kind"], "thread_reply")
        self.assertEqual(rec["thread_ts"], "1")

    def test_not_mine_acks_and_drops(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.return_value = parent_msg(user="U_OTHER")
        handler(socket, self.request("2"))
        handler(socket, self.request("2"))  # redelivery also drops cleanly
        self.assertEqual(self.acked_envelopes(socket), ["env-2", "env-2"])
        self.assertEqual(self.undelivered(), [])

    def test_check_timeout_withholds_ack_then_redelivery_succeeds(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.side_effect = [TimeoutError("t"),
                                                 parent_msg()]
        handler(socket, self.request("2"))
        self.assertEqual(self.acked_envelopes(socket), [])  # no ACK
        self.assertEqual(self.undelivered(), [])
        handler(socket, self.request("2"))  # Slack redelivers
        self.assertEqual(self.acked_envelopes(socket), ["env-2"])
        self.assertEqual(self.undelivered(), ["C1:2"])

    def test_fsync_failure_withholds_ack(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.return_value = parent_msg()
        with mock.patch.object(inbox_store, "_fsync_dir",
                               side_effect=OSError("injected")), \
             mock.patch("logging.exception"):
            handler(socket, self.request("2"))
        # 目录项没确认：不 ACK（崩溃后文件可能不可见，靠 Slack 重发）。
        # 数据本身已 fsync，文件里可见一条记录是符合设计的。
        self.assertEqual(self.acked_envelopes(socket), [])
        # 存储恢复后重发：去重命中并确认持久化，ACK，且只有一条记录
        handler(socket, self.request("2"))
        self.assertEqual(self.acked_envelopes(socket), ["env-2"])
        self.assertEqual(self.undelivered(), ["C1:2"])

    def test_crash_before_ack_confirmed_redelivery_dedups(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.return_value = parent_msg()
        # first attempt persists but the ACK never goes out (crash)
        socket.send_socket_mode_response.side_effect = [Exception("boom"),
                                                        None]
        with mock.patch("logging.exception"):
            handler(socket, self.request("2"))
        # redelivery: duplicate confirmed durable, ACK now succeeds
        handler(socket, self.request("2"))
        self.assertEqual(self.undelivered(), ["C1:2"])
        self.assertEqual(self.acked_envelopes(socket), ["env-2", "env-2"])

    def test_redelivery_processed_only_once(self):
        handler, socket, web = self.load_handler()
        web.conversations_replies.return_value = parent_msg()
        handler(socket, self.request("2"))
        handler(socket, self.request("2"))
        self.assertEqual(self.undelivered(), ["C1:2"])
        self.assertEqual(self.acked_envelopes(socket), ["env-2", "env-2"])

    def test_bot_thread_reply_never_queries(self):
        handler, socket, web = self.load_handler()
        handler(socket, self.request("2", bot_id="B_X",
                                     subtype="bot_message"))
        web.conversations_replies.assert_not_called()
        self.assertEqual(self.acked_envelopes(socket), ["env-2"])
        self.assertEqual(self.undelivered(), [])


if __name__ == "__main__":
    unittest.main()
