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
SLACK_ALLOWED_USERS=U01ABC2DEF3 # 授权 Member ID，逗号分隔。不设=拒收一切消息
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
| 其他 bot | `allow_bots: mentions` 时，对方消息里明确 @ 了本 bot 才受理 | 同上 |
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

- `mentions` 是安全默认——**每回合都要明确点名**，点名 → 单回 → 停
- 避免 `allow_bots: all`（两个互相全收的 bot 会无限对话）
- Hermes 永远忽略自己的消息（防自回环，无法关闭）
- 对方 bot 引用旧消息时把 @ 转纯文本名字，防二手点名触发回环

## 读取历史

Hermes 在会话内自动带 thread 上下文（adapter 内置 `conversations.replies` 水位缓存），一般不需要手动读。需要补频道级上下文（多 agent 对账、审计）时用本目录 CLI：

```bash
# 频道最近 20 条（正序 JSON lines；token 自动从 env 或 ~/.hermes/.env 取）
python3 channel_history.py C01234567890 20
# 某条消息的 thread
python3 channel_history.py C01234567890 --thread 1234567890.123456
# 顺带把 user ID 解析成显示名
python3 channel_history.py C01234567890 20 --resolve
```

失败输出 `{"error": …}`——如实报告，不编造。需要更深的历史回放/搜索走 Hermes 的 `session_search` 工具或 Slack SDK。

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
| 两个 bot 打乒乓 | 检查双方都是 `allow_bots: mentions`，绝不 `all` |
| token 疑似泄露 | Slack 后台 rotate + 更新 `~/.hermes/.env`，聊天记录里出现过的立即作废 |

## 安全

- token 只存 `~/.hermes/.env`（0600）；**永不提交 git、永不贴进聊天**
- 本仓库零凭据：清单里的 token 都是占位符
- `SLACK_ALLOWED_USERS` 必设，否则网关默认拒收所有消息（fail-closed）
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
