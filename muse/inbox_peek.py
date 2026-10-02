"""Print undelivered inbox messages as JSON lines. 只读不标记。"""
import fcntl
import json
import os

INBOX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox.jsonl")


def main():
    if not os.path.exists(INBOX_PATH):
        return
    with open(INBOX_PATH) as f:
        fcntl.flock(f, fcntl.LOCK_SH)
        try:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if not r.get("delivered"):
                    print(json.dumps(r, ensure_ascii=False))
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
