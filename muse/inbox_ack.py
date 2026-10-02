"""Append ack tombstones for processed messages. 处理成功后调用。

Usage: python inbox_ack.py <channel:ts> [<channel:ts> ...]

消息身份统一为 msg_id = "<channel>:<ts>"（与入队去重一致），
跨频道同 ts 不会误确认。确认是追加 tombstone，不做原地重写，
中途中断不会损坏已存消息。

退出码：0 = 全部写入；非 0 = 失败（调用方不得视为已确认）。
"""
import sys

import inbox_store


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: inbox_ack.py <channel:ts> [<channel:ts> ...]",
              file=sys.stderr)
        sys.exit(2)
    bad = [a for a in args if ":" not in a]
    if bad:
        print(f"invalid msg_id (want <channel:ts>): {bad}", file=sys.stderr)
        sys.exit(2)
    try:
        inbox_store.ack(args)
    except Exception as e:
        print(f"ack failed: {e}", file=sys.stderr)
        sys.exit(1)
    print("acked:", sorted(args))


if __name__ == "__main__":
    main()
