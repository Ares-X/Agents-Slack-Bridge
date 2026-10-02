"""One-shot pending fallback using the SAME claim/send/ack pipeline as consumer.

Usage (from grokbot/) — **single message only** (channel + ts required):

  python pending_notify.py
  python pending_consume_once.py --channel C012 --ts 1234.5 --text 'reply'
  # or positional: python pending_consume_once.py C012 1234.5 --text 'reply'
  # ack-only for already-sent: omit --text (or pass empty)

NEVER pass --text alone: that used to blast the same body to every actionable
row (cross-channel wrong send + mass ACK). Targeting is mandatory.

Actionable target = claimable OR sent (ack-only). sending/uncertain are
skipped (no auto-resend). rate_limited waits are honored by process_one.
"""
from __future__ import annotations

import argparse
import os
import sys

from inbox_store import escalate_stale_sending, msg_key, peek_actionable, peek_undelivered
from reply_pipeline import process_one

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")


def find_target(inbox: str, channel: str, ts: str) -> dict | None:
    want = (str(channel), str(ts))
    for r in peek_undelivered(inbox):
        if msg_key(r) == want:
            return r
    return None


def consume_one(
    inbox: str,
    channel: str,
    ts: str,
    text: str,
    *,
    reply_in_thread: bool = False,
    root: str = BASE,
    runner=None,
) -> dict:
    """Process exactly one (channel, ts) row. Raises ValueError if missing."""
    m = find_target(inbox, channel, ts)
    if m is None:
        raise ValueError(f"no undelivered row for {channel}:{ts}")
    st = m.get("reply_status")
    if st in ("sending", "uncertain"):
        return {
            "action": "skip",
            "outcome": "no_resend",
            "detail": st,
            "channel": channel,
            "ts": ts,
        }
    reply = "" if st == "sent" else text
    if st != "sent" and not (reply or "").strip():
        raise ValueError(
            "claimable row requires non-empty --text "
            f"(got empty for {channel}:{ts})"
        )
    r = process_one(
        inbox, m, reply, reply_in_thread=reply_in_thread, root=root, runner=runner
    )
    r = dict(r)
    r["channel"] = channel
    r["ts"] = ts
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Send/ack ONE inbox row by channel+ts (no broadcast)."
    )
    ap.add_argument("channel_pos", nargs="?", help="channel id (positional)")
    ap.add_argument("ts_pos", nargs="?", help="message ts (positional)")
    ap.add_argument("--channel", "-c", help="channel id")
    ap.add_argument("--ts", "-t", help="message ts")
    ap.add_argument(
        "--text",
        default=None,
        help="reply text for claimable target (required unless row is sent)",
    )
    ap.add_argument("--reply-in-thread", action="store_true")
    ap.add_argument(
        "--list",
        action="store_true",
        help="list actionable keys and exit (no send)",
    )
    args = ap.parse_args(argv)

    n = escalate_stale_sending(INBOX)
    if n:
        print(f"escalated {n} stale sending → uncertain", file=sys.stderr)

    if args.list:
        rows = peek_actionable(INBOX)
        for r in rows:
            print(f"{r.get('channel')}:{r.get('ts')}\t{r.get('reply_status')!r}")
        print(len(rows))
        return 0

    channel = (args.channel or args.channel_pos or "").strip()
    ts = (args.ts or args.ts_pos or "").strip()
    if not channel or not ts:
        print(
            "error: channel and ts are required "
            "(e.g. --channel C --ts 1.0 --text 'reply'). "
            "Refusing to broadcast --text to all actionable rows.",
            file=sys.stderr,
        )
        return 2

    text = args.text if args.text is not None else ""
    try:
        r = consume_one(
            INBOX, channel, ts, text, reply_in_thread=args.reply_in_thread
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"{channel}:{ts} → {r.get('outcome')}")
    if r.get("outcome") in ("sent_acked", "sent_ack_pending", "acked"):
        print(1)
        return 0
    print(0)
    return 1 if r.get("outcome") not in ("wait_rate_limit", "no_resend") else 0


if __name__ == "__main__":
    sys.exit(main())
