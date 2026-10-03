#!/usr/bin/env python3
"""POST a wake webhook so Grok Bot can craft Slack replies (no template).

Loads WEBHOOK_URL / WEBHOOK_KEY from webhook.env beside this file.
Never prints secret values.

Security: HTTP redirects are **refused** (urllib must not follow 3xx and
re-send Authorization / X-Webhook-Key / X-Sender-Key to another host).

Usage (from deploy root):
  python wake_agent.py              # read pending.json; POST if claimable>0
  python wake_agent.py --check      # only verify webhook.env loads
  python wake_agent.py --force      # POST even when claimable==0
  python wake_agent.py --claimable N --channels C1 C2

Exit: 0 on 2xx (or --check ok / nothing to post); non-zero on missing env,
redirect, or HTTP error. On 429, stdout includes `retry_after_sec=N` for the
consumer to honor (failure — not success).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

DIR = Path(__file__).resolve().parent
ENV = DIR / "webhook.env"
PENDING = DIR / "pending.json"
UA = "slack-bridge-grokbot-wake/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse all redirects so auth headers cannot leak to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect refused: {code} -> {newurl}",
            headers,
            fp,
        )


def load_webhook(env_path: Path | None = None) -> tuple[str | None, str | None]:
    env_path = ENV if env_path is None else env_path
    if not env_path.exists():
        return None, None
    url = key = None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k == "WEBHOOK_URL":
            url = v
        elif k in ("WEBHOOK_KEY", "SENDER_KEY", "WEBHOOK_SECRET"):
            key = v
    return url, key


def read_pending(pending_path: Path | None = None) -> tuple[int, list[str]]:
    pending_path = PENDING if pending_path is None else pending_path
    if not pending_path.exists():
        return 0, []
    try:
        data = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception:
        return 0, []
    claimable = data.get("claimable") or data.get("items") or []
    channels: list[str] = []
    seen: set[str] = set()
    for row in claimable:
        ch = str((row or {}).get("channel") or "")
        if ch and ch not in seen:
            seen.add(ch)
            channels.append(ch)
    return len(claimable), channels


def parse_retry_after(headers) -> Optional[float]:
    """Parse Retry-After as seconds (integer delta). Ignore HTTP-date forms."""
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        raw = None
    if raw is None:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


def post_webhook(
    url: str,
    key: str | None,
    payload: dict,
    *,
    opener: Any = None,
    timeout: float = 30,
) -> dict:
    """POST JSON. Never follows redirects. Returns result dict (not success on 3xx/4xx/5xx).

    Keys: ok (bool), status (int|None), retry_after_sec (float|None), error (str|None)
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": UA,
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
        headers["X-Webhook-Key"] = key
        headers["X-Sender-Key"] = key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if opener is None:
        opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(4096)
            code = int(getattr(resp, "status", None) or resp.getcode())
            if 200 <= code < 300:
                return {"ok": True, "status": code, "retry_after_sec": None, "error": None}
            # Non-2xx without HTTPError (unusual)
            return {
                "ok": False,
                "status": code,
                "retry_after_sec": parse_retry_after(getattr(resp, "headers", None)),
                "error": f"non-2xx status={code}",
            }
    except urllib.error.HTTPError as e:
        ra = parse_retry_after(e.headers)
        # Drain body so connection can close cleanly
        try:
            e.read(4096)
        except Exception:
            pass
        err = f"HTTPError status={e.code}"
        if 300 <= int(e.code) < 400:
            err = f"redirect refused status={e.code}"
        return {
            "ok": False,
            "status": int(e.code),
            "retry_after_sec": ra,
            "error": err,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": None,
            "retry_after_sec": None,
            "error": f"{type(e).__name__}",
        }


def same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.netloc) == (pb.scheme, pb.netloc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Wake Grok Bot via webhook")
    ap.add_argument("--check", action="store_true",
                    help="only verify webhook.env loads; do not POST")
    ap.add_argument("--force", action="store_true",
                    help="POST even when claimable count is 0")
    ap.add_argument("--claimable", type=int, default=None,
                    help="override claimable count (skip pending.json)")
    ap.add_argument("--channels", nargs="*", default=None,
                    help="override channel list")
    args = ap.parse_args(argv)

    url, key = load_webhook()
    if not url or not key:
        print("wake_agent: webhook.env missing or incomplete", file=sys.stderr)
        return 2

    if args.check:
        print("wake_agent: webhook.env ok")
        return 0

    if args.claimable is not None:
        n = int(args.claimable)
        channels = list(args.channels or [])
    else:
        n, channels = read_pending()
        if args.channels is not None:
            channels = list(args.channels)

    if n <= 0 and not args.force:
        print("wake_agent: nothing claimable; skip POST")
        return 0

    payload = {
        "source": "slack-bridge",
        "claimable": n,
        "channels": channels,
        "note": (
            "Slack bridge has claimable inbox rows. "
            "Read AGENT_WAKE.md in the deploy dir; "
            "Read pending rows together by channel/thread, recent authorized tasks "
            "and relevant full thread history (--thread-ts TS --all). Decide whether "
            "each row needs a useful reply or quiet resolution. Use "
            "pending_consume_once.py --channel --ts with --text, or --no-reply "
            "--reason. Mention peers only when requesting a concrete next action; "
            "authorized collaboration can continue across multiple turns."
        ),
        "deploy_dir": str(DIR),
    }
    result = post_webhook(url, key, payload)
    if result.get("ok"):
        print(f"wake_agent: posted webhook for {n} claimable "
              f"channels={len(channels)}")
        return 0

    status = result.get("status")
    ra = result.get("retry_after_sec")
    err = result.get("error") or "post failed"
    # Machine-readable line for consumer (never includes secrets)
    if status == 429 and ra is not None:
        print(f"retry_after_sec={ra}")
        print(f"wake_agent: 429 rate limited retry_after_sec={ra}", file=sys.stderr)
        return 4
    if status is not None and 300 <= int(status) < 400:
        print(f"wake_agent: {err}", file=sys.stderr)
        return 5
    print(f"wake_agent: {err}", file=sys.stderr)
    if ra is not None:
        print(f"retry_after_sec={ra}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
