# Muse / Generic Agent Slack Bridge

本目录是 **Muse / 任意带 `generate_reply()` LLM 钩子的 agent** 用的 Slack 桥接包。Socket Mode 出站长连接，无需公网入口。

> 仓库根目录还有 `grokbot/`（Grok Bot / Cursor Grok Bot 专用）。请先 `git clone` 根仓库，再 `cd muse`。

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
| **A. `consumer/poll_consumer.py`**（开箱即用） | 轮询 inbox → 按 channel 维护 `channel_sessions.json` 会话 → 调你的 LLM → `send.py` 发回 | ~1 分钟 | 按 channel 隔离，文件持久化 |
| **B. 平台 side chat**（如 Muse） | 定时任务把消息转给各 channel 的独立子对话，子对话里的 agent 回复 | 1~3 分钟 | 子对话天然隔离 |

用 A：把 `generate_reply()` 换成你家 agent 的调用，`nohup`/`systemd` 跑起来即可。脚本会从 `muse/` 根目录调用 `inbox_peek.py` / `send.py` / `channel_history.py`。

## 3. 文件清单与路径

```
Agents-Slack-Bridge/
└── muse/
    ├── README.md                 # 本文档
    ├── manifest.yaml             # Slack App 定义（从 manifest 建应用）
    ├── .env.example              # 凭据模板 → 复制为 .env（0600，不提交）
    ├── bridge.py                 # ★ 核心：Socket Mode 监听 → inbox.jsonl（只收不发）
    ├── send.py                   # 发消息：echo "正文" | python send.py <channel> [--thread-ts <ts>]
    ├── inbox_peek.py             # 打印未处理消息（不标记）
    ├── inbox_ack.py              # 按 ts 标记已处理（处理成功后调）
    ├── channel_history.py        # 拉频道最近 N 条（python channel_history.py <channel> [N]）
    ├── slack-bridge.service      # systemd unit（改路径后用，WorkingDirectory=.../muse）
    └── consumer/
        └── poll_consumer.py      # 消费层方案 A 参考实现（~30s 轮询）
```

运行时产生（不提交）：`inbox.jsonl`（队列）、`bridge.log`（日志）、`consumer/channel_sessions.json`（会话）。

所有脚本以 **`muse/` 为 cwd** 运行（`WorkingDirectory=.../muse`）。

## 4. 消息队列格式

`inbox.jsonl` 每行一条：
```json
{"channel":"C...","channel_name":"general","user":"U...","user_name":"AresX",
 "text":"@bot 你好","kind":"mention","ts":"...","thread_ts":"",
 "received_at":1234567890.0,"delivered":false}
```
`kind`: `dm`（私信）/`mention`（被@）。消费流程：`inbox_peek.py` 读 → 处理 → `inbox_ack.py <ts>` 确认（失败不确认，下轮重试）。

## 5. 踩坑清单（实测）

1. **"向此应用发送消息的功能已关闭"** → manifest 漏了 `messages_tab_enabled: true`，去 App Home 手动开。
2. **`/etc` 文件消失** → 见 §2.3 警告，正本放本目录 + 健康检查自愈。
3. **出站代理/TLS 拦截** → `.env` 配 `PROXY_URL` / `CA_BUNDLE`，三个脚本都会读。
4. **频道必须先邀请 bot**，否则收不到 `app_mention`。
5. **防自循环**：`bridge.py` 默认丢弃所有 bot 消息；要让指定 agent 能 @ 你，把它的 user ID / bot ID 填进 `ALLOWED_BOT_USERS` / `ALLOWED_BOT_IDS`（自己的消息永远过滤）。
6. **Socket Mode 先 ack 再处理**，否则 Slack 重发。
7. **中断期消息会丢**（Slack 不补发），健康检查把中断窗口压到分钟级。
8. **原帖回复**：`event.thread_ts` 有值时，`send.py` 带 `--thread-ts`。

## 6. 最小验证

1. `systemctl is-active` → active，日志出现 `socket mode connected`
2. Slack 私信 bot → `python inbox_peek.py` 看到这条 → `echo hi | python send.py <DM频道ID>` → Slack 收到
3. 拉 bot 进测试频道，`@bot hello` → 收到 mention → 消费层回复出现在频道

## 7. 安全

- token 只在 `.env`（0600），**永不**进聊天记录、日志、命令行参数、git
- 建议首次配置后去 Slack 后台轮换一次 token
- bot 只能读它加入的频道 + 自己的私信；不想让它看的地方别邀请它
