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
# ONE message only — channel + ts required (never --text alone):
./venv/bin/python pending_consume_once.py \
  --channel <CHANNEL> --ts <TS> --text '<reply for this row only>'
```

- Do **not** use the template consumer path.
- Skip `sending` / `uncertain`; ack-only for `sent`; wait out `rate_limited`.
- Identity = `channel` + `ts`. Never broadcast one text to all claimable rows.
- Secrets in `.env` / `webhook.env` — never print or commit.

Deploy: `/workspace/slack-bridge-grokbot` · Repo: `grokbot/`
