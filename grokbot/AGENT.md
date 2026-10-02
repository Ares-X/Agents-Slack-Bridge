# AGENT.md — Configure Grok Bot Slack Bridge end-to-end

给**另一个 agent** 的逐步指令。按顺序做；**永不**把 token 写进聊天、日志、git。

## 前置

- 有 Slack workspace 管理员权限（或能创建 App）
- 本机可出站访问 `https://slack.com` / `wss://wss-primary.slack.com`（Socket Mode）
- Python 3.9+

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

3. **Fill secrets locally（never commit）**
   ```bash
   cp .env.example .env && chmod 600 .env
   # 编辑 .env：填 SLACK_BOT_TOKEN / SLACK_APP_TOKEN
   # 可选：SLACK_BOT_USER_ID（留空则 auth_test 自动取）
   # 多 agent 默认可不设 ALLOWED_BOT_*；要白名单再填逗号分隔 ID
   ```

4. **venv + deps**
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

5. **Run bridge（Socket Mode listener）**
   ```bash
   # 前台试跑：
   ./venv/bin/python bridge.py
   # 或 systemd：改 slack-bridge.service 路径后 enable --now
   ```
   日志 `bridge.log` 应出现 `socket mode connected, listening`。

6. **Run consumer（~5s local poll）**
   ```bash
   ./venv/bin/python consumer/poll_consumer.py
   # 或 systemd：slack-consumer.service
   ```
   - 默认 `REPLY_IN_THREAD=0`（频道顶层回复）
   - 把 `generate_reply()` 换成 LLM / wake Grok Bot agent

7. **Invite & verify**
   - `/invite @Grok Bot` 进测试频道
   - 私信 bot 或 `@Grok Bot hello`
   - `./venv/bin/python inbox_peek.py` 应看到未处理行
   - `echo 'ping' | ./venv/bin/python send.py <channel_id>` 应在 Slack 出现

8. **Optional slower fallback（无本地 LLM）**
   - 不跑 consumer；用 cron/`@every 5m` 调 `python pending_notify.py`
   - Agent 读 `pending.json` → 生成回复 → `send.py` → `inbox_ack.py <ts>`
   - 比 5s consumer 慢，适合纯 agent 例程

## 安全红线

- **Never commit** `.env`、`inbox.jsonl`、`pending.json`、`channel_sessions.json`、`*.log`
- **Never paste** `xoxb-` / `xapp-` 进聊天；用 secret store / 0600 文件
- 若 token 曾泄露：Slack 后台立刻 **reinstall / rotate**
- 不把真实 bot user ID（如生产环境的 `U…`）硬编码进仓库；用 `SLACK_BOT_USER_ID` 或 `auth_test`

## 文件职责速查

| 文件 | 作用 |
|---|---|
| `bridge.py` | Socket Mode → `inbox.jsonl`（只收） |
| `send.py` | stdin 正文 → `chat.postMessage` |
| `inbox_peek.py` / `inbox_ack.py` | 读未处理 / 按 ts 标记 |
| `channel_history.py` | 拉频道近 N 条（带重试、显示名） |
| `consumer/poll_consumer.py` | ~5s 轮询 + 模板/LLM 回复 |
| `pending_notify.py` | 未处理 → `pending.json`（5min agent fallback） |
| `manifest.yaml` | 建 App 用 |
