"""Tests for the bridge self-healing supervisor (serve_forever).

Run:  python -m unittest discover -s tests   (from muse/)
Covers:
  * rebuild on exception with exponential backoff (fake runner)
  * backoff reset after a long-lived run / cap at max
  * SystemExit -> rebuild; KeyboardInterrupt -> clean exit
  * SIGTERM/SIGINT handlers set the shutdown flag + event
  * REAL run_client_once path: consecutive connect() failures release
    every created client (no thread/resource accumulation), then recovery
    stays healthy (test_real_path_* -- no mocked runner)
  * REAL subprocess: SIGTERM/SIGINT during backoff -> prompt clean exit,
    no rebuild, no traceback (test_subprocess_*)
"""
import os
import signal
import subprocess
import sys
import threading
import time
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
        calls, waits = [], []

        def fake_runner():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             wait_fn=waits.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(waits, [5, 10])

    def test_backoff_capped_at_max(self):
        calls, waits = [], []

        def fake_runner():
            calls.append(1)
            if len(calls) < 5:
                raise RuntimeError("boom")
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=12,
                             wait_fn=waits.append)
        self.assertEqual(len(calls), 5)
        self.assertEqual(waits, [5, 10, 12, 12])

    def test_backoff_resets_after_long_lived_run(self):
        calls, waits = [], []
        clock = {"t": 1000.0}

        def fake_runner():
            calls.append(1)
            if len(calls) == 1:
                clock["t"] += 400.0  # first run "lived" 400s -> transient blip
                raise RuntimeError("boom after long run")
            bridge._shutdown = True

        with mock.patch.object(bridge, "time") as mock_time:
            mock_time.monotonic.side_effect = lambda: clock["t"]
            bridge.serve_forever("x", "y", runner=fake_runner,
                                 initial_backoff=5, max_backoff=300,
                                 wait_fn=waits.append)
        self.assertEqual(len(calls), 2)
        self.assertEqual(waits, [])  # reset -> rebuild immediately, no wait

    def test_systemexit_triggers_rebuild(self):
        calls = []

        def fake_runner():
            calls.append(1)
            if len(calls) == 1:
                raise SystemExit(3)
            bridge._shutdown = True

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             wait_fn=lambda s: None)
        self.assertEqual(len(calls), 2)

    def test_keyboardinterrupt_exits_cleanly(self):
        calls = []

        def fake_runner():
            calls.append(1)
            raise KeyboardInterrupt()

        bridge.serve_forever("x", "y", runner=fake_runner,
                             initial_backoff=5, max_backoff=300,
                             wait_fn=lambda s: None)
        self.assertEqual(len(calls), 1)  # no rebuild after clean interrupt

    def test_sigterm_handler_sets_shutdown_flag(self):
        # handler 内只做布尔赋值 (logging/IO 在信号 handler 里不安全)。
        self.assertFalse(bridge._shutdown)
        bridge._handle_sigterm(15, None)
        self.assertTrue(bridge._shutdown)

    def test_sigint_handler_sets_shutdown_flag(self):
        bridge._handle_sigint(2, None)
        self.assertTrue(bridge._shutdown)

    def test_missing_tokens_still_exits_without_retry(self):
        # main() must not enter the retry loop on hard config errors.
        with mock.patch.dict(bridge._ENV, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                bridge.main()
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# Real-path tests: exercise run_client_once() itself (not a fake runner),
# with a stubbed slack_sdk that mimics the real resource model --
# threads + pool acquired at construction, released by close().
# ---------------------------------------------------------------------------

class FakeWebClient:
    def __init__(self, *a, **kw):
        pass

    def auth_test(self):
        return {"user_id": "U_TEST"}


class FakeSocketModeClient:
    """Stub mirroring slack_sdk 3.45.0's resource model: a worker thread is
    started at construction and must be released by close()."""
    instances = []
    fail_connect_times = 0

    def __init__(self, *a, **kw):
        self.closed = False
        self.socket_mode_request_listeners = []
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._stop.wait, name="fake-smc-worker", daemon=True)
        self._thread.start()
        FakeSocketModeClient.instances.append(self)

    def connect(self):
        if FakeSocketModeClient.fail_connect_times > 0:
            FakeSocketModeClient.fail_connect_times -= 1
            raise RuntimeError("simulated connect failure")

    def close(self):
        self.closed = True
        self._stop.set()
        self._thread.join(timeout=5)


class RealPathConnectFailureTest(unittest.TestCase):
    """Consecutive connect() failures must release every created client;
    recovery must stay healthy without rebuilds."""

    def setUp(self):
        bridge._shutdown = False
        FakeSocketModeClient.instances = []
        FakeSocketModeClient.fail_connect_times = 0
        self._threads_before = threading.active_count()
        self._patches = [
            mock.patch("slack_sdk.web.WebClient", FakeWebClient),
            mock.patch("slack_sdk.socket_mode.SocketModeClient",
                       FakeSocketModeClient),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        bridge._shutdown = True
        # join any leaked fake threads so later tests see a clean slate
        for inst in FakeSocketModeClient.instances:
            if not inst.closed:
                inst.close()
        bridge._shutdown = False

    def _run_supervisor_bg(self, **kw):
        kw.setdefault("initial_backoff", 0.1)
        kw.setdefault("max_backoff", 0.2)
        t = threading.Thread(
            target=lambda: bridge.serve_forever("x", "y", **kw),
            daemon=True)
        t.start()
        return t

    def _wait_for_instances(self, n, timeout=15):
        deadline = time.time() + timeout
        while (len(FakeSocketModeClient.instances) < n
               and time.time() < deadline):
            time.sleep(0.05)
        self.assertEqual(
            n, len(FakeSocketModeClient.instances),
            "timed out waiting for %d client instances" % n)

    def test_consecutive_failures_release_every_client_then_recover(self):
        FakeSocketModeClient.fail_connect_times = 5
        t = self._run_supervisor_bg()
        try:
            # 5 failures + 1 successful connect; the 6th instance stays up
            # until we shut down (run_client_once blocks while healthy).
            self._wait_for_instances(6)
            failed = FakeSocketModeClient.instances[:5]
            for i, inst in enumerate(failed):
                self.assertTrue(
                    inst.closed,
                    "failed client #%d was not close()d -- resource leak" % i)
                self.assertFalse(
                    inst._thread.is_alive(),
                    "failed client #%d thread still alive -- thread leak" % i)
            # healthy period: no rebuild while connected
            time.sleep(0.5)
            self.assertEqual(6, len(FakeSocketModeClient.instances))
            # clean shutdown releases the healthy client too
            bridge._shutdown = True
            t.join(timeout=10)
            self.assertFalse(t.is_alive())
            self.assertTrue(FakeSocketModeClient.instances[5].closed)
            self.assertEqual(self._threads_before, threading.active_count(),
                             "thread count did not return to baseline")
        finally:
            if t.is_alive():
                bridge._shutdown = True
                t.join(timeout=10)


# ---------------------------------------------------------------------------
# Subprocess signal tests: real process, real signals, during backoff.
# ---------------------------------------------------------------------------

class SubprocessSignalTest(unittest.TestCase):
    DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "supervisor_proc_driver.py")

    def _run_driver_until_backoff(self):
        proc = subprocess.Popen(
            [sys.executable, self.DRIVER, "30"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 25
        saw = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "DRIVER runner failed #1" in line:
                saw = True
                break
        self.assertTrue(saw, "driver did not reach backoff (rc=%s)"
                             % proc.poll())
        return proc

    def _assert_clean_signal_exit(self, proc, sig, name):
        try:
            t0 = time.time()
            proc.send_signal(sig)
            proc.wait(timeout=15)
            elapsed = time.time() - t0
            out, err = proc.communicate()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertLess(elapsed, 5,
                        "%s during backoff took %.1fs to exit" % (name, elapsed))
        self.assertNotIn("Traceback", err,
                         "%s produced a traceback: %s" % (name, err[-500:]))
        self.assertNotIn("DRIVER runner failed #2", out,
                         "%s caused a client rebuild after shutdown" % name)
        self.assertIn("DRIVER exiting cleanly", out,
                      "%s did not exit cleanly; stdout tail: %s"
                      % (name, out[-500:]))

    def test_sigterm_during_backoff(self):
        proc = self._run_driver_until_backoff()
        self._assert_clean_signal_exit(proc, signal.SIGTERM, "SIGTERM")

    def test_sigint_during_backoff(self):
        proc = self._run_driver_until_backoff()
        self._assert_clean_signal_exit(proc, signal.SIGINT, "SIGINT")


if __name__ == "__main__":
    unittest.main()
