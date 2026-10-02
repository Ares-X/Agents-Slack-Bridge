"""One-shot pending fallback using the SAME claim/send/ack pipeline as consumer.

Usage (from grokbot/):
  python pending_notify.py          # write pending.json (claimable in items)
  python pending_consume_once.py    # process actionable via reply_pipeline

Actionable = claimable OR sent (ack-only recovery). sending/uncertain are
skipped (no auto-resend).

Does not generate LLM replies — uses a stub line unless --text is given.
"""
import argparse
import os
import sys

from inbox_store import escalate_stale_sending, peek_actionable
from reply_pipeline import process_one

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="", help="reply text for claimable rows")
    ap.add_argument("--reply-in-thread", action="store_true")
    args = ap.parse_args()
    n = escalate_stale_sending(INBOX)
    if n:
        print(f"escalated {n} stale sending → uncertain", file=sys.stderr)
    rows = peek_actionable(INBOX)
    if not rows:
        print(0)
        return 0
    text = args.text or "（pending_consume_once stub — replace with agent reply）"
    done = 0
    for m in rows:
        # sent → ack-only (process_one ignores reply_text); claimable → send
        reply = "" if m.get("reply_status") == "sent" else text
        r = process_one(
            INBOX, m, reply, reply_in_thread=args.reply_in_thread, root=BASE
        )
        print(f"{m.get('channel')}:{m.get('ts')} → {r.get('outcome')}")
        if r.get("outcome") in ("sent_acked", "sent_ack_pending", "acked"):
            done += 1
    print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
