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
  python send_durable.py <msg_id> --no-reply --reason '控制/ACK，无需实质回复'

  msg_id 形如 "<channel>:<ts>"（与 inbox_ack.py 一致）。

退出码（调用方据此决定下一步，绝不靠猜）：
  0 = 已发送并确认 / 已被其他消费者完成 / 静默完成（原因已持久化）
  75 = 限流：retry_at 已持久保存，到期前本脚本不会重发；
       调用方不得 ack，不得提前重试
  1 = 明确证明未发送（claim 已释放，下轮可干净重试；不得 ack）
  2 = 结果不确定（已保持 uncertain，走 history 核验；不得 ack、
       不得盲目重发）
  3 = 发送成功但 ack 失败（已记 unacked，只重试 ack，绝不重发正文）
  4 = 状态损坏、存储失败或静默来源不可确认（fail-closed）
"""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(BASE, "consumer"))
sys.path.insert(0, BASE)

from poll_consumer import deliver_one, resolve_bot_identity  # noqa: E402
from send_state import SendState, StateCorruptError  # noqa: E402
import inbox_store  # noqa: E402

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
    "no-reply": 0,
    "held-sending": 2,
    "held-uncertain": 2,
    "held-unacked": 3,
    "held-retry_wait": 75,
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
    no_reply = False
    reason = ""
    i = 1
    while i < len(args):
        if args[i] == "--thread-ts" and i + 1 < len(args):
            thread_ts = args[i + 1]
            i += 2
        elif args[i] == "--mention" and i + 1 < len(args):
            mentions.append(args[i + 1])
            i += 2
        elif args[i] == "--no-reply":
            no_reply = True
            i += 1
        elif args[i] == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            i += 2
        else:
            print(f"unknown arg: {args[i]}", file=sys.stderr)
            return 1

    if no_reply and (not reason.strip() or mentions or thread_ts):
        print("--no-reply requires --reason and cannot include send options",
              file=sys.stderr)
        return 1
    if reason and not no_reply:
        print("--reason requires --no-reply", file=sys.stderr)
        return 1
    text = "" if no_reply else sys.stdin.read()
    if not no_reply and not text.strip():
        print("empty reply text from stdin, refusing to send",
              file=sys.stderr)
        return 1

    try:
        state = SendState(STATE_PATH, inbox_path=INBOX_PATH,
                          inbox_lock_path=INBOX_LOCK_PATH,
                          recover_sending=not no_reply)
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
        if no_reply:
            if (inbox_store.INBOX_PATH != INBOX_PATH
                    or inbox_store.LOCK_PATH != INBOX_LOCK_PATH):
                raise ValueError("no-reply inbox paths must match send state")
            result = state.complete_no_reply(msg_id, reason,
                                             inbox_store.complete_no_reply)
        else:
            bot_id, bot_user_id = resolve_bot_identity()
            result = deliver_one(m, text, mentions, state, bot_id, bot_user_id)
    except StateCorruptError as e:
        print(f"send state corrupt during delivery (fail-closed): {e}",
              file=sys.stderr)
        return 4
    except Exception as e:
        print(f"durable delivery failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    print(f"send_durable: {msg_id} -> {result}")
    return EXIT.get(result, 2)


if __name__ == "__main__":
    sys.exit(main())
