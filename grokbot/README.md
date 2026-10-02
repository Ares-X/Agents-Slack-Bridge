# Grok Bot Slack Bridge

本目录是 **Grok Bot / Cursor Grok Bot** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

与 `muse/` 的差异：
- **默认允许其他 bot 的 @mention**（多 agent 频道友好；仅丢弃自己）
- **~5s 本地 consumer**（或可选 `pending.json` + agent `@every 5m` fallback）
- **默认频道顶层回复**（`REPLY_IN_THREAD=0`）

> 给另一个 agent 的完整配置清单见 **[AGENT.md](./AGENT.md)**。

## 快速开始

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
cd Agents-Slack-Bridge/grokbot

cp .env.example .env && chmod 600 .env   # 填 token，永不提交
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

./venv/bin/python bridge.py &            # Socket Mode 监听
./venv/bin/python consumer/poll_consumer.py   # ~5s 消费
```

用 `manifest.yaml` 在 https://api.slack.com/apps 建 App → 拿 `xoxb-` / `xapp-` → 邀请 bot 进频道。

## 文件树

```
grokbot/
├── README.md / AGENT.md
├── manifest.yaml
├── .env.example
├── requirements.txt
├── bridge.py                 # 监听 → inbox.jsonl（默认允许多 agent bot）
├── send.py / inbox_peek.py / inbox_ack.py / channel_history.py
├── pending_notify.py         # 未处理 → pending.json（5min agent fallback）
├── slack-bridge.service
├── slack-consumer.service
└── consumer/
    └── poll_consumer.py      # ~5s 轮询；generate_reply() 可换 LLM
```

运行时（不提交）：`inbox.jsonl`、`bridge.log`、`pending.json`、`consumer/channel_sessions.json`。

## 环境变量要点

| 变量 | 说明 |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | 必填 |
| `SLACK_BOT_USER_ID` | 可选；空则 `auth_test` |
| `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` | 可选白名单；都空 = 允许任意其他 bot |
| `SLACK_BRIDGE_POLL_SEC` | 默认 `5` |
| `REPLY_IN_THREAD` | 默认 `0`（顶层回复） |
| `PROXY_URL` / `CA_BUNDLE` | 可选代理 |

## 两种消费模式

1. **本地 5s consumer**（推荐有本机进程时）：`consumer/poll_consumer.py`
2. **Agent 例程 fallback**：`pending_notify.py` → agent 读 `pending.json` → `send.py` → `inbox_ack.py`

## 安全

token 只在 `.env`（0600）；永不进 git / 聊天。详见仓库根 README 与 `AGENT.md`。
