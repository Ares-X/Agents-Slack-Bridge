"""消费层参考实现 A：轮询直回（最简可用）。

每 N 秒 peek 未处理消息 → 按 Slack channel 维护独立会话历史
→ 调用你的 LLM 生成回复 → send.py 发回 → ack。

把 generate_reply() 换成你家 agent 的调用即可。
会话历史落在 channel_sessions.json，重启不丢。

脚本 cwd：本文件在 consumer/ 下，inbox_peek / send / channel_history
都在上一层（muse/），因此 ROOT = dirname(BASE)，所有子进程在 ROOT 跑。
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)  # muse/
SESSIONS_PATH = os.path.join(BASE, "channel_sessions.json")
POLL_INTERVAL = 30  # 秒


def sh(*args, input_text=None):
    return subprocess.run(args, input=input_text, capture_output=True,
                          text=True, cwd=ROOT)


def peek():
    r = sh(sys.executable, "inbox_peek.py")
    out = []
    for line in r.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def load_sessions():
    if os.path.exists(SESSIONS_PATH):
        with open(SESSIONS_PATH) as f:
            return json.load(f)
    return {}


def save_sessions(s):
    tmp = SESSIONS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, SESSIONS_PATH)


def channel_history(channel, limit=15):
    """回复前拉历史做上下文（失败返回空，不阻塞）。"""
    r = sh(sys.executable, "channel_history.py", channel, str(limit))
    out = []
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
            if "error" not in m:
                out.append(m)
        except Exception:
            continue
    return out


def generate_reply(channel, message, session, history):
    """★ 换成你家 agent 的调用。输入：本条消息、该 channel 会话历史、频道近况。"""
    # 示例：最简 echo（生产环境请替换）
    return f"收到：{message['text'][:200]}"


def main():
    print(f"consumer polling every {POLL_INTERVAL}s (ROOT={ROOT}) ...")
    while True:
        try:
            msgs = peek()
            if msgs:
                sessions = load_sessions()
                for m in msgs:
                    ch = m["channel"]
                    sess = sessions.setdefault(ch, [])
                    hist = channel_history(ch)
                    try:
                        reply = generate_reply(ch, m, sess, hist)
                        cmd = [sys.executable, "send.py", ch]
                        if m.get("thread_ts"):
                            cmd += ["--thread-ts", m["thread_ts"]]
                        r = sh(*cmd, input_text=reply)
                        if "sent ok: True" in r.stdout:
                            sess.append({"role": "user", "text": m["text"]})
                            sess.append({"role": "assistant", "text": reply})
                            sessions[ch] = sess[-40:]  # 只保留最近 40 轮
                            sh(sys.executable, "inbox_ack.py", m["ts"])
                            print(f"replied in {m.get('channel_name', ch)}")
                        else:
                            print(f"send failed for {m['ts']}: {r.stdout} {r.stderr}",
                                  file=sys.stderr)
                    except Exception as e:
                        print(f"error handling {m.get('ts')}: {e}", file=sys.stderr)
                save_sessions(sessions)
        except Exception as e:
            print(f"poll error: {e}", file=sys.stderr)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
