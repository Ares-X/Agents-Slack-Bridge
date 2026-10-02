"""Print undelivered inbox messages as JSON lines. 只读不标记。"""
import json
import os
import sys

from inbox_store import peek_undelivered

INBOX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox.jsonl")


def main():
    for r in peek_undelivered(INBOX_PATH):
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
