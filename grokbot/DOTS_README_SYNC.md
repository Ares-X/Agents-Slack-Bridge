# DOTS sync facts（根 README 由 DOTS 维护；本文件仅供同步参考）

请 DOTS 在更新仓库根 `README.md` 的 grokbot 行/段时对齐下列事实：

1. **可靠性**：先 durable 写入 `inbox.jsonl`（sidecar lock + fsync；dir fsync 失败不吞），再 Socket Mode ACK；ack 用 temp + `os.replace`。
2. **Ack 身份**：`(channel, ts)`；CLI：`inbox_ack.py <channel> <ts>`。
3. **Claim→send→ack**：发送前先 durable `reply_status=sending`；仅 `not_sent:` / `sent ok: False` 可重试；其余 → `uncertain`；`sending` 重启 escalate 为 `uncertain`，永不盲发。
4. **DM**：过滤 edit/delete 等 subtype。
5. **`REPLY_IN_THREAD=1`**：`thread_ts` 否则消息 `ts`；默认 `0`。
6. **历史失败**：错误可见；不得假装已读。
7. **`generate_reply()`**：模板 stub ≠ 模型集成完成。
8. **pending fallback**：必须走 `reply_pipeline` / `pending_consume_once.py`，禁止 raw send→ack。
9. **接收路径**：bridge 入队不做网络查名（快 ACK）。
10. **测试**：`grokbot/tests/`；live Slack **NOT_EXERCISED**；无新第三方依赖。
11. **Corrupt-tail repair** 用 temp+fsync+replace，崩溃不丢旧队列；Slack `internal_error`/`fatal_error` → uncertain；WebClient `retry_handlers=[]`；pending actionable 含 sent ack-only。
