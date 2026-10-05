"""Tests for the bridge self-healing supervisor (serve_forever).

Run:  python -m unittest discover -s tests   (from muse/)
Covers: rebuild on exception with exponential backoff,
backoff reset after a long-lived run, SystemExit -> rebuild,
KeyboardInterrupt / SIGTERM flag -> clean exit (no rebuild).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        bridge._shutdown = False

    def tearDown(self):
        bridge._shutdown = False

    def test_rebuilds_on_exception_with_exponential_backoff(self):
        calls, sleeps = [], []

        def fake_runner():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             sleep_fn=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [5, 10])

    def test_backoff_capped_at_max(self):
        calls, sleeps = [], []

        def fake_runner():
            calls.append(1)
            if len(calls) < 5:
                raise RuntimeError("boom")
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=12,
                             sleep_fn=sleeps.append)
        self.assertEqual(len(calls), 5)
        self.assertEqual(sleeps, [5, 10, 12, 12])

    def test_backoff_resets_after_long_lived_run(self):
        calls, sleeps = [], []
        clock = {"t": 1000.0}

        def fake_runner():
            calls.append(1)
            if len(calls) == 1:
                clock["t"] += 400.0  # first run "lived" 400s -> transient blip
                raise RuntimeError("boom after long run")
            bridge._shutdown = True

        with mock.patch.object(bridge, "time") as mock_time:
            mock_time.time.side_effect = lambda: clock["t"]
            bridge.serve_forever("x", "y", runner=fake_runner,
                                 initial_backoff=5, max_backoff=300,
                                 sleep_fn=sleeps.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [])  # reset -> rebuild immediately, no sleep

    def test_systemexit_triggers_rebuild(self):
        calls = []

        def fake_runner():
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit(3)
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             sleep_fn=lambda s: None)
        self.assertEqual(len(calls), 2)

    def test_keyboardinterrupt_exits_cleanly(self):
        calls = []

        def fake_runner():
            calls.append(1)
            raise KeyboardInterrupt()

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             sleep_fn=lambda s: None)
        self.assertEqual(len(calls), 1)  # no rebuild after clean interrupt

    def test_sigterm_handler_sets_shutdown_flag(self):
        self.assertFalse(bridge._shutdown)
        bridge._handle_sigterm(15, None)
        self.assertTrue(bridge._shutdown)

    def test_missing_tokens_still_exits_without_retry(self):
        # main() must not enter the retry loop on hard config errors.
        with mock.patch.dict(bridge._ENV, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                bridge.main()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
