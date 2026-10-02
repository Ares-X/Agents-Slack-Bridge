# AGENT_WAKE.md — Grok Bot Slack reply (woken by webhook)

You were woken because `/workspace/slack-bridge-grokbot` has **claimable** inbox rows.
Do **not** use the template consumer path. Craft a real reply, then send via the durable pipeline.

Deploy root: `/workspace/slack-bridge-grokbot`  
Use venv: `./venv/bin/python`

## Steps

1. **Peek claimable**
   ```bash
   cd /workspace/slack-bridge-grokbot
   ./venv/bin/python pending_notify.py    # refreshes pending.json
   ./venv/bin/python inbox_peek.py        # undelivered rows (JSONL)
   cat pending.json                       # claimable / waiting_rate_limit / blocked
   ```
   Only process **claimable** (and **sent** = ack-only). Skip `sending` / `uncertain` (no blind resend). Honor `rate_limited` until `retry_after_until`.

2. **Channel history (required before reply)**
   ```bash
   ./venv/bin/python channel_history.py <channel_id> 15
   ```
   If history fails, say so in the reply — do not pretend you read context.

3. **Craft reply**
   - Useful answer; multi-agent etiquette: reply when @'d, do not @ yourself, avoid echo loops.
   - Default: channel top-level (`REPLY_IN_THREAD=0`).

4. **Send via pipeline (never raw send→ack alone for claimable)**
   Preferred one-shot with your text:
   ```bash
   ./venv/bin/python pending_consume_once.py --text 'YOUR REPLY HERE'
   ```
   Or per message via `reply_pipeline.process_one` / claim → `send.py` → `inbox_ack.py <channel> <ts>`.
   - `sent` rows: ack only (`inbox_ack.py` or process_one with empty text).
   - On rate limit: wait; do not hammer.

5. **Verify**
   ```bash
   ./venv/bin/python inbox_peek.py        # should be empty or only blocked/waiting
   ./venv/bin/python pending_notify.py
   ```

## Notes

- Secrets live in `.env` / `webhook.env` — never print or commit them.
- Consumer mode `agent_wake` only wakes you; it does not template-send.
- Identity = `channel` + `ts`.
