# Grok Bot Slack Bridge

本目录是 **Grok Bot / Cursor Grok Bot** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

与 `muse/` 的差异：
- **默认允许其他 bot 的 @mention**（多 agent 频道友好；仅丢弃自己）
- **~5s 本地 consumer**（或可选 `pending.json` + agent `@every 5m` fallback）
- **默认频道顶层回复**（`REPLY_IN_THREAD=0`），同伴 agent 看得见；本 bot 线程跟帖（`message.channels`，无需 @）强制同线程回
- **每次回复前拉 `channel_history`** 作上下文（失败可见降级，不假装已读）

> 给另一个 agent 的完整配置清单见 **[AGENT.md](./AGENT.md)**（默认按多 agent 协作配置）。
> 唤醒：[wakeup.md](./wakeup.md) / [AGENT_WAKE.md](./AGENT_WAKE.md)；@-peer 常驻规则：[PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md)。

## 多 agent 协作默认

| 项 | 默认 | 说明 |
|---|---|---|
| 其他 bot @ 本 bot | **允许**（仅丢弃自己） | 勿改回「丢弃全部 bot」；收紧用 `ALLOWED_BOT_*` |
| 回复前读上下文 | **是** | `channel_history.py` / consumer 内置；失败要可见 |
| 回复位置 | **频道顶层**（`REPLY_IN_THREAD=0`） | `1` = 跟帖；`kind=thread_reply` 始终同线程 |
| 协作礼仪 | 有用单回；回 agent **必须**带 `<@USER_ID>`；不 @ 自己；防回环 | 对方 bridge 只在 `<@USER_ID>` 上醒；人类 @ 你时优先回 @，除非明显多余；点名→回→停 |

## 快速开始

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git
cd Agents-Slack-Bridge/grokbot

cp .env.example .env && chmod 600 .env   # 填 token；保持 REPLY_IN_THREAD=0；永不提交
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

./venv/bin/python bridge.py &            # Socket Mode：durable 入队后再 ACK
./venv/bin/python consumer/poll_consumer.py   # ~5s；模式见下，不是默认模板
```

consumer **不是**默认模板 stub（fail-closed）：

| 条件 | 模式 |
|---|---|
| 有 `webhook.env`，且未设置 `REPLY_MODE` | **`agent_wake`**（唤醒，不发模板） |
| 显式 `REPLY_MODE=agent_wake` | **`agent_wake`** |
| 显式 `REPLY_MODE=template` | 模板 stub（**只有**这一档） |
| 没有 `webhook.env`，且 `REPLY_MODE` 未设或非法 | **拒绝启动**，inbox 保留，不会悄悄退回模板 |

用 `manifest.yaml` 在 https://api.slack.com/apps 建 App → 拿 `xoxb-` / `xapp-` → 邀请 bot 进频道。 **已有 App 更新 manifest 后须在 api.slack.com 重新 Apply / 重装**，否则 `message.channels` 事件不到。

### 验收标准（勿混淆）

| 通过项 | 含义 | **不等于** |
|---|---|---|
| 私信 / @mention 出现在 `inbox.jsonl` | 桥接收正常 | Agent 集成完成 |
| consumer 在显式 `REPLY_MODE=template` 时打出 `replied in …` | **模板 stub** 回过一次 | 默认模式；已接线 Grok / LLM |
| `channel_history` 有真实行 | 上下文可读 | — |
| 换成真实 `generate_reply()` / wake agent 且仍用 `history` | **Agent 集成完成** | — |

## 文件树

```
grokbot/
├── README.md / AGENT.md
├── DOTS_README_SYNC.md       # 供 DOTS 同步到根 README 的要点（本包不改根 README）
├── manifest.yaml
├── .env.example
├── requirements.txt          # slack_sdk only
├── inbox_store.py            # durable append/claim/ack；corrupt-tail 隔离；fsync 严格
├── reply_pipeline.py         # claim→send→ack（consumer 与 pending 共用）
├── pending_consume_once.py   # fallback 必须走 pipeline，禁止 raw send→ack
├── bridge.py                 # 先入队再 ACK；DM edit/delete 过滤；多 agent 默认
├── send.py / inbox_peek.py / inbox_ack.py / channel_history.py
├── pending_notify.py         # 未处理 → pending.json（ack 用 channel+ts）
├── slack-bridge.service / slack-consumer.service
├── keepalive.sh              # 只重启缺失进程；flock；30s 与 5min 共用
├── consumer/poll_consumer.py # send/ack 状态机；REPLY_IN_THREAD；历史降级
└── tests/                    # 无 live Slack 的单元测试
```

运行时（不提交）：`inbox.jsonl`、`bridge.log`、`pending.json`、`consumer/channel_sessions.json`。

## 环境变量要点

| 变量 | 说明 |
|---|---|
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | 必填 |
| `SLACK_BOT_USER_ID` | 可选；空则 `auth_test` |
| `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` | 可选白名单；**都空 = 允许任意其他 bot** |
| `SLACK_BRIDGE_POLL_SEC` | 默认 `5` |
| `REPLY_IN_THREAD` | 默认 `0`（顶层）；`1` = 跟帖（无 `thread_ts` 时用消息 `ts`） |
| `PROXY_URL` / `CA_BUNDLE` | 可选代理 |

## 两种消费模式

1. **本地 5s consumer**（推荐）：`consumer/poll_consumer.py`
   - `reply_status=sent` 且未 ack → **只重试 ack，不重发**
   - `reply_status=uncertain` → **不盲发**
   - ack：`python inbox_ack.py <channel> <ts>`（与去重同一身份）
2. **Agent 例程 fallback**：`pending_notify.py`（只导出 claimable）→ `pending_consume_once.py` / `reply_pipeline.process_one`（**禁止** raw `send.py`→`inbox_ack.py`）

## Keepalive

`keepalive.sh` 只在 bridge / consumer 不在时拉起它们。不截断日志，不碰队列。脚本里没有 sleep。

**30 秒循环**和 **5 分钟 agent 例程**调用的是同一个脚本，不要在例程里自己起 python：

```bash
while true; do /workspace/slack-bridge-grokbot/keepalive.sh >> keepalive.log 2>&1; sleep 30; done
```

锁被别人拿着时，`flock -w 20 -E 11` 最多等 20 秒，打一行日志并以 **0** 退出（下一拍再试）。`flock` 不在 `PATH`、锁文件打不开、或 `flock` 返回别的状态（用法错误 / I/O，例如 **64**）会写明原因并以 **非 0** 退出，不会假装「已经等了 20 秒」。

进程算「已在跑」必须同时满足：`argv0` 以 `/venv/bin/python` 结尾、某个参数等于该脚本、cwd 是部署根。不是 `pgrep -f`。shell 命令行里只是提到 `bridge.py` 不算。

### 本分支 vs 正在跑的进程

| | 代码 | flock |
|---|---|---|
| 本 PR（`fix/grokbot-keepalive` 的 `grokbot/keepalive.sh`） | cwd 身份 + `flock -E` | 有 |
| 正在跑的 `/workspace/slack-bridge-grokbot/keepalive.sh` | 旧脚本（cmdline 匹配，无 cwd 锁） | **没有** |

30 秒循环现在执行的是部署目录里的**旧脚本**。本 PR **还没有**换到运行中的进程上；合并或拷贝之前，跑着的 keepalive 不会因为这支分支而改变。

## 测试

keepalive 用例依赖 **Linux**：`/proc`、Bash（`mapfile`）、util-linux `flock`。非 Linux（例如 macOS）这些用例**显式 skip**，不移植；其余测试不依赖它们。

```bash
cd grokbot
python3 -m unittest discover -s tests -v
```

Live Slack 往返需事先授权；未授权时标 **NOT_EXERCISED**。

## 安全

token 只在 `.env`（0600）；永不进 git / 聊天。详见仓库根 README 与 `AGENT.md`。
