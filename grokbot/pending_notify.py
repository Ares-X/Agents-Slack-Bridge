"""Write undelivered inbox summary to pending.json (agent @every 5m fallback).

IMPORTANT: do NOT send.py → inbox_ack.py directly for exported items.
Fallback MUST reuse the same claim → send → ack rules as the 5s consumer:

  python pending_consume_once.py
  # or: from reply_pipeline import process_one

pending.json splits:
  - claimable: ready to send now (incl. rate_limited after wait expiry)
  - waiting_rate_limit: rate_limited with retry_after_until in the future
  - blocked: sending/sent/uncertain (sent = ack-only via pending_consume_once)

Ack identity = channel + ts.
"""
import json
import os
import time

from inbox_store import is_claimable, is_send_ready, peek_undelivered

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")
PENDING = os.path.join(BASE, "pending.json")

now = time.time()
rows = peek_undelivered(INBOX)
claimable = []
waiting_rate_limit = []
blocked = []
for r in rows:
    st = r.get("reply_status")
    if st == "rate_limited" and not is_send_ready(r, now=now):
        waiting_rate_limit.append({
            "channel": r.get("channel"),
            "ts": r.get("ts"),
            "retry_after_until": r.get("retry_after_until"),
            "note": "wait_then_pipeline",
        })
    elif is_send_ready(r, now=now):
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
    "updated_at": now,
    "instruction": (
        "Use reply_pipeline.process_one / pending_consume_once.py. "
        "Never raw send.py→inbox_ack.py. Honor rate_limited wait "
        "(no hammer); retry only after retry_after_until. Read pending rows "
        "together with current channel/thread tasks before deciding. Notifications "
        "and acknowledgements may use --no-reply --reason; do not reply to every row."
    ),
    "claimable": claimable,
    "waiting_rate_limit": waiting_rate_limit,
    "blocked": blocked,
    "items": claimable,
}

with open(PENDING, "w") as f:
    json.dump(payload, f, ensure_ascii=False, indent=2)
print(len(claimable))
