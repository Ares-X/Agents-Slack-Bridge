"""Mark inbox messages delivered by ts. 处理成功后调用，避免丢消息。

Usage: python inbox_ack.py <ts> [<ts> ...]
"""
import fcntl
import json
import os
import sys

INBOX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox.jsonl")


def main():
    want = set(sys.argv[1:])
    if not want or not os.path.exists(INBOX_PATH):
        return
    with open(INBOX_PATH, "r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            out = []
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts") in want:
                    r["delivered"] = True
                out.append(json.dumps(r, ensure_ascii=False))
            f.seek(0)
            f.truncate()
            f.write("\n".join(out) + "\n")
            print("acked:", sorted(want))
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
