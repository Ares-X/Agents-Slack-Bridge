#!/usr/bin/env python3
"""Wake-guard for the Slack inbox watcher hooks.

Reads inbox_peek JSON lines from stdin, decides which undelivered messages
(for WATCH_CHANNEL) may wake the worker, and prints a decision JSON.

Wake budget per msg_id (2026-10-07 fix for the 10-07 wake storm that burned
~60M tokens): first sight -> wake once ("slack_new_message"); if still
unacked ESCALATE_AFTER seconds after the first wake -> one escalation wake
("slack_stale_unacked", payload records carry "stale_retry": true); after
that the message never wakes again. State lives in WAKE_STATE (JSON);
WATCH_DRY=1 runs read-only (no state write, for hook dry-runs).
"""
import json
import os
import sys
import time


def main():
    chan = os.environ["WATCH_CHANNEL"]
    state_path = os.environ["WAKE_STATE"]
    dry = os.environ.get("WATCH_DRY") == "1"
    escalate_after = int(os.environ.get("ESCALATE_AFTER", "1800"))
    now = time.time()

    try:
        with open(state_path) as f:
            state = json.load(f)
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}

    undelivered = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("channel") == chan and not r.get("delivered") and r.get("msg_id"):
            undelivered.append(r)

    wake_msgs, reason = [], ""
    for r in undelivered:
        mid = r["msg_id"]
        st = state.get(mid)
        if not st:
            wake_msgs.append(r)
            state[mid] = {"count": 1, "first": now}
            reason = "slack_new_message"
        elif st.get("count", 0) == 1 and now - st.get("first", now) >= escalate_after:
            r2 = dict(r)
            r2["stale_retry"] = True
            wake_msgs.append(r2)
            state[mid] = {"count": 2, "first": now}
            if not reason:
                reason = "slack_stale_unacked"
        # count >= 2: never wake again for this message

    # prune: forget acked messages; cap state size
    live = {r["msg_id"] for r in undelivered}
    state = {k: v for k, v in state.items() if k in live}
    if len(state) > 1000:
        drop = sorted(state, key=lambda k: state[k].get("first", 0))[: len(state) - 1000]
        for k in drop:
            del state[k]

    if not dry:
        tmp = state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_path)

    print(json.dumps({"reason": reason, "msgs": wake_msgs}, ensure_ascii=False))


if __name__ == "__main__":
    main()
