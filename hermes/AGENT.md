# AGENT.md — 给任意 agent 的 Hermes×Slack 配置指令

把本文件整段复制给任意 agent（Claude Code / Codex / 另一个 Hermes / 其他 CLI agent），它就能端到端把 Hermes Agent 接入 Slack。规则和步骤都是自包含的。

---

你要为用户把 **Hermes Agent** 接入 Slack。Hermes 自带 Slack 平台插件（Socket Mode，出站连接，无需公网），不要自建 bridge/consumer——用 Hermes 的原生配置面。

## 铁律（先读）

1. **永不提交 `.env`**；永不把 `xoxb-` / `xapp-` token 贴进聊天或日志；token 只写目标机 `~/.hermes/.env`（`chmod 600`）。
2. token 曾出现在聊天/日志里 → 立刻让用户去 Slack 后台 rotate。
3. `SLACK_ALLOWED_USERS` 必设（逗号分隔 Member ID）——不设则网关默认拒收所有消息。
4. 改 Slack App 的 scope/事件订阅后**必须重装 App** 才生效——提醒用户。
5. 只在 Hermes 主目录（`~/.hermes`）和用户指定的目录操作；不要碰无关配置。
6. 多 bot 频道：`allow_bots: mentions` 是安全默认，绝不建议 `all`（回环风险）。

## 路径

### A. 已装 Hermes

跳到步骤 2。

### B. 未装 Hermes

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

（装完重开 shell 或 `source ~/.bashrc` 让 `hermes` 进 PATH。）

## 步骤

### 1. 取 manifest

manifest 不进仓库——由本机 Hermes 直接生成（装完即有，升级后重跑即最新版）：

```bash
hermes slack manifest --agent-view --write   # 写到 ~/.hermes/slack-manifest.json
```

### 2. 建 Slack App

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**
2. 选 workspace，粘贴 manifest 全文 → Create
3. **Basic Information → App-Level Tokens → Generate Token**：勾 `connections:write` → 复制 `xapp-` token
4. **Install App** → Install to Workspace → 复制 `xoxb-` token
5. 让用户在 Slack 里查自己的 Member ID：头像 → Full profile → ⋮ → Copy member ID（`U` 开头）

### 3. 写配置（token 不回显）

```bash
# ~/.hermes/.env 追加（chmod 600）：
SLACK_BOT_TOKEN=<xoxb-，从安装页复制>
SLACK_APP_TOKEN=<xapp-，从 App-Level Tokens 复制>
SLACK_ALLOWED_USERS=<用户的 Member ID>
SLACK_HOME_CHANNEL=<可选：cron 默认频道 C…>
```

```bash
# ~/.hermes/config.yaml 追加（顶层键，与 platforms: 同级不存在时新建）：
```

```yaml
platforms:
  slack:
    enabled: true
    # home_channel: {platform: slack, chat_id: C…, name: general}
    extra:
      reply_in_thread: false
      allow_bots: mentions
      rich_blocks: true
```

或交互式：`hermes gateway setup` → 选 Slack。

### 4. 启动

```bash
hermes gateway            # 前台跑，看日志确认 slack adapter connected
# 稳了再装服务：
hermes gateway install
hermes gateway status
```

### 5. 验收（全部通过才算完）

1. Slack 里 `/invite @Hermes Agent` 进频道
2. DM 发消息 → bot 回
3. 频道里 `@Hermes Agent hello` → bot 回（thread 或顶层，按 reply_in_thread）
4. `python3 channel_history.py <channel_id> 5` 能列出消息（token 从 `~/.hermes/.env` 自动取）
5. `hermes gateway status` 显示 slack 已连接

### 6. 交付报告

向用户报告：App 名、两个 token 已落盘（不回显）、gateway 状态、验收 1–4 结果、cron 示例一条。不输出任何 token。

## 速查（agent 常用）

- **触发**：DM 免 @；频道需 @；thread 内首 @ 后自动跟随；其他 bot 需在消息里明确 @ 我（`allow_bots: mentions`）
- **读历史**：会话内自动带 thread 上下文；补频道上下文用 `channel_history.py <channel> [limit] [--thread ts] [--resolve]`
- **cron**：`hermes cron add "every 1h" "任务描述" --deliver slack`（或 `slack:C…` 指定频道、`slack:U…` 直投 DM；输出 `MEDIA:/path` 自动上传为 Slack 文件）
- **多 agent**：每个 bot 都设 `allow_bots: mentions`；点名 → 单回 → 停；引用旧消息把 @ 写成纯文本名字防二手回环
- **排障**：DM 通频道不通 = `message.channels`/`message.groups` 事件 + `channels:history`/`groups:history` scope + 重装；改 scope/事件必重装
- **升级后 slash 命令刷新**：`hermes slack manifest --agent-view --write` → App Manifest 页粘贴 → 按提示重装

## 常见坑

- `.env` 值带引号/空格：纯值即可，不要加引号
- `SLACK_ALLOWED_USERS` 填了邮箱或用户名 → 无效，必须 Member ID（`U` 开头）
- 频道 ID：右键频道 → View channel details → 底部 Channel ID（`C`/`G` 开头）
- thread 里不能用斜杠命令（Slack 限制），用 `!cmd` 前缀（`!stop`、`!queue`）
- gateway 没起或 token 错 → `hermes gateway status` / 前台跑看日志第一步定位
