"""Shutdown-phase tests for the bridge supervisor (round 3).

Covers the three blocking phases with the REAL slack_sdk 3.45.0 client --
not a fake runner and not only the bridge's own backoff:

  * SubprocessRateLimitTest: apps.connections.open always returns
    ratelimited (controlled SlackApiError). After the parent observes the
    client has entered the retry wait, it sends a real SIGTERM/SIGINT and
    verifies: prompt exit, no further apps_connections_open calls after
    shutdown, clean close, no traceback.
  * CloseWhileReconnectWaitTest: a background thread sits in the retry
    wait (simulating the SDK monitor's reconnect path); close() from the
    main thread must return promptly (bounded) and retries must stop.
  * BoundedIssuanceUnitTest: _issue_new_wss_url_bounded honors _shutdown
    mid-wait, stops retrying, and propagates non-rate-limit errors.
"""
import os
import signal
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient


class FakeRateLimitResponse(dict):
    def __init__(self, retry_after="30"):
        super().__init__(error="ratelimited")
        self.headers = {"Retry-After": retry_after}


class RateLimitedWebClient:
    """Stub WebClient: auth works, apps.connections.open is always limited."""
    def __init__(self):
        self.calls = 0

    def auth_test(self):
        return {"user_id": "U_TEST"}

    def apps_connections_open(self, app_token=None):
        self.calls += 1
        raise SlackApiError("ratelimited", FakeRateLimitResponse())

    def ok_then_limited(self, app_token=None):
        # helper for the success-path unit test, unused by default
        return {"url": "wss://example.invalid/"}


def _real_patched_client(web):
    """Real SocketModeClient with the same instance patch run_client_once
    applies, so tests exercise the production wiring."""
    smc = SocketModeClient(app_token="xapp-test", web_client=web)
    smc.issue_new_wss_url = lambda: bridge._issue_new_wss_url_bounded(smc)
    return smc


# ---------------------------------------------------------------------------
# Unit: bounded issuance
# ---------------------------------------------------------------------------

class BoundedIssuanceUnitTest(unittest.TestCase):
    def setUp(self):
        self._prev = bridge._shutdown
        bridge._shutdown = False

    def tearDown(self):
        bridge._shutdown = self._prev

    def test_ratelimit_wait_aborts_promptly_on_shutdown(self):
        web = RateLimitedWebClient()
        smc = _real_patched_client(web)
        try:
            def _ask():
                try:
                    bridge._issue_new_wss_url_bounded(smc)
                except bridge._ShutdownRequested:
                    results["shutdown"] = True
                except Exception as e:  # noqa: BLE001
                    results["other"] = e

            results = {}
            t = threading.Thread(target=_ask, daemon=True)
            t0 = time.monotonic()
            t.start()
            # wait until it is inside the Retry-After wait
            deadline = time.time() + 10
            while web.calls < 1 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(web.calls, 1, "never entered retry wait")
            time.sleep(0.3)  # firmly inside the 30s wait
            bridge._shutdown = True
            t.join(timeout=10)
            elapsed = time.monotonic() - t0
            self.assertFalse(t.is_alive(), "issuance did not abort on shutdown")
            self.assertTrue(results.get("shutdown"),
                            "expected _ShutdownRequested, got %r" % results)
            # Retry-After was 30s; abort must be ~1s-granularity, not 30s.
            self.assertLess(elapsed, 5,
                            "shutdown abort took %.1fs" % elapsed)
            calls_at_shutdown = web.calls
            time.sleep(1.5)
            self.assertEqual(calls_at_shutdown, web.calls,
                             "apps_connections_open retried after shutdown")
        finally:
            bridge._shutdown = True
            bridge._close_bounded(smc)
            bridge._shutdown = self._prev

    def test_non_ratelimit_error_propagates(self):
        class BadAuthWebClient(RateLimitedWebClient):
            def apps_connections_open(self, app_token=None):
                self.calls += 1
                raise SlackApiError("invalid_auth", {"error": "invalid_auth"})

        web = BadAuthWebClient()
        smc = _real_patched_client(web)
        try:
            with self.assertRaises(SlackApiError):
                bridge._issue_new_wss_url_bounded(smc)
            self.assertEqual(1, web.calls, "must not retry non-ratelimit")
        finally:
            bridge._close_bounded(smc)


# ---------------------------------------------------------------------------
# In-process: close() while a reconnect retry wait is in progress
# ---------------------------------------------------------------------------

class CloseWhileReconnectWaitTest(unittest.TestCase):
    """Simulates the SDK monitor thread stuck in a rate-limit retry wait
    when the main thread closes the client: close() must stay bounded and
    the retry loop must not fire again."""

    def setUp(self):
        self._prev = bridge._shutdown
        bridge._shutdown = False

    def tearDown(self):
        bridge._shutdown = self._prev

    def test_close_while_reconnect_wait_in_progress(self):
        web = RateLimitedWebClient()
        smc = _real_patched_client(web)

        # background "monitor" thread: keeps asking for a WSS URL, i.e. the
        # SDK reconnect path (connect_to_new_endpoint -> issue_new_wss_url)
        def _reconnect_loop():
            try:
                smc.issue_new_wss_url()
            except bridge._ShutdownRequested:
                pass
            except Exception:  # noqa: BLE001 -- never fail the test thread
                pass

        t = threading.Thread(target=_reconnect_loop, name="fake-monitor",
                             daemon=True)
        t.start()
        try:
            deadline = time.time() + 10
            while web.calls < 1 and time.time() < deadline:
                time.sleep(0.05)
            self.assertGreaterEqual(web.calls, 1,
                                    "reconnect thread never entered wait")
            time.sleep(0.3)  # firmly inside the Retry-After wait

            bridge._shutdown = True
            t0 = time.monotonic()
            ok = bridge._close_bounded(smc, timeout=10)
            elapsed = time.monotonic() - t0
            self.assertTrue(ok, "close() did not complete (timed out)")
            self.assertLess(elapsed, 5,
                            "close() while retry wait took %.1fs" % elapsed)

            calls_at_close = web.calls
            t.join(timeout=5)
            self.assertFalse(t.is_alive(),
                             "reconnect thread still alive after close")
            time.sleep(1.0)
            self.assertEqual(calls_at_close, web.calls,
                             "reconnect retry continued after close")
        finally:
            bridge._shutdown = True
            t.join(timeout=5)


# ---------------------------------------------------------------------------
# Subprocess: real signals during the SDK rate-limit wait
# ---------------------------------------------------------------------------

class SubprocessRateLimitTest(unittest.TestCase):
    DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "rate_limit_driver.py")

    def _run_driver_until_ratelimit_wait(self):
        proc = subprocess.Popen(
            [sys.executable, "-u", self.DRIVER],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 30
        saw = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "Rate limited. Retrying in" in line:
                saw = True
                break
        self.assertTrue(saw, "driver did not enter SDK rate-limit wait "
                             "(rc=%s)" % proc.poll())
        return proc

    def _assert_clean_signal_exit(self, proc, sig, name):
        try:
            t0 = time.time()
            proc.send_signal(sig)
            proc.wait(timeout=20)
            elapsed = time.time() - t0
            out, err = proc.communicate()
        finally:
            if proc.poll() is None:
                proc.kill()
                out, err = proc.communicate()
        # 1) bounded shutdown: Retry-After is 30s; exit must be ~1s-scale.
        self.assertLess(elapsed, 10,
                        "%s during SDK rate-limit wait took %.1fs to exit"
                        % (name, elapsed))
        # 2) no traceback, clean driver exit.
        self.assertNotIn("Traceback", err,
                         "%s produced a traceback: %s" % (name, err[-500:]))
        self.assertIn("DRIVER exiting cleanly", out,
                      "%s did not exit cleanly; stdout tail: %s"
                      % (name, out[-500:]))
        # 3) no more apps_connections_open calls after shutdown was logged.
        lines = out.splitlines()
        shutdown_idx = next(
            (i for i, l in enumerate(lines)
             if "shutdown requested, exiting cleanly" in l
             or "shutdown during connect/retry; exiting" in l),
            None)
        self.assertIsNotNone(shutdown_idx,
                              "no shutdown marker in driver output")
        calls_after = [l for l in lines[shutdown_idx:]
                       if "DRIVER apps_connections_open #" in l]
        self.assertEqual([], calls_after,
                         "%s: apps_connections_open retried after shutdown: %s"
                         % (name, calls_after))
        # 4) total calls stay small: exactly 1 before the signal (the wait
        #    began right after it), proving retries stopped, not piled up.
        total = len([l for l in lines
                     if "DRIVER apps_connections_open #" in l])
        self.assertLessEqual(total, 2,
                             "%s: too many pre-shutdown calls: %d"
                             % (name, total))

    def test_sigterm_during_sdk_ratelimit_wait(self):
        proc = self._run_driver_until_ratelimit_wait()
        self._assert_clean_signal_exit(proc, signal.SIGTERM, "SIGTERM")

    def test_sigint_during_sdk_ratelimit_wait(self):
        proc = self._run_driver_until_ratelimit_wait()
        self._assert_clean_signal_exit(proc, signal.SIGINT, "SIGINT")


if __name__ == "__main__":
    unittest.main()
