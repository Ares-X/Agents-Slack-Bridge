"""Subprocess driver for SDK rate-limit shutdown tests (not a unittest module).

Uses the REAL SocketModeClient and the REAL run_client_once() /
serve_forever() path; only the network layer is stubbed: WebClient
always raises a controlled ratelimited SlackApiError from
apps_connections_open, so the client sits in the (bounded) retry wait.
The parent test then sends a real SIGTERM/SIGINT after observing the
wait, and verifies prompt exit, no further apps_connections_open calls,
and clean resource release.
"""
import logging
import os
import sys
import threading
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
from slack_sdk.errors import SlackApiError


class _FlushHandler(logging.StreamHandler):
    """Flush every record: the parent watches stdout for the wait marker."""

    def emit(self, record):
        super().emit(record)
        self.flush()


# bridge import configured file logging; redirect to stdout for observability.
logging.basicConfig(handlers=[_FlushHandler(sys.stdout)], level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", force=True)


class FakeRateLimitResponse(dict):
    def __init__(self, retry_after="30"):
        super().__init__(error="ratelimited")
        self.headers = {"Retry-After": retry_after}


class RateLimitedWebClient:
    """Stub WebClient: auth works, apps.connections.open is always limited."""
    calls = 0

    def __init__(self, *a, **kw):
        pass

    def auth_test(self):
        return {"user_id": "U_TEST"}

    def apps_connections_open(self, app_token=None):
        RateLimitedWebClient.calls += 1
        print("DRIVER apps_connections_open #%d" % RateLimitedWebClient.calls,
              flush=True)
        raise SlackApiError("ratelimited", FakeRateLimitResponse())


if __name__ == "__main__":
    with mock.patch("slack_sdk.web.WebClient", RateLimitedWebClient):
        bridge.serve_forever("x", "y", initial_backoff=1, max_backoff=1)
    print("DRIVER exiting cleanly", flush=True)
    print("DRIVER total apps_connections_open: %d" % RateLimitedWebClient.calls,
          flush=True)
    # Observability for the parent: any lingering SDK threads at exit?
    # (Prompt process exit itself proves non-daemon pool workers were
    # released -- the interpreter joins them on shutdown.)
    names = sorted(t.name for t in threading.enumerate())
    print("DRIVER threads at exit: %s" % names, flush=True)
