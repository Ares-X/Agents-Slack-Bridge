# AGENT.md — 给任意 agent 的 Hermes×Slack 配置指令

供具备目标环境访问能力的 agent 配置 Hermes。先读顶层 [AGENTS.md](../AGENTS.md) 与[统一配置流程](../docs/setup.md)：检查现状，一次收集缺失的人工前提，再执行配置和验收；已有授权不重复询问。配置执行者不一定是 Hermes，路线取决于被接入的目标。

---

你要为用户把 **Hermes Agent** 接入 Slack。Hermes 自带 Slack 平台插件（Socket Mode，出站连接，无需公网），不要自建 bridge/consumer——用 Hermes 的原生配置面。

## 铁律（先读）

1. **永不提交 `.env`**；永不把 `xoxb-` / `xapp-` token 贴进聊天或日志；token 只写目标机 `~/.hermes/.env`（`chmod 600`）。
2. token 曾出现在聊天/日志里 → 立刻让用户去 Slack 后台 rotate。
3. `SLACK_ALLOWED_USERS` 必设（逗号分隔 Member ID）——不设则**人类消息**默认拒收（fail-closed）。
   无 `user_id` 的 bot 帖在 `allow_bots: mentions` + 明确 @ 下仍能进（授权细节见仓库 README「授权模型」，
   按 hermes-agent `b3059921bc` 核对）。
4. 改 Slack App 的 scope/事件订阅后**必须重装 App** 才生效——提醒用户。
5. 只在实际 Hermes 实例目录和用户指定的目录操作；默认目录是 `~/.hermes`，但先核实当前实例的配置来源、服务用户与加载路径。不要碰无关配置。
6. 多 bot 频道：`allow_bots: mentions` 是安全默认，绝不建议 `all`（回环风险）。`allow_bots` 只管
   「bot 签名的消息受不受理」，不是身份白名单，也不是绝对防回环保证（还有 own-echo 丢弃、bot loop
   guard 兜底）。

## 路径

### A. 已装 Hermes

检查安装版本、实际 executable、模型/provider 登录、现有 gateway 与 Slack App。复用已有连接和服务；需要新建或更新 App 时仍执行步骤 1 生成 manifest。只跳过已有且已核实完成的步骤，不重复安装或启动 gateway。

### B. 未装 Hermes

把安装权限、目标路径和模型/provider 登录纳入一次前提清单。按 [Hermes 官方仓库](https://github.com/NousResearch/hermes-agent)当前安装说明完成所选环境的安装，核对 `hermes` 实际路径及可用模型；不把某一种 shell 初始化文件或 systemd 当跨平台前提。没有模型访问能力时，不能仅凭 Slack connected 宣称 agent 已可用。

## 步骤

### 1. 取 manifest

manifest 不进仓库——由本机 Hermes 直接生成（装完即有，升级后重跑即最新版）：

```bash
hermes slack manifest --agent-view --write   # 写到 ~/.hermes/slack-manifest.json
```

### 2. 建 Slack App

已有合适 App 时比较并更新必要字段，保留其他功能，不另建重复 App。用户必须处理的安装/授权动作与缺失凭据集中交接，agent 能查询的 ID 自己查询。

1. <https://api.slack.com/apps> → **Create New App** → **From an app manifest**
2. 选 workspace，粘贴 manifest 全文 → Create
3. **Basic Information → App-Level Tokens → Generate Token**：勾 `connections:write` → 复制 `xapp-` token
4. **Install App** → Install to Workspace → 复制 `xoxb-` token
5. 通过授权连接核实用户 Member ID；没有可用查询入口才请用户复制该 ID（`U` 开头）

### 3. 写配置（token 不回显）

```bash
# 默认实例 ~/.hermes/.env 的配置意图；实际修改时按键合并，不重复追加或回显值（chmod 600）：
SLACK_BOT_TOKEN=<xoxb-，从安装页复制>
SLACK_APP_TOKEN=<xapp-，从 App-Level Tokens 复制>
SLACK_ALLOWED_USERS=<用户的 Member ID>
SLACK_HOME_CHANNEL=<可选：cron 默认频道 C…>
```

```bash
# 合并到实际 config.yaml 的现有 platforms.slack；保留其他平台，不生成重复 YAML 键：
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

然后核对已授权频道、对端 user ID、对应版本的授权判断、来源线程回复和真实上下文加载。`home_channel` 是默认投递位置，不是频道访问白名单。若需要仓库中的 836 补丁，先按 [README](./README.md) 检查精确上游版本、前置补丁顺序、脏改及 `git apply --check`；不把示例候选字段直接当作所有版本都生效的配置。协作指引必须进入实际每轮入口，文件存在不代表已加载。

### 4. 启动

先检查当前服务负责人和已有进程。以下用于尚无 gateway 的适用安装；已有实例使用该版本受支持的加载/重启方式，不再并行启动前台进程。

```bash
hermes gateway            # 前台跑，看日志确认 slack adapter connected
# 前台检查结束后，停止该检查实例；按实际 OS/版本支持安装或复用服务：
hermes gateway install
hermes gateway status
```

核对最终服务的实际运行路径、配置、重启策略与新加载进程。不要假定所有 OS 都是 systemd；保留在途任务、会话、审批及其他平台工作。

### 5. 验收（全部通过才算完）

1. Slack 里 `/invite @Hermes Agent` 进频道
2. DM 发消息 → bot 回
3. 频道里 `@Hermes Agent hello` → bot 回（thread 或顶层，按 reply_in_thread）
4. `python3 channel_history.py <channel_id> 5` 能列出消息（token 从 `~/.hermes/.env` 自动取）
5. `hermes gateway status` 显示 slack 已连接
6. 依照[统一验收清单](../docs/setup.md#4-验收实际效果)，确认模型实际使用此前频道事实和线程修正，再分别验收 A→B 与 B→A 的自动触发、正确回复位置和条数
7. 用户要求自然协作时执行一次开放任务，观察自主审查、修订、共同终稿和结束；没有可用对端时报告单端已验收、协作 `NOT_EXERCISED`

### 6. 交付报告

按[统一交付清单](../docs/setup.md#5-交付可用结果)报告：目标版本/ref、私有配置位置、加载证据、实际验收结果与对端方向、状态/日志/重载方法、剩余阻塞。不输出 token，不把保存配置或历史 CLI 成功等同于真实协作通过；不额外创建未要求的 cron。

## 速查（agent 常用）

- **触发**：DM 免 @；频道需 @；thread 内首 @ 后自动跟随；其他 bot 需在消息里明确 @ 我（`allow_bots: mentions`）
- **读历史**：会话内自动带 thread 上下文；补频道上下文用 `channel_history.py <channel> [limit] [--thread ts] [--resolve]`（统一时间正序）。补读更早的频道消息用 `channel_history.py <channel> 100 --before-ts <oldest_ts> --page-info`；`has_more` 或候选上下文的 `Context gap` 意味着覆盖不完整，不能据此认定没有开局/报名。重要状态结论发前先补查，并把频道观察与当前作者的授权指令分开。
- **部署后的工具发现**：核对脚本实际绝对路径，并登记到当前 Hermes 实例已加载的 Slack 专用 skill/操作指引；仓库里存在本文件不代表运行中的 agent 会自动看到它。先做上述 5 条消息只读验收。读历史直接调用脚本，让它读取既有凭据；不要扫描或打印配置中的 token，也不要为读取历史另建发送工具或服务。
- **cron**：`hermes cron add "every 1h" "任务描述" --deliver slack`（或 `slack:C…` 指定频道、`slack:U…` 直投 DM；输出 `MEDIA:/path` 自动上传为 Slack 文件）
- **多 agent 消息触发**：`allow_bots: mentions` 下——**无 `user_id` 的经典 bot 帖**在消息里
  明确 @ 本 bot 就会进来，无需把它加进 `SLACK_ALLOWED_USERS`，会话自动带 thread 上下文；**带
  `user_id` 的 app/automation 帖**则还要求该 user_id 通过用户授权（allowlist/pairing/allow-all），
  否则静默拒绝。每一步最多一条实质终稿，走正常 adapter final 后立即结束该回合并等待新事件；必要的接手、澄清、结果返回可继续有界多轮。不要在 terminal `sleep` 长轮询或 raw curl `chat.postMessage` 另发正文；引用旧消息把 @ 写成纯文本名字防二手回环，需要接手时在终稿明确 @ 对方。原生审批入口继续使用。
- **频道连续协作**：固定 836 默认顶层历史按作者隔离，原生自动补史仅适用于真实 thread。可评估 [自然协作候选补丁](./patches/natural-collaboration-836.patch)，并按 [README](./README.md) 显式配置 workspace:channel 观察范围；候选与离线绿灯不代表已加载或完成真实协作验收。主回复使用 adapter 自动终稿；raw curl 绕过发送工具镜像，不能作为连续 assistant 历史来源。
- **成功静默**：候选只额外允许配置范围内、通过现有授权的 Slack group bot 回合使用精确 `[SILENT]` 结束且不发正文；普通和 queued 作者逐回合判定。无需回复的 ACK/控制事件可静默，但不能隐藏实质工作、人类请求或失败；不要把事件改成 internal 来绕过授权。
- **每轮规则加载**：[增量补丁](./patches/collaboration-turn-contract-836.patch) 在固定 836 + PR17 + PR19 的现有 trusted `channel_prompt` 路径为精确 workspace:channel group 注入短协作规则；DM/范围外保持默认。skill 只在新会话自动绑定，文件落盘不能代替运行时加载。新事件及其 queued 回合携带规则，已有会话通过原 prompt cache signature 生效，无需重置会话；已运行/已创建的旧事件不追溯更新。规则要求正文只走 adapter final、修订先说变化点、最新 ts 对账、未收回复 pending；不把旧版表态当新版接受，不凭沉默/自设冻结期宣称共同通过，遵守实际任务共识规则。加载与真实并发验收见 [README](./README.md#每轮协作规则的运行时加载)。
- **排障**：DM 通频道不通 = `message.channels`/`message.groups` 事件 + `channels:history`/`groups:history` scope + 重装；改 scope/事件必重装
- **升级后 slash 命令刷新**：`hermes slack manifest --agent-view --write` → App Manifest 页粘贴 → 按提示重装

## 常见坑

- `.env` 值带引号/空格：纯值即可，不要加引号
- `SLACK_ALLOWED_USERS` 填了邮箱或用户名 → 无效，必须 Member ID（`U` 开头）
- 频道 ID：右键频道 → View channel details → 底部 Channel ID（`C`/`G` 开头）
- thread 里不能用斜杠命令（Slack 限制），用 `!cmd` 前缀（`!stop`、`!queue`）
- gateway 没起或 token 错 → `hermes gateway status` / 前台跑看日志第一步定位
