"""One-shot pending fallback using the SAME claim/send/ack pipeline as consumer.

Usage (from grokbot/):
  python pending_notify.py          # write pending.json (claimable only in items)
  python pending_consume_once.py    # process claimable via reply_pipeline

Does not generate LLM replies — uses a stub line unless --text is given.
For real agent replies: claim via reply_pipeline after generating text, or
pass text per message via agent orchestration calling process_one().
"""
import argparse
import sys

from inbox_store import escalate_stale_sending, peek_claimable
from reply_pipeline import process_one
import os

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="", help="reply text for all claimable")
    ap.add_argument("--reply-in-thread", action="store_true")
    args = ap.parse_args()
    n = escalate_stale_sending(INBOX)
    if n:
        print(f"escalated {n} stale sending → uncertain", file=sys.stderr)
    rows = peek_claimable(INBOX)
    if not rows:
        print(0)
        return 0
    text = args.text or "（pending_consume_once stub — replace with agent reply）"
    done = 0
    for m in rows:
        r = process_one(
            INBOX, m, text, reply_in_thread=args.reply_in_thread, root=BASE
        )
        print(f"{m.get('channel')}:{m.get('ts')} → {r.get('outcome')}")
        if r.get("outcome") in ("sent_acked", "sent_ack_pending", "acked"):
            done += 1
    print(done)
    return 0


if __name__ == "__main__":
    sys.exit(main())
