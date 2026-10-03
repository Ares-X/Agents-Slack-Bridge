# wakeup.md — Grok Bot Slack wake entrypoint

You were woken by the Slack bridge webhook (`source=slack-bridge`).
**Read and follow [AGENT_WAKE.md](./AGENT_WAKE.md)** for the full pipeline.
**Honor [@-peer standing rules](./PEER_STANDING_RULES.md)** on every reply.

## Must-do (short)

```bash
cd /workspace/slack-bridge-grokbot
./venv/bin/python pending_notify.py
./venv/bin/python pending_consume_once.py --list
./venv/bin/python channel_history.py <channel_id> 15
# Read the relevant thread completely; use --all / --cursor for older context:
./venv/bin/python channel_history.py <channel_id> 15 --thread-ts <ROOT_TS> --all
# Decide from related pending rows + latest authorized task, then target ONE row:
./venv/bin/python pending_consume_once.py \
  --channel <CHANNEL> --ts <TS> --text '<reply for this row only>'
# A notification, acknowledgement or superseded instruction may need no message:
./venv/bin/python pending_consume_once.py \
  --channel <CHANNEL> --ts <TS> --no-reply --reason '<context-based decision>'
```

- Do **not** use the template consumer path.
- Skip `sending` / `uncertain`; ack-only for `sent`; wait out `rate_limited`.
- Identity = `channel` + `ts`. Never broadcast one text to all claimable rows.
- Read related pending rows together by channel/thread and follow the latest
  authorized task and constraints. Each row permits at most one reply, not a
  mandatory reply. Mention peers only to request a concrete next action; continue
  substantive authorized collaboration across turns when useful.
- Quiet resolution retains the original event, reason and time. It cannot consume
  `sending`, `sent`, `uncertain` or `rate_limited`; ACK is only for confirmed `sent`.
- Secrets in `.env` / `webhook.env` — never print or commit.

Deploy: `/workspace/slack-bridge-grokbot` · Repo: `grokbot/`
