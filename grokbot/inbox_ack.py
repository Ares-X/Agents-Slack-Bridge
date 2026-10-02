"""Mark inbox messages delivered by (channel, ts). 处理成功后调用，避免丢消息。

Ack identity matches dedupe: channel + ts (NOT ts alone — same ts can appear
in different channels).

Usage:
  python inbox_ack.py <channel> <ts> [<channel> <ts> ...]
  python inbox_ack.py <channel>:<ts> [<channel>:<ts> ...]

Exit codes:
  0 — all requested keys matched (idempotent if already delivered)
  1 — usage / parse error, or none of the keys found
  2 — partial: some keys missing
"""
import os
import sys

from inbox_store import ack_keys, parse_ack_argv

INBOX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox.jsonl")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(
            "usage: inbox_ack.py <channel> <ts> [...]  OR  channel:ts [...]",
            file=sys.stderr,
        )
        return 1
    try:
        want = parse_ack_argv(argv)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    n, missing = ack_keys(INBOX_PATH, want)
    if n == 0:
        print("acked: none (no matching channel+ts)", file=sys.stderr)
        return 1
    shown = sorted(f"{c}:{t}" for c, t in want if (c, t) not in missing)
    print("acked:", shown)
    if missing:
        miss = sorted(f"{c}:{t}" for c, t in missing)
        print("missing:", miss, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
