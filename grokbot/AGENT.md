# AGENT.md — Configure Grok Bot Slack Bridge end-to-end

给**另一个 agent** 的逐步指令。按顺序做；**永不**把 token 写进聊天、日志、git。

先按顶层 [AGENTS.md](../AGENTS.md) 和[统一配置流程](../docs/setup.md)检查现状，一次收集缺失前提。已有连接、配置和授权直接复用；先确认目标确实是 Grok Bot，以及真实 agent 的唤醒入口，不根据执行者名称猜路线。

**默认目标：多 agent 频道协作**——同频道其他 bot 可以 @ 你；你每次回复前读上下文；默认在频道顶层回复，让同伴看得见。

## 前置

- 有 Slack workspace 管理员权限（或能创建 App）
- 本机可出站访问 `https://slack.com` / `wss://wss-primary.slack.com`（Socket Mode）
- Python 3.9+

## 多 agent 协作默认（必读）

独立成文（wake 时优先）：[PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md) · 唤醒入口：[wakeup.md](./wakeup.md) / [AGENT_WAKE.md](./AGENT_WAKE.md)

以下区分代码默认与本次授权范围；配置时采用用户已明确的频道和对端：

1. **允许其他 bot @ 本 bot**  
   `bridge.py` **不要**默认丢弃全部 bot 消息。默认放行其他 bot，仅丢弃自己的 `user_id`（防自循环）。  
   用 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS` 表达指定对端（逗号分隔）；任一非空 = 白名单模式。两者都空会允许任意其他 bot；这不是频道或人类用户授权，所需范围还需在实际入口核对。

2. **每次回复前拉频道历史**  
   按频道/线程整体读 pending，核对最新授权任务与后续限制，再跑
   `channel_history.py <channel> [N]`；相关线程用 `--thread-ts <ROOT_TS> --all`
   （`--thread` 同义）。正文完整保留，必要时用 `--all` / `--cursor` 翻页。
   失败时记录上下文缺口，**不得假装已读或据此猜测回复/静默决定**。

3. **默认频道顶层回复**  
   `REPLY_IN_THREAD=0`（`.env.example` 默认）。  
   若设 `1`：跟帖目标 = 已有 `thread_ts`，否则用本消息 `ts`（顶层消息也能开帖）。  
   **已有来源线程始终保留**，包括 thread 内的 mention 与 `kind=thread_reply`（用户在本 bot 消息下跟帖、无需 @）；`0` 不会把这些回复搬到顶层。

4. **协作礼仪**  
   - 被其他 agent `@` 时，先判断是否有助于当前授权任务；通知、纯确认、审批提示、
     已被当前任务吸收的旧控制消息可以带原因静默完成，不要求每源各发一条。
   - **仅在请求对方具体下一步动作时**使用 `<@USER_ID>`；引用、致谢、确认、
     状态报告用普通名字，避免这些消息再次叫醒对方。
   - **不要 @ 自己**；允许授权任务的实质讨论持续多轮，不给整场任务设单轮上限。
   - 引用对方旧消息时，把 `@` 转成纯文本名字，降低二手触发。  
   - 需要收紧对端时用 `ALLOWED_BOT_*`，不要关掉「读历史」或改回「丢弃全部 bot」。

## 步骤

1. **Clone & enter**
   已有部署先检查 Git 状态、当前服务和队列，复用路径；以下仅用于新 checkout。
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
   test -e .env || cp .env.example .env
   chmod 600 .env
   # 编辑 .env：填 SLACK_BOT_TOKEN / SLACK_APP_TOKEN
   # 可选：SLACK_BOT_USER_ID（留空则 auth_test 自动取）
   # 保持 REPLY_IN_THREAD=0（多 agent 可见）
   # 根据已授权对端设置 ALLOWED_BOT_*；保留已有合法配置
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

6. **接入真实 agent，再运行 consumer（~5s local poll）**
   - 使用现有平台支持的唤醒入口，在私有 `webhook.env` 配置 `WEBHOOK_URL` 和 `WEBHOOK_KEY`，权限设为 `0600`。不要把值贴进聊天；这是 agent webhook，不是 Slack Incoming Webhook。
   - 在 `.env` 设置 `REPLY_MODE=agent_wake`。URL/key 的取得、接收端注册和模型登录取决于实际平台，仓库不提供 webhook 接收服务器或 Grok 模型客户端。
   - 把 [AGENT_WAKE.md](./AGENT_WAKE.md) 和 [PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md) 绑定到真正接收唤醒的 agent。核实它能访问同一部署的队列与工具，在其执行环境把 `GROK_BRIDGE_DIR` 设为已核实的绝对部署目录；远端接收成功不等于可以读本机队列。
   ```bash
   ./venv/bin/python wake_agent.py --check
   ./venv/bin/python consumer/poll_consumer.py
   # 已有运行实例不再前台重复启动；使用现有 supervisor 加载
   ```
   - `--check` 只验证 webhook 字段存在，不验证网络、认证或真实模型执行。
   - `agent_wake` 只唤醒；接收端必须实际读取 pending、最新频道/线程上下文并决定回复或静默，使用 `pending_consume_once.py` / `reply_pipeline` 完成持久发送。
   - 未显式选择模式且无 `webhook.env` 时 consumer 拒绝启动；无效模式同样拒绝。不能切成模板掩盖缺少真实 agent。
   - 可选自建本地模型路线：`generate_reply()` 原为模板 stub，替换为实际模型并使用 `history` 后，还须显式选择当前名为 `REPLY_MODE=template` 的执行分支。此定制路线需单独验收，不是默认 Grok 接线。
   - ACK 只用于已发送的 `sent` 状态；不以 raw send→ACK 绕过持久管线。
   - 若 send 成功但 ack 失败：下一轮只重试 ack，不重发
   - listener 与 consumer 都要常驻。systemd 仅适用于支持它的宿主；同机多个 bridge 使用不同服务名，并同步修改 consumer 依赖。不要让现有服务与另一个 keepalive 同时管理同一进程。

7. **Invite & verify（含多 agent）**
   - `/invite @Grok Bot` 进测试频道
   - 私信 bot 或 `@Grok Bot hello` → 应入队
   - consumer 模板回复出现 ≠ Agent/LLM 集成完成（见下表）
   - 让另一个 agent `@Grok Bot`：应自动唤醒真实 agent、读历史、在**来源线程或约定顶层**有用回复
   - `./venv/bin/python inbox_peek.py` 检查处理前后状态，不能通过手动 ACK 清空未处理消息来通过验收
   - `./venv/bin/python channel_history.py <channel_id> 10` 能列出近况
   - 再按[统一验收清单](../docs/setup.md#4-验收实际效果)验证上下文内容、双向交接与共同任务。历史读取成功或 webhook HTTP 2xx 不是自动触发模型的证据。

8. **Optional slower fallback（无本地 LLM）**
   - 仅在明确选择此替代路线时不跑 consumer，使用实际平台支持的任务调度入口唤醒 agent，由它执行 `pending_notify.py` 及后续流程。只定时运行该脚本会生成 JSON，不会唤醒模型。
   - Agent 读 `pending.json` → **先** `channel_history.py` → 生成回复 → 经 `reply_pipeline` / `pending_consume_once.py`（禁止 raw send→ack）
   - 真实 `agent_wake` 由外部 Agent 依据当前上下文决定回复或静默，模板测试不能证明
     其语义行为。`pending_consume_once.py --channel C --ts T --no-reply --reason '原因'`
     只消费未发送 claimable，保留原文、`resolution_reason`、`resolved_at`；发送、ACK
     与静默共用队列锁。ACK 只允许 `sent`，不能清掉 `sending/uncertain/rate_limited`。
   - 比 5s consumer 慢，适合纯 agent 例程；协作规则同上

## 验收：模板回复 ≠ Agent 集成

| 现象 | 结论 |
|---|---|
| inbox 有行、模板回复或 webhook 返回 2xx | 仅证明对应组件，尚未证明真实 agent 集成 |
| 实际 consumer/hook 已加载，真实入站自动触发模型并使用上下文，正确位置回复且完成状态可核对 | **已验收所测单端路线**；协作范围另按对端与方向记录 |
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
| `consumer/poll_consumer.py` | ~5s；默认真实 agent 唤醒路线；ACK 恢复；显式选择的模板/自建模型分支 |
| `pending_notify.py` | 未处理 → `pending.json`（5min agent fallback） |
| `manifest.yaml` | 建 App 用 |
| `tests/` | 无 live Slack 单元测试 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

Live Slack E2E：**NOT_EXERCISED** unless explicitly authorized.
