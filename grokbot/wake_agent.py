#!/usr/bin/env python3
"""POST a wake webhook so Grok Bot can craft Slack replies (no template).

Loads WEBHOOK_URL / WEBHOOK_KEY from webhook.env beside this file.
Never prints secret values.

Usage (from deploy root):
  python wake_agent.py              # read pending.json; POST if claimable>0
  python wake_agent.py --check      # only verify webhook.env loads
  python wake_agent.py --force      # POST even when claimable==0
  python wake_agent.py --claimable N --channels C1 C2

Exit: 0 on 2xx (or --check ok / nothing to post); non-zero on missing env or HTTP error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parent
ENV = DIR / "webhook.env"
PENDING = DIR / "pending.json"
UA = "slack-bridge-grokbot-wake/1.0"


def load_webhook() -> tuple[str | None, str | None]:
    if not ENV.exists():
        return None, None
    url = key = None
    for line in ENV.read_text(encoding="utf-8").splitlines():
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


def read_pending() -> tuple[int, list[str]]:
    if not PENDING.exists():
        return 0, []
    try:
        data = json.loads(PENDING.read_text(encoding="utf-8"))
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


def post_webhook(url: str, key: str | None, payload: dict) -> int:
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
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read(4096)
            code = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(code) < 300:
                return 0
            print(f"wake_agent: non-2xx status={code}", file=sys.stderr)
            return 3
    except urllib.error.HTTPError as e:
        print(f"wake_agent: HTTPError status={e.code}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"wake_agent: post failed: {type(e).__name__}", file=sys.stderr)
        return 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Wake Grok Bot via webhook")
    ap.add_argument("--check", action="store_true",
                    help="only verify webhook.env loads; do not POST")
    ap.add_argument("--force", action="store_true",
                    help="POST even when claimable count is 0")
    ap.add_argument("--claimable", type=int, default=None,
                    help="override claimable count (skip pending.json)")
    ap.add_argument("--channels", nargs="*", default=None,
                    help="override channel list")
    args = ap.parse_args()

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
            "Read /workspace/slack-bridge-grokbot/AGENT_WAKE.md; "
            "peek claimable, channel_history, craft reply, "
            "reply_pipeline/process_one or pending_consume_once, then ack."
        ),
        "deploy_dir": str(DIR),
    }
    rc = post_webhook(url, key, payload)
    if rc == 0:
        print(f"wake_agent: posted webhook for {n} claimable "
              f"channels={len(channels)}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
