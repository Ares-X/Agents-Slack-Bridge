"""Write undelivered inbox summary to pending.json (agent @every 5m fallback).

IMPORTANT: do NOT send.py → inbox_ack.py directly for exported items.
Fallback MUST reuse the same claim → send → ack rules as the 5s consumer:

  python pending_consume_once.py
  # or: from reply_pipeline import process_one

pending.json splits:
  - claimable: reply_status empty/retryable (safe to run through pipeline)
  - blocked: sending/sent/uncertain (no blind resend; ack-only if sent)

Ack identity = channel + ts.
"""
import json
import os
import time

from inbox_store import is_claimable, peek_undelivered

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")
PENDING = os.path.join(BASE, "pending.json")

rows = peek_undelivered(INBOX)
claimable = []
blocked = []
for r in rows:
    st = r.get("reply_status")
    if is_claimable(st):
        claimable.append(r)
    else:
        blocked.append({
            "channel": r.get("channel"),
            "ts": r.get("ts"),
            "reply_status": st,
            "note": (
                "ack_only" if st == "sent"
                else "no_resend_verify"
            ),
        })

payload = {
    "updated_at": time.time(),
    "instruction": (
        "Use reply_pipeline.process_one / pending_consume_once.py. "
        "Never raw send.py→inbox_ack.py (bypasses claim/uncertain guards)."
    ),
    "claimable": claimable,
    "blocked": blocked,
    # backward-compatible alias: only claimable (not all undelivered)
    "items": claimable,
}

with open(PENDING, "w") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
print(len(claimable))
