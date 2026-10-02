"""Directory-sync recovery with the real bridge handler and isolated storage."""
import errno
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


class InboxDurabilityRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="inbox_durability_")
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

    def bridge_handler(self):
        # Import the actual bridge without reading deployment credentials or
        # creating its log. Slack clients are replaced before main() runs.
        spec = importlib.util.spec_from_file_location(
            "durability_test_bridge", os.path.join(ROOT, "bridge.py")
        )
        bridge = importlib.util.module_from_spec(spec)
        env_path = os.path.join(ROOT, ".env")
        exists = os.path.exists
        with mock.patch("logging.basicConfig"), mock.patch(
            "os.path.exists", side_effect=lambda p: p != env_path and exists(p)
        ):
            spec.loader.exec_module(bridge)
        bridge._ENV = {
            "SLACK_BOT_TOKEN": "test-only",
            "SLACK_APP_TOKEN": "test-only",
            "SLACK_BOT_USER_ID": "U_SELF",
        }
        socket = types.SimpleNamespace(
            socket_mode_request_listeners=[],
            connect=mock.Mock(),
            close=mock.Mock(),
            send_socket_mode_response=mock.Mock(),
        )
        module_attrs = {
            "slack_sdk": {},
            "slack_sdk.web": {"WebClient": mock.Mock()},
            "slack_sdk.socket_mode": {
                "SocketModeClient": lambda **kwargs: socket,
            },
            "slack_sdk.socket_mode.request": {"SocketModeRequest": object},
            "slack_sdk.socket_mode.response": {
                "SocketModeResponse": types.SimpleNamespace,
            },
        }
        modules = {}
        for name, attrs in module_attrs.items():
            module = types.ModuleType(name)
            module.__dict__.update(attrs)
            modules[name] = module
        with mock.patch.dict(sys.modules, modules), mock.patch.object(
            bridge, "_ssl_ctx", return_value=None
        ), mock.patch.object(bridge.time, "sleep", side_effect=KeyboardInterrupt):
            bridge.main()
        return socket.socket_mode_request_listeners[0], socket

    @staticmethod
    def request(ts):
        return types.SimpleNamespace(
            type="events_api",
            envelope_id="envelope-" + ts,
            payload={"event": {
                "type": "app_mention", "channel": "C_TEST",
                "user": "U_OTHER", "text": "hello", "ts": ts,
            }},
        )

    def test_creation_failure_blocks_new_messages_and_duplicates_until_recovery(self):
        handler, socket = self.bridge_handler()
        failure = OSError(errno.EIO, "injected directory sync failure")
        with mock.patch.object(inbox_store, "_fsync_dir", side_effect=failure), \
                mock.patch("logging.exception"):
            # The first failure leaves a visible file behind. New identities
            # must not confuse that existence with confirmed durability.
            for ts in ("1", "2", "3", "1"):
                handler(socket, self.request(ts))
                socket.send_socket_mode_response.assert_not_called()

        # Redeliver after storage recovers: each retained record can now be
        # acknowledged, and the duplicate must not create a second body.
        for ts in ("1", "2", "3", "4"):
            handler(socket, self.request(ts))
        self.assertEqual(
            [call.args[0].envelope_id
             for call in socket.send_socket_mode_response.call_args_list],
            ["envelope-1", "envelope-2", "envelope-3", "envelope-4"],
        )
        self.assertEqual(
            [r["msg_id"] for r in inbox_store.read_undelivered()],
            ["C_TEST:1", "C_TEST:2", "C_TEST:3", "C_TEST:4"],
        )

    def test_append_after_failed_compaction_still_confirms_directory(self):
        inbox_store.append_record({"channel": "C_TEST", "ts": "1"})
        inbox_store.ack(["C_TEST:1"])
        with mock.patch.object(
            inbox_store, "_fsync_dir",
            side_effect=OSError(errno.EIO, "injected directory sync failure"),
        ):
            with self.assertRaises(OSError):
                inbox_store.compact()
            with self.assertRaises(OSError):
                inbox_store.append_record({"channel": "C_TEST", "ts": "2"})

        self.assertFalse(inbox_store.append_record({"channel": "C_TEST", "ts": "2"}))
        self.assertEqual(
            [r["msg_id"] for r in inbox_store.read_undelivered()], ["C_TEST:2"]
        )


if __name__ == "__main__":
    unittest.main()
