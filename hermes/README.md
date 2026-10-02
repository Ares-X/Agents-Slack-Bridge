# Hermes Agent × Slack（原生平台插件）

本目录是 **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** 的 Slack 接入包。
Hermes 自带一等的 Slack 平台插件（slack-bolt **Socket Mode**，出站 WebSocket，无需公网入口），
**不需要** `muse/`、`grokbot/` 那类自建桥——直接用 Hermes 自己的配置面。

> 给另一个 agent 的完整配置清单见 **[AGENT.md](./AGENT.md)**（整段复制给它即可）。

## 和兄弟目录的差异

| | `muse/` / `grokbot/` | `hermes/` |
|---|---|---|
| 形态 | 自建 bridge.py + consumer 轮询 | Hermes 原生平台插件（gateway 进程内） |
| 触发 | inbox 队列轮询 | 事件直达 agent 回合 |
| 会话/记忆/cron | 自己写 | 全部内置（session、memory、skills、cron、slash 命令） |
| 依赖 | slack_sdk | Hermes 本体（`pip install slack-bolt` 随安装器带上） |

## 架构

```
Slack ──DM/@mention/斜杠命令──▶ Slack App（Socket Mode）
        ──出站 WebSocket──▶ hermes gateway（本机/服务器）
        ──▶ agent 回合（工具/记忆/skill）──▶ chat.postMessage 回 Slack
```

## 快速开始

### 0. 安装 Hermes

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

### 1. 建 Slack App（manifest 一把梭）

manifest 由 Hermes 自带命令生成（含全部内置 slash 命令、bot scope、事件订阅——Hermes 升级后重跑即得新版，无需依赖本仓库）：

```bash
hermes slack manifest --agent-view --write   # 写到 ~/.hermes/slack-manifest.json
```

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**
2. 选 workspace，粘贴生成的 manifest 全文 → **Create**
3. **Settings → Socket Mode** 应已开启（manifest 自带）；Basic Information → **App-Level Tokens** → Generate（scope 含 `connections:write`）→ 复制 `xapp-` token
4. **Settings → Install App** → Install to Workspace → 复制 Bot User OAuth Token（`xoxb-`）
5. manifest 里 messages tab 已开，无需再手动启用

> Hermes 升级新增 slash 命令后：重跑上面命令，到 App Manifest 页粘贴更新，Slack 要求时重装。

### 2. 配置 Hermes

token 与授权走 `~/.hermes/.env`（参考 [.env.example](./.env.example)）：

```bash
SLACK_BOT_TOKEN=xoxb-…          # Bot Token
SLACK_APP_TOKEN=xapp-…          # App-Level Token（connections:write）
SLACK_ALLOWED_USERS=U01ABC2DEF3 # 授权 Member ID，逗号分隔（人类消息的前提，见「授权模型」）
SLACK_HOME_CHANNEL=C01234567890 # 可选：cron/通知默认频道
```

行为走 `~/.hermes/config.yaml`（参考 [config.example.yaml](./config.example.yaml)）：

```yaml
platforms:
  slack:
    enabled: true
    home_channel: {platform: slack, chat_id: C01234567890, name: general}
    extra:
      reply_in_thread: false   # 频道顶层直接回复；true=开 thread
      allow_bots: mentions     # 多 agent 协作推荐值（见下）
      rich_blocks: true        # Block Kit 富渲染（表格/嵌套列表）
```

或直接交互式向导：`hermes gateway setup` → 选 Slack。

### 3. 启动

```bash
hermes gateway            # 前台跑一次看日志
hermes gateway install    # 装成 systemd user service（开机自启）
hermes gateway status     # 看连接状态
```

### 4. 验收

- Slack 里 `/invite @Hermes Agent` 进目标频道
- DM 直接说话；频道里 `@Hermes Agent 你好`
- 终端能跑 `hermes status` / `hermes gateway status` 看到 slack adapter connected

## @ 方式（触发模型）

| 场景 | 触发 | 回复位置 |
|---|---|---|
| 1:1 DM | 无需 @，每条都回 | DM |
| 频道顶层 | 必须 `@bot`（`require_mention: true` 默认） | `reply_in_thread: false` → 频道顶层；`true` → thread |
| thread 内 | 首次 @后，同 thread 后续免 @自动跟随 | 原 thread |
| 其他 bot（经典 bot 帖：`bot_id`、无 `user_id`） | `allow_bots: mentions` 时，对方消息里明确 @ 了本 bot 才受理 | 同上 |
| 其他 bot（带 `user_id` 的 app/automation 帖） | 上者之外，**其 user_id 还必须通过用户授权**（`SLACK_ALLOWED_USERS` 等），否则静默拒绝 | 同上 |
| 斜杠命令 | `/new` `/model` `/help` …（50 个，原生注册） | ephemeral（仅自己可见） |
| thread 里的命令 | Slack 禁止 thread 内斜杠，用 `!cmd` 前缀（`!queue`、`!stop`） | thread 内 |

细粒度开关：`strict_mention`（每条都要 @，禁自动跟随）、`thread_require_mention`（thread 内强制 @）、`free_response_channels`（免 @ 频道白名单）、`mention_patterns`（自定义触发词）。

## 多 agent 协作（防回环）

本仓库的三 bot 同频道实测配置（Hermes 视角）：

```yaml
platforms:
  slack:
    extra:
      allow_bots: mentions   # 对方 bot 消息里明确 @ 我才受理
      reply_in_thread: false
```

配合顶层 `slack.strict_mention: true` 更严。铁律：

- `mentions` 是协作起点——**每回合都要明确点名**，点名 → 单回 → 停
- 避免 `allow_bots: all`（两个互相全收的 bot 会无限对话）
- Hermes 永远忽略自己的消息（防自回环，无法关闭）；网关另有 bot loop guard（每会话滑动窗口超额冷却）兜底
- 对方 bot 引用旧消息时把 @ 转纯文本名字，防二手点名触发回环
- `allow_bots` 管「bot 消息受不受理」，**不是身份白名单也非绝对防回环保证**——授权全貌见下节

## 授权模型（以源码为准）

> 以下按 hermes-agent commit `b3059921bc`（origin/main，2026-10-02）核对；`9a71d5a8..b3059921` 期间
> `gateway/authz_mixin.py` 与 `plugins/platforms/slack/adapter.py` 的授权路径零改动。
> 入站链：`adapter._drop_bot_sender`（含 own-echo/`allow_bots` gate）→ adapter `_early_reject_unauthorized`
> （注入的网关 authz 回调，带 `is_bot`）→ 网关 `_is_user_authorized`（`_chat_scoped_grant` 内
> `{PLATFORM}_ALLOW_BOTS` 短路 → allow-all → pairing → allowlist → default-deny）→ bot loop guard。

**三件套分工，别混为一谈：**

1. **用户授权**（谁能说话）：`SLACK_ALLOWED_USERS`（逗号分隔 Member ID）是主开关；辅助有
   `SLACK_ALLOW_ALL_USERS=true`、`GATEWAY_ALLOWED_USERS`、pairing 配对码、`GATEWAY_ALLOW_ALL_USERS`。
   - 人类消息：不设任何 allowlist/pairing → **默认拒收**（fail-closed）。没配白名单不等于全员放行。
   - 带法：Member ID（`U…`）精确匹配；填邮箱/用户名无效。

2. **bot 触发条件**（bot 消息额外过的一道 gate，`allow_bots`）：
   - `none`（默认）：所有 bot 帖丢弃。
   - `mentions`：对方 bot 的消息**明确 @ 本 bot** 才受理。
   - `all`：全部受理（仅靠 own-echo 丢弃 + bot loop guard 兜底，不推荐）。
   - **关键分叉**——对方 bot 的帖子里带不带 `user_id`：
     - **经典 bot 帖**（`bot_id`/`bot_profile`、**无** `user_id`，含 Slack Workflow Builder 无 user 的帖子）：
       `_chat_scoped_grant` 里 `{PLATFORM}_ALLOW_BOTS` 短路在 no-user-id guard **之前**生效——
       即 `allow_bots: mentions` + 明确 @ 就能进，**无需**把它加进 `SLACK_ALLOWED_USERS`。
     - **带 `user_id` 的 bot/app/automation 帖**（app 发的且带 user 字段、`app_id`+无 `client_msg_id` 签名）：
       adapter 的早期授权检查带着 user_id 先行，**user_id 必须本身通过用户授权**（allowlist/pairing/allow-all），
       否则在 allow_bots gate 之前就被静默拒绝。想接这类发送者：把它的 user_id 加进 `SLACK_ALLOWED_USERS`，
       且 `allow_bots` 不能是 `none`。
   - `allow_bots` 不是身份白名单：它只决定「bot 签名的消息要不要受理」，身份与授权始终由上面的用户授权层裁决。

3. **频道范围**（在哪响应）：`allowed_channels`（只在这些频道响应，DM 豁免）、`free_response_channels`
   （免 @ 白名单）、`ignore_other_user_mentions`、`strict_mention`/`thread_require_mention`。
   授权≠响应：**已授权**用户在非 `allowed_channels` 频道发消息，bot 也会静默不响应。
   事件订阅面另有限制：`app_mention` 只在 bot 已加入的频道/DM 生效（自身家 bot 例外）。

**独立验证**（上游 smoke profile，`tests/gateway/test_slack_peer_agent_smoke.py` 同款加载方式，12/12 断言过）：

| # | 场景 | allowlist | allow_bots | @本bot | 结果 |
|---|---|---|---|---|---|
| A | 人类、无任何 allowlist | 无 | — | — | 拒（fail-closed） |
| B | 无 user_id 的 bot 帖 | 无 | mentions | 是 | **过**（ALLOW_BOTS 短路） |
| C | 带 user_id 的 bot 帖 | 无 | mentions | 是 | 拒（早期检查 user_id 未授权） |
| D | 经典 bot 帖 | 有(人类) | mentions | 是 | 过 |
| E | 经典 bot 帖 | 有(人类) | none | 是 | 拒（adapter gate） |
| F | 本 bot 自己的帖子 | 有(人类) | all | — | 拒（own-echo） |
| G | 经典 bot 帖、无 @ | 有(人类) | mentions | 否 | 拒（adapter gate） |

> 上游自带的同类回归：`tests/gateway/test_discord_bot_auth_bypass.py`（#4466：`DISCORD_ALLOW_BOTS` 无
> allowlist 也放行 is_bot source）、`tests/gateway/test_slack_peer_agent_smoke.py`（peer-agent 路由不变量）。

## 读取历史

Hermes 在会话内自动带 thread 上下文（adapter 内置 `conversations.replies` 水位缓存），一般不需要手动读。需要补频道级上下文（多 agent 对账、审计）时用本目录 CLI：

```bash
# 频道最近 20 条（正序 JSON lines；token 自动从 env 或 ~/.hermes/.env 取）
python3 channel_history.py C01234567890 20
# 某条消息的 thread（limit 放 --thread 前后都行）
python3 channel_history.py C01234567890 --thread 1234567890.123456
python3 channel_history.py C01234567890 --thread 1234567890.123456 10
# 顺带把 user ID 解析成显示名
python3 channel_history.py C01234567890 20 --resolve
```

排序依据（Slack 官方文档）：`conversations.history` 最新在前，脚本反转为旧→新；
`conversations.replies` 本身旧→新（父消息开头），脚本不再二次反转——两种模式输出统一为时间正序。

失败输出一行结构化 `{"error": {"kind", "detail", …}}` 并退出码 1——`kind` ∈ `missing_token /
http_429 / timeout / connection_failed / invalid_json / slack_api_error`；429 带 `retry_after`（读
Slack 的 `Retry-After` 头，秒）。不自动重试，调用方按 `retry_after` 自行调度。如实报告，不编造。需要
更深的历史回放/搜索走 Hermes 的 `session_search` 工具或 Slack SDK。

## Cron / 定时任务

Hermes cron 原生支持 Slack 投递：

```bash
# 对话里直接说或命令行：
hermes cron add "every 1h" "总结一下最新动态" --deliver slack
hermes cron add "in 30m" "提醒我上线" --deliver slack:C0123456789   # 指定频道
hermes cron add "every 2h" "盯价格" --deliver slack:U0123456789    # 直投某人 DM
```

- 默认 `deliver: origin`（谁创建投回哪）；`"slack"` = home channel（`SLACK_HOME_CHANNEL` 或 `platforms.slack.home_channel`）
- cron 输出里的 `MEDIA:/path/file` 会作为原生 Slack 文件分享上传
- 投递不依赖 gateway 在线（standalone sender 用 `SLACK_BOT_TOKEN` 兜底）
- 管理命令：`hermes cron list` / `edit` / `remove` / `pause` / `resume`

## 排障速查

| 症状 | 解法 |
|---|---|
| DM 能回、频道不回 | 加 `message.channels`/`message.groups` 事件订阅 + `channels:history`/`groups:history` scope → **重装 App** |
| 收不到附件 | 加 `files:read` scope → 重装 |
| "发送消息已关闭" | App Home → Messages Tab 开启 |
| 群发 DM（MPIM）没反应 | 加 `message.mpim` 事件 + `mpim:history`/`mpim:read` scope → 重装 |
| 改了 scope/事件没生效 | **必须重装 App**（Install App 页会提示） |
| 两个 bot 打乒乓 | 双方都 `allow_bots: mentions`（绝不 `all`）；仍有兜底：网关 bot loop guard 对超额会话冷却 |
| token 疑似泄露 | Slack 后台 rotate + 更新 `~/.hermes/.env`，聊天记录里出现过的立即作废 |

## 安全

- token 只存 `~/.hermes/.env`（0600）；**永不提交 git、永不贴进聊天**
- 本仓库零凭据：清单里的 token 都是占位符
- `SLACK_ALLOWED_USERS` 必设：不设时人类消息默认拒收（fail-closed）；无 `user_id` 的 bot 帖在
  `allow_bots: mentions` + 明确 @ 下仍可进入（详见「授权模型」）——「不设白名单=拒收一切」
  只对人类消息成立，别当成绝对承诺
- Socket Mode = 纯出站连接，不暴露公网入口
- 提交前自检：`git grep -E 'xoxb-[0-9]|xapp-[0-9]'` 应无真实 token

## 文件树

```
hermes/
├── README.md             # 本文档
├── AGENT.md              # 给任意 agent 的端到端配置指令（整段复制）
├── config.example.yaml   # ~/.hermes/config.yaml 的 platforms.slack 片段
├── .env.example          # token 模板（真实值永不进仓）
└── channel_history.py    # 频道/线程历史 CLI（stdlib-only，零依赖）
```
