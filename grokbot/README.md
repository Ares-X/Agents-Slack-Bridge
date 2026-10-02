# Grok Bot Slack Bridge

本目录是 **Grok Bot / Cursor Grok Bot** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

与 `muse/` 的差异：
- **默认允许其他 bot 的 @mention**（多 agent 频道友好；仅丢弃自己）
- **~5s 本地 consumer**（或可选 `pending.json` + agent `@every 5m` fallback）
- **默认频道顶层回复**（`REPLY_IN_THREAD=0`），同伴 agent 看得见
- **每次回复前拉 `channel_history`** 作上下文

> 给另一个 agent 的完整配置清单见 **[AGENT.md](./AGENT.md)**（默认按多 agent 协作配置）。

## 多 agent 协作默认

| 项 | 默认 | 说明 |
|---|---|---|
| 其他 bot @ 本 bot | **允许**（仅丢弃自己） | 勿改回「丢弃全部 bot」；收紧用 `ALLOWED_BOT_*` |
| 回复前读上下文 | **是** | `channel_history.py` / consumer 内置 |
| 回复位置 | **频道顶层**（`REPLY_IN_THREAD=0`） | 让同频道 peers 看到；勿默认跟帖 |
| 协作礼仪 | 有用单回、不 @ 自己、防回环 | 点名→回→停；可选白名单收紧 |

## 快速开始

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
cd Agents-Slack-Bridge/grokbot

cp .env.example .env && chmod 600 .env   # 填 token；保持 REPLY_IN_THREAD=0；永不提交
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

./venv/bin/python bridge.py &            # Socket Mode 监听（默认允许多 agent bot）
./venv/bin/python consumer/poll_consumer.py   # ~5s 消费；每次先拉频道历史
```

用 `manifest.yaml` 在 https://api.slack.com/apps 建 App → 拿 `xoxb-` / `xapp-` → 邀请 bot 进频道。

## 文件树

```
grokbot/
├── README.md / AGENT.md
├── manifest.yaml
├── .env.example              # REPLY_IN_THREAD=0；ALLOWED_BOT_* 可选
├── requirements.txt
├── bridge.py                 # 监听 → inbox.jsonl（默认允许多 agent bot，仅丢弃自己）
├── send.py / inbox_peek.py / inbox_ack.py / channel_history.py
├── pending_notify.py         # 未处理 → pending.json（5min agent fallback）
├── slack-bridge.service
├── slack-consumer.service
└── consumer/
    └── poll_consumer.py      # ~5s 轮询；回复前 channel_history；generate_reply() 可换 LLM
```

运行时（不提交）：`inbox.jsonl`、`bridge.log`、`pending.json`、`consumer/channel_sessions.json`。

## 环境变量要点

| 变量 | 说明 |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | 必填 |
| `SLACK_BOT_USER_ID` | 可选；空则 `auth_test` |
| `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` | 可选白名单；**都空 = 允许任意其他 bot**（多 agent 默认） |
| `SLACK_BRIDGE_POLL_SEC` | 默认 `5` |
| `REPLY_IN_THREAD` | 默认 `0`（**顶层回复**，多 agent 可见）；`1` = 跟帖 |
| `PROXY_URL` / `CA_BUNDLE` | 可选代理 |

## 两种消费模式

1. **本地 5s consumer**（推荐有本机进程时）：`consumer/poll_consumer.py`（内置拉历史 + 顶层回复）
2. **Agent 例程 fallback**：`pending_notify.py` → agent 读 `pending.json` → **先** `channel_history.py` → `send.py`（默认勿带 `--thread-ts`）→ `inbox_ack.py`

## 安全

token 只在 `.env`（0600）；永不进 git / 聊天。详见仓库根 README 与 `AGENT.md`。
