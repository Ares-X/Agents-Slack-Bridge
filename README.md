# Agents Slack Bridge

Slack ↔ Agent 双向桥接：**Socket Mode** 出站长连接，机器只需出站联网，**不需要公网入口**。用户私信 bot 或在频道 `@bot`，本地队列承接，消费层生成回复后发回 Slack。

仓库提供两个独立可部署的 flavor，选一个目录按它自己的 README 配置即可。

---

## 两个 flavor

| 目录 | 面向 | 消费 | 其它 bot @ | 默认回复位置 |
|---|---|---|---|---|
| **[`muse/`](./muse/README.md)** | Muse / 任意带 `generate_reply()` 的通用 LLM agent | ~30s 轮询 consumer（或平台 side chat） | 默认丢弃；白名单 `ALLOWED_BOT_*` 放行 | 有 `thread_ts` 则跟帖 |
| **[`grokbot/`](./grokbot/README.md)** | Grok Bot / Cursor Grok Bot | **5s** 本地 consumer，或可选 **5min** agent 例程（`pending.json`） | **默认允许**其他 bot（仅丢弃自己）；可选白名单收紧 | 默认**频道顶层**（`REPLY_IN_THREAD=0`） |

```
Agents-Slack-Bridge/
├── README.md                 # 本文档
├── .gitignore
├── muse/                     # Muse / 通用 LLM 桥
│   ├── README.md
│   ├── bridge.py, send.py, inbox_*, channel_history.py
│   ├── manifest.yaml, .env.example, slack-bridge.service
│   └── consumer/poll_consumer.py
└── grokbot/                  # Grok Bot 桥
    ├── README.md, AGENT.md
    ├── bridge.py, send.py, inbox_*, channel_history.py, pending_notify.py
    ├── manifest.yaml, .env.example, requirements.txt
    ├── slack-bridge.service, slack-consumer.service
    └── consumer/poll_consumer.py
```

## 架构（共性）

```
Slack ──私信/@mention──▶ Slack App ──Socket Mode（出站 websocket）──▶
bridge.py ──▶ inbox.jsonl ──▶ 消费层 ──▶ send.py ──▶ Slack
```

## 安全

- **仓库内无真实凭据**。Token 只存在你机器上的 `.env`（`chmod 600`）。
- 永不提交：`.env`、`inbox.jsonl`、`pending.json`、`channel_sessions.json`、`*.log`、`venv/`
- Token 若曾出现在聊天/日志里：立刻去 Slack 后台 **rotate / reinstall**
- 用 secret store；**永远不要把 `xoxb-` / `xapp-` 贴进聊天**

---

## Agent 指令

把下面整段复制给任意 agent，即可端到端搭桥：

```
你要为用户配置 Agents Slack Bridge（https://github.com/Ares-X/Agents-Slack-Bridge）。

规则：
- 永不提交 .env；永不把 xoxb-/xapp- token 贴进聊天；用 0600 文件或 secret store。
- 只在 /workspace 或用户指定的持久目录操作。

步骤：
1. git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
   进入仓库根目录。
2. 让用户选择 flavor：
   - muse/     → Muse / 通用 LLM（~30s consumer）
   - grokbot/  → Grok Bot / Cursor Grok Bot（5s consumer 或 pending.json 5min fallback）
   cd 进所选目录。若选 grokbot，同时阅读 grokbot/AGENT.md。
3. 用该目录的 manifest.yaml 在 https://api.slack.com/apps
   「Create New App → From a manifest」创建 App；改 display_name；
   Install；复制 Bot User OAuth Token (xoxb-) 与带 connections:write 的
   App-Level Token (xapp-)；App Home → Messages Tab 允许用户发消息。
4. cp .env.example .env && chmod 600 .env
   填入两个 token（及可选 PROXY_URL / CA_BUNDLE / SLACK_BOT_USER_ID）。
   不要把填好的 .env 内容回显到聊天。
5. python3 -m venv venv && ./venv/bin/pip install slack_sdk
   （grokbot 可用 pip install -r requirements.txt）
6. 启动 bridge：./venv/bin/python bridge.py
   （或按该目录 *.service 配 systemd，WorkingDirectory 指向该 flavor 目录）
   确认 bridge.log 出现 "socket mode connected"。
7. 启动消费层：
   - muse:    ./venv/bin/python consumer/poll_consumer.py
              （把 generate_reply 换成 LLM）
   - grokbot: ./venv/bin/python consumer/poll_consumer.py
              （默认 5s、顶层回复；或改用 pending_notify.py + agent 例程）
8. /invite @bot 进测试频道；私信或 @bot hello；
   ./venv/bin/python inbox_peek.py 应看到消息；
   echo 'hi' | ./venv/bin/python send.py <channel_id> 验证回发。
9. 完成后向用户报告：选用的 flavor、App 名、bridge/consumer 是否在跑、
   验证结果。不要输出任何 token。
```

更细的 Grok 专用步骤见 [`grokbot/AGENT.md`](./grokbot/AGENT.md)；Muse 细节见 [`muse/README.md`](./muse/README.md)。

---

## 许可与贡献

自用 / 内部分享优先。改完请保持 **零凭据进仓**；提交前：

```bash
git grep -E 'xoxb-[0-9]|xapp-[0-9]' -- ':!.git'
# 应无真实 token；占位符 xoxb-your-… / 文档里的前缀说明可以保留
# 另：确认代码里没有硬编码生产 bot user ID，一律用 SLACK_BOT_USER_ID / auth_test
```
