# Muse / Generic Agent Slack Bridge

本目录是 **Muse / 任意带 `generate_reply()` LLM 钩子的 agent** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

> 仓库根目录还有 `grokbot/`（默认允许多 agent）、`hermes/`（原生插件）。请先 `git clone` 根仓库，再 `cd muse`。

### 多 agent 频道协作（默认开启）

muse 桥**默认放行其他 bot 的 @mention**（自己的消息永远过滤，防自循环）——与 grokbot 行为一致，装完即可同频道互相 @、读上下文、协作：

1. **互相 @**：默认无需配置。要收紧成白名单，在 `.env` 填 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（对方 U…/B…，逗号分隔；任一非空即白名单模式）。
2. **每次回复前读上下文**：`channel_history.py <channel> [N]`（`poll_consumer.py` 已调用）；把 `history` 交给 LLM。
3. **尽量顶层回复**：跟帖会藏住回复，其他 agent 不易看到。consumer 默认在有 `thread_ts` 时跟帖——多 agent 协作时可改成不传 `--thread-ts`（与 grokbot 的 `REPLY_IN_THREAD=0` 对齐）。
4. **礼仪**：被 @ 时结合上下文有用回答；不要 @ 自己；点名→单回→停，转述别人 @ 时写纯文本名字，避免回环。

---

## 1. 架构

```
Slack ──① 私信/@mention──▶ Slack App ──② Socket Mode 事件推送（出站 websocket，秒级）──▶
bridge.py ──③ 写入 inbox.jsonl（本地队列）──▶ 消费层（轮询 ~30s）──④ LLM 生成回复 ──▶
send.py ──⑤ chat.postMessage ──▶ Slack
```

延迟构成：①→③ 实时；④→⑤ 取决于消费层轮询间隔 + LLM 生成，约 1~3 分钟。

## 2. 配置步骤（按顺序做）

### 2.1 建 Slack App（5 分钟）

1. 打开 https://api.slack.com/apps → **Create New App → From a manifest** → 选你的 workspace
2. 粘贴本目录 `manifest.yaml` 全文（先把 `display_name` 改成你的 bot 名）→ Create
3. **拿两个 token**：
   - `OAuth & Permissions` 页面顶部 → **Bot User OAuth Token**（`xoxb-` 开头）
   - `Basic Information` → **App-Level Tokens** → Generate（勾 `connections:write`，`xapp-` 开头）
4. **开消息开关**：`Features → App Home → Messages Tab` → 勾选 *Allow users to send messages*（否则用户私信 bot 会看到"发送消息功能已关闭"）
5. 想让 bot 在频道里被 `@`：把 bot **邀请进频道**（`/invite @你的bot名`）

### 2.2 部署代码（10 分钟）

```bash
git clone https://github.com/Ares-X/Agents-Slack-Bridge.git /opt/Agents-Slack-Bridge
cd /opt/Agents-Slack-Bridge/muse

cp .env.example .env && chmod 600 .env
# 编辑 .env，填入 xoxb- / xapp-（以及代理配置，如果有）
# 多 agent 协作：填 ALLOWED_BOT_USERS / ALLOWED_BOT_IDS（对端 U…/B…）

python3 -m venv venv
./venv/bin/pip install slack_sdk
```

### 2.3 常驻服务（5 分钟）

```bash
# 编辑 slack-bridge.service，把 /path/to/ 换成实际路径后：
sudo cp slack-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now slack-bridge.service

# 验证：
systemctl is-active slack-bridge.service   # → active
tail -f bridge.log                          # → "socket mode connected, listening"
```

> ⚠️ `/etc` 下的文件在容器/VM 重建后会消失。**正本永远留在本目录**，并配一个每分钟的健康检查（不存在则从仓库复制 + `daemon-reload` + `restart`）。

### 2.4 接消费层（二选一）

| 方案 | 说明 | 延迟 | 上下文 |
|---|---|---|---|
| **A. `consumer/poll_consumer.py`**（参考实现） | 轮询 inbox → 按 channel 维护 `channel_sessions.json` 会话 → 调你的 LLM → `send.py` 发回 | ~1 分钟 | 按 channel 隔离，文件持久化 |
| **B. 平台 side chat**（如 Muse） | 定时任务把消息转给各 channel 的独立子对话，子对话里的 agent 回复 | 1~3 分钟 | 子对话天然隔离 |

> ⚠️ 方案 A 的 `generate_reply()` **默认只是 echo 示例**（`收到：…`，mention 已脱敏），**不是真实回复**。生产使用必须换成你的模型调用：把函数体替换为 LLM 请求，返回 `str`（正文）或 `(str, [uid...])`（正文 + 显式点名，经 `send.py --mention` 发出真实 @）。
>
> 真实回复位置：`send.py <channel>` 发到频道顶层；带 `--thread-ts` 则跟帖。多 agent 协作想让同伴看见时用顶层（不传 `--thread-ts`）。
>
> 已验证范围：`tests/` 隔离行为测试（并发入队、跨频道同 ts、ack 语义、发送状态机、mention 脱敏、子类型过滤，详见 §8）；**真实 Slack 联调（NOT_EXERCISED）**——未在授权测试频道执行，需部署者按 §6 自行验证。

用 A：把 `generate_reply()` 换成你家 agent 的调用（务必使用传入的 `history` 上下文），`nohup`/`systemd` 跑起来即可。脚本会从 `muse/` 根目录调用 `inbox_peek.py` / `send.py` / `channel_history.py`。多 agent 协作前先配好 bot 白名单；需要同伴看见回复时不要默认跟帖。

## 3. 文件清单与路径

```
Agents-Slack-Bridge/
└── muse/
    ├── README.md                 # 本文档
    ├── manifest.yaml             # Slack App 定义（从 manifest 建应用）
    ├── .env.example              # 凭据模板 → 复制为 .env（0600，不提交）
    ├── bridge.py                 # ★ 核心：Socket Mode 监听 → inbox.jsonl（只收不发；先落盘后 ACK）
    ├── inbox_store.py            # 队列存储：append-only + tombstone ack，锁文件并发控制
    ├── send.py                   # 发消息：echo "正文" | python send.py <channel> [--thread-ts <ts>] [--mention <UID>]...
    ├── inbox_peek.py             # 打印未处理消息（不标记）
    ├── inbox_ack.py              # 按 <channel:ts> 标记已处理（处理成功后调）
    ├── channel_history.py        # 拉频道最近 N 条（python channel_history.py <channel> [N]）
    ├── resolve.py                # ID → 显示名（user|channel）
    ├── slack-bridge.service      # systemd unit（改路径后用，WorkingDirectory=.../muse）
    ├── consumer/
    │   └── poll_consumer.py      # 消费层方案 A 参考实现（~30s 轮询；默认 echo 示例，需接模型）
    └── tests/
        ├── test_store.py         # 并发入队/跨频道同 ts/ack 语义/compact
        ├── test_bridge.py        # 事件分类/子类型过滤/bot 白名单
        └── test_consumer.py      # mention 脱敏/发送状态机/历史降级
```

运行时产生（不提交）：`inbox.jsonl`（队列）、`bridge.log`（日志）、`consumer/channel_sessions.json`（会话）。

所有脚本以 **`muse/` 为 cwd** 运行（`WorkingDirectory=.../muse`）。

## 4. 消息队列格式

`inbox.jsonl` append-only，每行一条。消息身份统一为 `msg_id = "<channel>:<ts>"`
（入队去重与确认都用它，跨频道同 `ts` 互不干扰）：

```json
{"msg_id":"C...:123.456","channel":"C...","user":"U...","text":"@bot 你好",
 "kind":"mention","ts":"123.456","thread_ts":"","received_at":1234567890.0}
{"type":"ack","msg_id":"C...:123.456","at":1234567900.0}
```
- `kind`: `dm`（私信）/`mention`（被@）。热路径不做名称查询，显示名用 `resolve.py` 按需解析。
- 确认是追加 tombstone（`{"type":"ack",...}`），**不做原地重写**——中途中断不会损坏已存消息。
- 消费流程：`inbox_peek.py` 读未确认消息 → 处理 → `inbox_ack.py <channel:ts>` 确认（失败不确认，下轮重试；退出码非 0 = 未确认，调用方不得重发正文）。
- 维护：`python -c "import inbox_store; print(inbox_store.compact())"` 清理已确认记录。
- 旧队列升级：pre-`msg_id` 时代的队列（无 `msg_id`、用 `delivered` 标记）首次被操作时自动迁移为 v2：`delivered=true` 转成 ack tombstone（**绝不**重播已处理消息），`delivered=false` 补上计算出的 `msg_id`。迁移是 temp+fsync+replace 原子重写，可重复跑、可崩溃恢复。

## 4b. 可靠性语义（防重复发送 / 队列 / 恢复）

发送端（`consumer/poll_consumer.py` + `send_state.py`）：
- **发送前先持久化领取**：`spawn send.py` 之前把 `sending` 状态 fsync 落盘。进程崩溃后重启，`sending` 一律转为 `uncertain`，**永远不会**恢复成"可直接发送"。
- **领取时检查持久完成标记**：`claim()` 在同一临界区（`send_state` EX → `inbox` SH）内检查 inbox 的 ack tombstone；另一个 consumer 已经发送+ack+resolve（删条目）后，拿旧快照的 consumer 再领取会得到 `completed`，直接跳过、绝不发送。已配置的队列缺失或读取失败时拒绝领取，不能把读取失败当作“没有完成记录”。
- **只有"明确证明未发送"才重试**：`send.py` 用 `RESULT not-sent` / `sent ok: False` 报告死在 API 调用之前或被 API 明确拒绝；超时、连接中断（`RESULT uncertain`）、输出含糊一律视为不确定，走 history 核验，绝不自动重发。
- **限流持久延期**：`send.py` 收到 HTTP 429 / `ratelimited` / `rate_limited` 后只返回 JSON `{"result":"retry_wait","retry_at":截止时间}`，退出码为 75，本进程不重发。consumer 将该期限持久保存为 `retry_wait`；未到期不发送、不核验、不 ACK，到期后才原子领取新的发送尝试。`Retry-After` 不得因单次进程超时预算被截短；缺失或无效时默认等待 60 秒。重启保留期限，并发只有一个消费者能重新领取，迟到的旧尝试核验不能覆盖延期或新尝试。其他调用 `send.py` 的平台消费层也必须处理退出码 75、保存 `retry_at`，到期前不得重试。连接断开和其他未知结果仍走 uncertain；SDK 自动重试保持关闭。
- **核验必须五项全中**：是我们自己的 bot（`SLACK_BOT_ID`/`SLACK_BOT_USER_ID` 命中其一）、频道/线程一致、消息时间不早于本次发送尝试（允许 60s 时钟偏差）、全文精确匹配（sha256）、**携带与本次发送尝试相同的 `client_msg_id`**。`client_msg_id` 是每次 claim 生成的唯一 id，随 POST 提交、Slack 在 history 里回显，是唯一能把一条 history 消息关联到"这一次发送尝试"的证据；同身份+同正文+时间窗口不能单独证明成功（30 秒前发过相同正文也会命中）。证明不了就保持 uncertain，3 轮无结论转人工日志；旧版本条目（无 `client_msg_id`）永远无法自动确认。
- **ack 抛异常按确认失败处理**：发送成功但 `inbox_ack.py` 子进程崩溃/超时抛异常时，条目记为 `unacked`（只重试 ack），绝不卡在 `sending`。
- **状态文件损坏不静默**：`send_state.json` 加载时按 primary → `.tmp` → `.bak` 顺序尝试，每个候选都要过严格校验（版本号 == 2、`sends` 为 dict、每条 status 合法；未知版本号绝不"迁移"成空状态）。`.tmp` 是崩溃写一半留下的 fsync 过的**更新**版本，恢复安全；`.bak` 是上一次保存**之前**的旧版本——如果 primary 和 `.tmp` 都不可用而只有 `.bak` 可解析，恢复可能是 stale 的（会丢失已持久化的领取记录、把已发送的消息变回可发送），此时**拒绝恢复**：隔离证据并抛 `StateCorruptError`，consumer 在此期间**拒绝发送**（fail-closed），直到人工恢复经证明安全的副本。全部不可用时隔离为 `send_state.json.corrupt.<毫秒>.<primary|tmp|bak>`（每个来源独立后缀、证据不互相覆盖）并留 `.quarantined` 标记，重启也不许静默变空。

队列端（`inbox_store.py`）：
- **去重 tombstone 保留期限：7 天**（`TOMBSTONE_RETENTION_SECONDS`）。Slack 的 at-least-once 重投发生在分钟级，7 天是慷慨上界。
- **恢复规则**：(1) 保留期内重投 → tombstone 命中 → 不再入队；(2) tombstone 过期并被 compact 清掉后重投 → 视为新消息重新入队（at-least-once 的显式取舍；需要更长可调大保留期）；(3) 崩溃导致的队尾残行在下次写入前被隔离为 `inbox.jsonl.corrupt.*`（保留证据）再截断，绝不把新记录粘到残行上。
- **每次成功入队都确认持久化**：新消息与重复消息返回前都确认文件及目录 fsync。首次创建或 compact 后目录同步失败，即使文件已可见，后续不同消息也不能跳过确认；同步失败则抛异常，调用方不得向 Slack ACK（等重投）。
- **目录持久化在成功边界确认**：迁移恢复（replace 后）、首次创建文件、重复追加、ack 首次创建文件，成功返回前都要确认目录 fsync；目录同步持续失败时持续抛异常、持续拒绝 ACK，绝不确认一次可能因崩溃丢失的消息。

## 5. 踩坑清单（实测）

1. **"向此应用发送消息的功能已关闭"** → manifest 漏了 `messages_tab_enabled: true`，去 App Home 手动开。
2. **`/etc` 文件消失** → 见 §2.3 警告，正本放本目录 + 健康检查自愈。
3. **出站代理/TLS 拦截** → `.env` 配 `PROXY_URL` / `CA_BUNDLE`，三个脚本都会读。
4. **频道必须先邀请 bot**，否则收不到 `app_mention`。
5. **多 agent 协作（默认开）**：其他 bot 的 @mention 默认放行，自己的消息永远过滤（防自循环）。要收紧成白名单，把协作对象的 user ID / bot ID 填进 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（任一非空即白名单模式）。防回环纪律：被 @ 才回、回一轮就停、转述别人 @ 时写纯文本名字不写实 @。
6. **回复前读上下文**：消费层应先 `channel_history.py` 再生成回复（参考 `poll_consumer.py`）。
7. **可靠性顺序：先落盘，再 ACK**：`bridge.py` 收到事件后先 `flush+fsync` 写入队列，**成功后才**向 Slack 发 ACK；名称查询等慢操作不在热路径。落盘失败则不 ACK，靠 Slack 重发 + `msg_id` 去重实现 at-least-once（重复入队会被去重丢弃）。
8. **mention 回声脱敏**：默认 echo/转述必须把 `<@U...>` 转成纯文本 `@U...`（不触发通知）；主动点名走 `send.py --mention <UID>` 显式发出。不要为了防回环禁掉全部 mention。
9. **子类型白名单**：DM 里只有无 subtype 和 `me_message` 被当作新消息；`message_changed` / `message_deleted` 等只 ACK 不入队。
8. **中断期消息会丢**（Slack 不补发），健康检查把中断窗口压到分钟级。
9. **回复位置**：默认有 `thread_ts` 则跟帖；多 agent 想让同伴看见时，改成频道顶层（不传 `--thread-ts`）。

## 6. 最小验证

1. `systemctl is-active` → active，日志出现 `socket mode connected`
2. Slack 私信 bot → `python inbox_peek.py` 看到这条 → `echo hi | python send.py <DM频道ID>` → Slack 收到
3. 拉 bot 进测试频道，`@bot hello` → 收到 mention → 消费层回复出现在频道

## 8. 隔离行为测试

```bash
cd muse && python3 -m unittest discover -s tests -v
```
测试覆盖：并发入队不丢不重、跨频道同 `ts`、tombstone ack 语义、compact、事件子类型过滤、bot 白名单两种模式、mention 脱敏/显式点名、ack 失败不重发、发送结果不确定不盲目重发、历史失败延迟→降级。`test_reliability.py` 另有行为测试：旧队列升级（含 delivered=false→true 顺序无关的完成标记优先）、发送结果判定、发送后崩溃恢复、并发 claim 互斥、存储失败（fsync/损坏隔离）、错误回执匹配、wire text 哈希（含 mention 追加）、队尾截断、compact 后重投。`test_reliability_round2.py` 覆盖 stale `.bak` 恢复拒绝、严格版本/结构校验、分来源隔离证据、ack 异常、旧快照、`client_msg_id` 核验和目录持久化。恢复测试另外覆盖目录持续故障下真实 bridge handler 不 ACK、完成记录读取失败不重发，以及限流 60/120 秒跨重启等待、到期领取互斥、错误期限拒绝和迟到核验。仅标准库与模拟网络，无新增依赖。

**NOT_EXERCISED**：真实 Slack 联调未在授权测试频道执行（无凭据、无部署修改），需部署者按 §6 自行验证。

## 7. 安全

- token 只在 `.env`（0600），**永不**进聊天记录、日志、命令行参数、git
- 建议首次配置后去 Slack 后台轮换一次 token
- bot 只能读它加入的频道 + 自己的私信；不想让它看的地方别邀请它
