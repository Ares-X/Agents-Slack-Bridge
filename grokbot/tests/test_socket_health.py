"""socket_health heartbeat + probe decisions. No live Slack."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from socket_health import (
    PROBE_NO_RESTART,
    PROBE_OK,
    PROBE_RESTART,
    connection_health_state,
    connection_healthy,
    decide_probe,
    probe_once,
    read_heartbeat_age,
    write_heartbeat,
)


class FakeSession:
    def __init__(self, last_ping_pong_time=None):
        self.last_ping_pong_time = last_ping_pong_time


class FakeSMC:
    def __init__(self, connected=True, last_ping_pong_time=None):
        self._connected = connected
        self.current_session = FakeSession(last_ping_pong_time)

    def is_connected(self):
        return self._connected


class SocketHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hb = os.path.join(self.tmp.name, "socket_heartbeat")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_read_heartbeat_age(self):
        write_heartbeat(self.hb, now=1000.0)
        age, hb = read_heartbeat_age(self.hb, now=1030.0)
        self.assertEqual(hb, 1000.0)
        self.assertAlmostEqual(age, 30.0, places=3)

    def test_missing_heartbeat(self):
        age, hb = read_heartbeat_age(self.hb, now=1.0)
        self.assertIsNone(age)
        self.assertIsNone(hb)

    def test_connection_healthy_requires_fresh_pong(self):
        now = 10_000.0
        ok = FakeSMC(True, last_ping_pong_time=now - 5)
        self.assertTrue(connection_healthy(ok, ping_stale_sec=25, now=now))
        stale = FakeSMC(True, last_ping_pong_time=now - 40)
        self.assertFalse(connection_healthy(stale, ping_stale_sec=25, now=now))
        down = FakeSMC(False, last_ping_pong_time=now - 1)
        self.assertFalse(connection_healthy(down, ping_stale_sec=25, now=now))
        no_pong = FakeSMC(True, last_ping_pong_time=None)
        self.assertEqual(
            connection_health_state(
                no_pong, ping_stale_sec=25, now=now, connected_at=now - 5
            ),
            "warmup",
        )
        self.assertEqual(
            connection_health_state(
                no_pong, ping_stale_sec=25, now=now, connected_at=now - 60
            ),
            "bad",
        )

    def test_decide_probe_ok(self):
        code, reason = decide_probe(
            heartbeat_age=10.0, auth_ok=True, auth_detail="n/a", stale_sec=120
        )
        self.assertEqual(code, PROBE_OK)
        self.assertIn("probe ok", reason)

    def test_decide_probe_restart_when_stale_and_auth_ok(self):
        code, reason = decide_probe(
            heartbeat_age=200.0, auth_ok=True, auth_detail="auth.test ok", stale_sec=120
        )
        self.assertEqual(code, PROBE_RESTART)
        self.assertIn("restarting bridge.py", reason)

    def test_decide_probe_no_restart_when_auth_fails(self):
        code, reason = decide_probe(
            heartbeat_age=None,
            auth_ok=False,
            auth_detail="auth.test error: Timeout",
            stale_sec=120,
        )
        self.assertEqual(code, PROBE_NO_RESTART)
        self.assertIn("not restarting", reason)

    def test_probe_once_skips_auth_when_fresh(self):
        write_heartbeat(self.hb, now=1000.0)
        calls = []

        def auth_fn():
            calls.append(1)
            return False, "should not run"

        code, reason = probe_once(
            heartbeat_path=self.hb,
            stale_sec=120,
            now=1050.0,
            auth_fn=auth_fn,
        )
        self.assertEqual(code, PROBE_OK)
        self.assertEqual(calls, [])
        self.assertIn("probe ok", reason)

    def test_probe_once_restart_path(self):
        write_heartbeat(self.hb, now=1000.0)
        code, reason = probe_once(
            heartbeat_path=self.hb,
            stale_sec=120,
            now=1300.0,
            auth_fn=lambda: (True, "auth.test ok"),
        )
        self.assertEqual(code, PROBE_RESTART)
        self.assertIn("restarting", reason)


    def test_decide_probe_waits_for_first_heartbeat(self):
        code, reason = decide_probe(
            heartbeat_age=None,
            auth_ok=True,
            auth_detail="auth.test ok",
            stale_sec=120,
            bridge_age=30.0,
        )
        self.assertEqual(code, PROBE_OK)
        self.assertIn("waiting for first heartbeat", reason)

    def test_probe_once_missing_hb_young_bridge_skips_auth(self):
        calls = []

        def auth_fn():
            calls.append(1)
            return True, "auth.test ok"

        code, reason = probe_once(
            heartbeat_path=self.hb,
            stale_sec=120,
            now=1000.0,
            auth_fn=auth_fn,
            bridge_age=10.0,
        )
        self.assertEqual(code, PROBE_OK)
        self.assertEqual(calls, [])
        self.assertIn("waiting for first heartbeat", reason)



    def test_decide_probe_warmup_with_leftover_stale_heartbeat(self):
        # Leftover file from previous process must not restart a young bridge.
        code, reason = decide_probe(
            heartbeat_age=200.0,
            auth_ok=True,
            auth_detail="auth.test ok",
            stale_sec=120,
            bridge_age=30.0,
        )
        self.assertEqual(code, PROBE_OK)
        self.assertIn("warmup", reason)
        self.assertIn("leftover heartbeat", reason)

    def test_probe_once_stale_hb_young_bridge_skips_auth(self):
        write_heartbeat(self.hb, now=1000.0)
        calls = []

        def auth_fn():
            calls.append(1)
            return True, "auth.test ok"

        code, reason = probe_once(
            heartbeat_path=self.hb,
            stale_sec=120,
            now=1300.0,
            auth_fn=auth_fn,
            bridge_age=10.0,
        )
        self.assertEqual(code, PROBE_OK)
        self.assertEqual(calls, [])
        self.assertIn("warmup", reason)


if __name__ == "__main__":
    unittest.main()
