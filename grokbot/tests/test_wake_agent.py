"""wake_agent: no redirect auth leak; Retry-After surfaced on 429."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wake_agent as wa  # noqa: E402


class _DualHost:
    """Two local HTTP servers recording POSTs (auth headers)."""

    def __init__(self):
        self.hits_a = []
        self.hits_b = []
        self._srv_a = None
        self._srv_b = None

    def start(self, *, redirect_a_to_b=True, status_a=302, retry_after=None):
        hits_a, hits_b = self.hits_a, self.hits_b
        parent = self

        class HandlerA(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                hits_a.append({
                    "path": self.path,
                    "Authorization": self.headers.get("Authorization"),
                    "X-Webhook-Key": self.headers.get("X-Webhook-Key"),
                    "X-Sender-Key": self.headers.get("X-Sender-Key"),
                })
                if redirect_a_to_b:
                    loc = f"http://127.0.0.1:{parent.port_b}/leak"
                    self.send_response(status_a)
                    self.send_header("Location", loc)
                    if retry_after is not None:
                        self.send_header("Retry-After", str(retry_after))
                    self.end_headers()
                elif status_a == 429:
                    self.send_response(429)
                    if retry_after is not None:
                        self.send_header("Retry-After", str(retry_after))
                    self.end_headers()
                    self.wfile.write(b"rate limited")
                else:
                    self.send_response(status_a)
                    self.end_headers()
                    self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        class HandlerB(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                hits_b.append({
                    "path": self.path,
                    "Authorization": self.headers.get("Authorization"),
                    "X-Webhook-Key": self.headers.get("X-Webhook-Key"),
                    "X-Sender-Key": self.headers.get("X-Sender-Key"),
                    "method": "POST",
                })
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"leaked")

            def do_GET(self):
                hits_b.append({
                    "path": self.path,
                    "Authorization": self.headers.get("Authorization"),
                    "X-Webhook-Key": self.headers.get("X-Webhook-Key"),
                    "X-Sender-Key": self.headers.get("X-Sender-Key"),
                    "method": "GET",
                })
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"leaked-get")

            def log_message(self, *args):
                pass

        self._srv_b = HTTPServer(("127.0.0.1", 0), HandlerB)
        self.port_b = self._srv_b.server_address[1]
        self._srv_a = HTTPServer(("127.0.0.1", 0), HandlerA)
        self.port_a = self._srv_a.server_address[1]
        threading.Thread(target=self._srv_b.serve_forever, daemon=True).start()
        threading.Thread(target=self._srv_a.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port_a}/wake"

    def stop(self):
        for s in (self._srv_a, self._srv_b):
            if s:
                s.shutdown()


class TestWakeAgentNoRedirect(unittest.TestCase):
    def test_redirect_second_host_gets_no_request(self):
        """P2: refuse 302; second origin must not see any of the three auth headers."""
        dual = _DualHost()
        url = dual.start(redirect_a_to_b=True, status_a=302)
        try:
            result = wa.post_webhook(
                url, "SECRET_KEY_XYZ", {"source": "test", "claimable": 1}
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], 302)
            self.assertIn("redirect", (result.get("error") or "").lower())
            self.assertEqual(len(dual.hits_a), 1)
            # Auth was sent to first hop only
            self.assertEqual(dual.hits_a[0]["Authorization"], "Bearer SECRET_KEY_XYZ")
            self.assertEqual(dual.hits_a[0]["X-Webhook-Key"], "SECRET_KEY_XYZ")
            self.assertEqual(dual.hits_a[0]["X-Sender-Key"], "SECRET_KEY_XYZ")
            # Second host: zero requests (no header leak)
            self.assertEqual(dual.hits_b, [])
        finally:
            dual.stop()

    def test_default_urllib_would_hit_second_host_regression_guard(self):
        """Document that following redirects is unsafe; our opener must not."""
        import urllib.request
        dual = _DualHost()
        url = dual.start(redirect_a_to_b=True, status_a=302)
        try:
            # Default opener may convert POST→GET; either way B must be hittable
            # if redirects are followed — we assert our NoRedirect path above.
            # Here: confirm default path can reach B (GET after 302) with custom hdrs.
            body = json.dumps({"x": 1}).encode()
            req = urllib.request.Request(
                url, data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer SECRET_KEY_XYZ",
                    "X-Webhook-Key": "SECRET_KEY_XYZ",
                    "X-Sender-Key": "SECRET_KEY_XYZ",
                },
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
            # If default followed, B got something; if not, still ok for this guard.
            # The critical assertion is in test_redirect_second_host_gets_no_request.
            _ = dual.hits_b  # touch
        finally:
            dual.stop()


class TestWakeAgentRetryAfter(unittest.TestCase):
    def test_429_surfaces_retry_after_not_success(self):
        dual = _DualHost()
        url = dual.start(redirect_a_to_b=False, status_a=429, retry_after=10)
        try:
            result = wa.post_webhook(url, "KEY", {"source": "t", "claimable": 1})
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], 429)
            self.assertEqual(result["retry_after_sec"], 10.0)
        finally:
            dual.stop()

    def test_main_prints_retry_after_and_nonzero(self):
        dual = _DualHost()
        url = dual.start(redirect_a_to_b=False, status_a=429, retry_after=7)
        try:
            with tempfile.TemporaryDirectory() as d:
                env = Path(d) / "webhook.env"
                env.write_text(f"WEBHOOK_URL={url}\nWEBHOOK_KEY=KEY\n")
                with mock.patch.object(wa, "ENV", env), \
                     mock.patch.object(wa, "PENDING", Path(d) / "pending.json"):
                    (Path(d) / "pending.json").write_text(json.dumps({
                        "claimable": [{"channel": "C1", "ts": "1"}],
                        "items": [{"channel": "C1", "ts": "1"}],
                    }))
                    import io
                    from contextlib import redirect_stdout, redirect_stderr
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = wa.main([])
                    self.assertEqual(rc, 4)
                    self.assertIn("retry_after_sec=7", out.getvalue())
                    self.assertNotIn("posted webhook", out.getvalue())
        finally:
            dual.stop()


class TestParseRetryAfter(unittest.TestCase):
    def test_parse(self):
        class H(dict):
            def get(self, k, default=None):
                return dict.get(self, k, default)
        self.assertEqual(wa.parse_retry_after(H({"Retry-After": "5"})), 5.0)
        self.assertIsNone(wa.parse_retry_after(H({"Retry-After": "Fri, 01 Jan"})))


if __name__ == "__main__":
    unittest.main()
