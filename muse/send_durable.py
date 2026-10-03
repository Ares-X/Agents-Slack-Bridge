#!/usr/bin/env python3
"""Agent 消费链路的持久化单次投递。

真实消费入口是：hook（slack-dm-watch / slack-general-watch）-> side
chat -> agent。agent 用真实模型生成回复正文后，经 stdin 交给本脚本
投递，而不是直接调 send.py。

本脚本与 consumer/poll_consumer.py 共享同一套防重复发送状态机
（deliver_one），只差在正文来源：这里是调用方的真实回复，那边是
参考实现的 echo 示例。

用法：
  echo "回复正文" | python send_durable.py <msg_id> [--thread-ts <ts>]
                                     [--mention <UID> ...]

  msg_id 形如 "<channel>:<ts>"（与 inbox_ack.py 一致）。

退出码（调用方据此决定下一步，绝不靠猜）：
  0 = 已发送并确认 / 已被其他消费者完成 / 已确认（幂等跳过）
  75 = 限流：retry_at 已持久保存，到期前本脚本不会重发；
       调用方不得 ack，不得提前重试
  1 = 明确证明未发送（claim 已释放，下轮可干净重试；不得 ack）
  2 = 结果不确定（已保持 uncertain，走 history 核验；不得 ack、
       不得盲目重发）
  3 = 发送成功但 ack 失败（已记 unacked，只重试 ack，绝不重发正文）
  4 = 发送状态损坏（fail-closed：拒绝发送，等人工恢复）
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "consumer"))
sys.path.insert(0, BASE)

from poll_consumer import deliver_one, resolve_bot_identity  # noqa: E402
from send_state import SendState, StateCorruptError  # noqa: E402

STATE_PATH = os.path.join(BASE, "consumer", "send_state.json")
INBOX_PATH = os.path.join(BASE, "inbox.jsonl")
INBOX_LOCK_PATH = os.path.join(BASE, "inbox.lock")

# deliver_one 结果 -> 本脚本退出码
EXIT = {
    "replied": 0,
    "already-acked": 0,
    "acked-later": 0,
    "verified-acked": 0,
    "retry-deferred": 75,
    "send-failed": 1,
    "uncertain-held": 2,
    "deferred": 2,
    "sent-unacked": 3,
    "verified-unacked": 3,
}


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    msg_id = args[0]
    if ":" not in msg_id:
        print(f"bad msg_id (want <channel>:<ts>): {msg_id}", file=sys.stderr)
        return 1
    channel = msg_id.rsplit(":", 1)[0]
    thread_ts = ""
    mentions = []
    i = 1
    while i < len(args):
        if args[i] == "--thread-ts" and i + 1 < len(args):
            thread_ts = args[i + 1]
            i += 2
        elif args[i] == "--mention" and i + 1 < len(args):
            mentions.append(args[i + 1])
            i += 2
        else:
            print(f"unknown arg: {args[i]}", file=sys.stderr)
            return 1

    text = sys.stdin.read()
    if not text.strip():
        print("empty reply text from stdin, refusing to send",
              file=sys.stderr)
        return 1

    bot_id, bot_user_id = resolve_bot_identity()
    try:
        state = SendState(STATE_PATH, inbox_path=INBOX_PATH,
                          inbox_lock_path=INBOX_LOCK_PATH)
        state.ensure_usable()
    except StateCorruptError as e:
        print(f"send state corrupt, refusing to send (fail-closed): {e}",
              file=sys.stderr)
        return 4
    except Exception as e:
        print(f"cannot open send state: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 4

    m = {"msg_id": msg_id, "channel": channel, "thread_ts": thread_ts}
    try:
        result = deliver_one(m, text, mentions, state, bot_id, bot_user_id)
    except StateCorruptError as e:
        print(f"send state corrupt during delivery (fail-closed): {e}",
              file=sys.stderr)
        return 4
    print(f"send_durable: {msg_id} -> {result}")
    return EXIT.get(result, 2)


if __name__ == "__main__":
    sys.exit(main())
