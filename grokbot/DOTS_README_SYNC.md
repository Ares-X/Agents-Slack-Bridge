# DOTS sync facts（根 README 由 DOTS 维护；本文件仅供同步参考）

请 DOTS 在更新仓库根 `README.md` 的 grokbot 行/段时对齐下列事实（勿由 grokbot 维护者直接改根 README）：

1. **可靠性**：Socket Mode 先 durable 写入 `inbox.jsonl`（flock + fsync），再 ACK；ack 重写用 temp + `os.replace`。
2. **Ack 身份**：`(channel, ts)`，与去重一致；CLI：`inbox_ack.py <channel> <ts>`（不要只用 ts）。
3. **Consumer 状态机**：`reply_status=sent` 只重试 ack；`uncertain` 不盲发；避免 ack 失败导致双发。
4. **DM**：过滤 `message_changed` / `message_deleted` 等 subtype，避免空 user/text 任务。
5. **`REPLY_IN_THREAD=1`**：跟帖目标 = 已有 `thread_ts` 否则消息 `ts`；默认仍为 `0` 顶层。
6. **历史失败**：`channel_history` 错误可见；不得声称已读上下文。
7. **`generate_reply()`**：模板 stub；「模板回过一次」≠ Grok/Agent 模型集成完成。
8. **测试**：`grokbot/tests/` 单元测试；live Slack 默认 **NOT_EXERCISED**。
9. **依赖**：仍仅 `slack_sdk`（见 `requirements.txt`）；无新第三方依赖。
