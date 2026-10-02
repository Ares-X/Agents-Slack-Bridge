# AGENT.md — Configure Grok Bot Slack Bridge end-to-end

给**另一个 agent** 的逐步指令。按顺序做；**永不**把 token 写进聊天、日志、git。

**默认目标：多 agent 频道协作**——同频道其他 bot 可以 @ 你；你每次回复前读上下文；默认在频道顶层回复，让同伴看得见。

## 前置

- 有 Slack workspace 管理员权限（或能创建 App）
- 本机可出站访问 `https://slack.com` / `wss://wss-primary.slack.com`（Socket Mode）
- Python 3.9+

## 多 agent 协作默认（必读）

独立成文（wake 时优先）：[PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md) · 唤醒入口：[wakeup.md](./wakeup.md) / [AGENT_WAKE.md](./AGENT_WAKE.md)

配置与回复行为按下列默认执行（除非用户明确要求收紧）：

1. **允许其他 bot @ 本 bot**  
   `bridge.py` **不要**默认丢弃全部 bot 消息。默认放行其他 bot，仅丢弃自己的 `user_id`（防自循环）。  
   需要收紧时再设 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（逗号分隔）；任一非空 = 白名单模式。

2. **每次回复前拉频道历史**  
   处理 inbox 前先跑 `channel_history.py <channel> [N]`（或 consumer 内同等调用）。  
   失败时必须可见降级（日志 / 回复注明），**不得假装已读上下文**。

3. **默认频道顶层回复**  
   `REPLY_IN_THREAD=0`（`.env.example` 默认）。  
   若设 `1`：跟帖目标 = 已有 `thread_ts`，否则用本消息 `ts`（顶层消息也能开帖）。  
   **例外**：`kind=thread_reply`（用户在本 bot 消息下跟帖、无需 @）**始终同线程回复**。

4. **协作礼仪**  
   - 被其他 agent `@` 你时：结合上下文给出有用回答，不要空转或只 echo。  
   - **不要 @ 自己**；点名 → 单回 → 停，避免互相 @ 造成的 echo / 回环风暴。  
   - 引用对方旧消息时，把 `@` 转成纯文本名字，降低二手触发。  
   - 需要收紧对端时用 `ALLOWED_BOT_*`，不要关掉「读历史」或改回「丢弃全部 bot」。

## 步骤

1. **Clone & enter**
   ```bash
   git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
   cd Agents-Slack-Bridge/grokbot
   ```

2. **Create Slack App from manifest**
   - https://api.slack.com/apps → Create New App → From a manifest
   - Paste `manifest.yaml`（可改 `display_name`）
   - Install to workspace
   - Copy **Bot User OAuth Token** (`xoxb-…`) and generate **App-Level Token** with `connections:write` (`xapp-…`)
   - App Home → Messages Tab → allow users to send messages
   - **已有 App**：更新 `manifest.yaml` 后必须到 api.slack.com → App Manifest → 粘贴保存 → 按提示
     Reinstall / 更新事件（新增 `message.channels` / `message.groups` + `groups:*` scopes），
     否则线程跟帖（无需 @）不会进 bridge。改完后重启 `bridge.py`。

3. **Fill secrets locally（never commit）**
   ```bash
   cp .env.example .env && chmod 600 .env
   # 编辑 .env：填 SLACK_BOT_TOKEN / SLACK_APP_TOKEN
   # 可选：SLACK_BOT_USER_ID（留空则 auth_test 自动取）
   # 保持 REPLY_IN_THREAD=0（多 agent 可见）
   # 多 agent 默认可不设 ALLOWED_BOT_*；要白名单再填逗号分隔 ID
   ```

4. **venv + deps**
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

5. **Run bridge（Socket Mode listener）**
   ```bash
   ./venv/bin/python bridge.py
   # 或 systemd：改 slack-bridge.service 路径后 enable --now
   ```
   日志 `bridge.log` 应出现 `socket mode connected, listening`。  
   可靠性：先 durable 写入 `inbox.jsonl`（flock+fsync），再 Socket Mode ACK。  
   DM 的 `message_changed` / `message_deleted` 等 subtype 会被丢弃。

6. **Run consumer（~5s local poll）**
   ```bash
   ./venv/bin/python consumer/poll_consumer.py
   # 或 systemd：slack-consumer.service
   ```
   - 默认 `REPLY_IN_THREAD=0`（频道顶层回复）
   - 每条消息回复前会调 `channel_history`；失败可见
   - **`generate_reply()` 是模板 stub，不是已接线的 Grok 模型**
   - 换成 LLM / wake Grok Bot agent 时仍须使用传入的 `history`
   - ack：`inbox_ack.py <channel> <ts>`（不要只用 ts）
   - 若 send 成功但 ack 失败：下一轮只重试 ack，不重发

7. **Invite & verify（含多 agent）**
   - `/invite @Grok Bot` 进测试频道
   - 私信 bot 或 `@Grok Bot hello` → 应入队
   - consumer 模板回复出现 ≠ Agent/LLM 集成完成（见下表）
   - 让另一个 agent `@Grok Bot`：应入队、读历史、在**频道顶层**有用回复
   - `./venv/bin/python inbox_peek.py` 应看到未处理行
   - `echo 'ping' | ./venv/bin/python send.py <channel_id>` 应在 Slack 出现
   - `./venv/bin/python channel_history.py <channel_id> 10` 能列出近况

8. **Optional slower fallback（无本地 LLM）**
   - 不跑 consumer；用 cron/`@every 5m` 调 `python pending_notify.py`
   - Agent 读 `pending.json` → **先** `channel_history.py` → 生成回复 → 经 `reply_pipeline` / `pending_consume_once.py`（禁止 raw send→ack）
   - 比 5s consumer 慢，适合纯 agent 例程；协作规则同上

## 验收：模板回复 ≠ Agent 集成

| 现象 | 结论 |
|---|---|
| inbox 有行、`replied in …` 打出 | 桥 + **模板 stub** 通路 OK |
| 已把 `generate_reply()` 换成真实 LLM/agent，且使用 `history` | **Agent 集成完成** |
| 仅看到模板套话 | **尚未**完成模型接线 |

## 安全红线

- **Never commit** `.env`、`inbox.jsonl`、`pending.json`、`channel_sessions.json`、`*.log`
- **Never paste** `xoxb-` / `xapp-` 进聊天；用 secret store / 0600 文件
- 若 token 曾泄露：Slack 后台立刻 **reinstall / rotate**
- 不把真实 bot user ID（如生产环境的 `U…`）硬编码进仓库；用 `SLACK_BOT_USER_ID` 或 `auth_test`

## 文件职责速查

| 文件 | 作用 |
|---|---|
| `inbox_store.py` | durable append/claim/ack；corrupt-tail；严格 fsync |
| `reply_pipeline.py` | claim→send→ack 共用状态机 |
| `pending_consume_once.py` | pending fallback 入口（同 pipeline） |
| `bridge.py` | Socket Mode → 先入队再 ACK；DM subtype 过滤；多 agent 默认 |
| `send.py` | stdin 正文 → `chat.postMessage` |
| `inbox_peek.py` / `inbox_ack.py` | 读未处理 / 按 **channel+ts** 标记 |
| `channel_history.py` | 拉频道近 N 条；失败 JSON error + exit 1 |
| `consumer/poll_consumer.py` | ~5s；send/ack 状态机；模板 stub；历史降级 |
| `pending_notify.py` | 未处理 → `pending.json`（5min agent fallback） |
| `manifest.yaml` | 建 App 用 |
| `tests/` | 无 live Slack 单元测试 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

Live Slack E2E：**NOT_EXERCISED** unless explicitly authorized.
