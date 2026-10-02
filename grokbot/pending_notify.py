"""Write undelivered inbox items to pending.json.

给可选的 Grok Bot @every 5m 例程用：没有本地 LLM / 不跑 5s consumer 时，
agent 定时读 pending.json → 生成回复 → send.py → inbox_ack.py。
比本地 consumer 慢（分钟级），但是纯 agent 侧 fallback。

Ack identity = channel + ts（与去重一致），例如：
  python inbox_ack.py <channel> <ts>
  python inbox_ack.py <channel>:<ts>

Usage (from grokbot/):
  python pending_notify.py
→ 写出 pending.json，stdout 打印未处理条数。
"""
import json
import os
import time

from inbox_store import peek_undelivered

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(BASE, "inbox.jsonl")
PENDING = os.path.join(BASE, "pending.json")

rows = peek_undelivered(INBOX)

with open(PENDING, "w") as f:
    json.dump({"updated_at": time.time(), "items": rows}, f, ensure_ascii=False, indent=2)
print(len(rows))
