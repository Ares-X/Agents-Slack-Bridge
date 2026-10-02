# AGENT_WAKE.md — Grok Bot Slack reply (woken by webhook)

You were woken because the Slack bridge deploy has **claimable** inbox rows.
Do **not** use the template consumer path. Craft a real reply, then send via the durable pipeline.

Short entrypoint: [wakeup.md](./wakeup.md) · Peer rules: [PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md)

Deploy root (live box): `/workspace/slack-bridge-grokbot`  
Repo path: `grokbot/`  
Use venv: `./venv/bin/python`

## Steps

1. **Peek claimable**
   ```bash
   cd /workspace/slack-bridge-grokbot
   ./venv/bin/python pending_notify.py    # refreshes pending.json
   ./venv/bin/python inbox_peek.py        # undelivered rows (JSONL)
   ./venv/bin/python pending_consume_once.py --list
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
   - **One reply per (channel, ts).** Different channels / questions need different texts.

4. **Send ONE message via pipeline (channel + ts required)**

   **Do not** run `pending_consume_once.py --text '...'` alone — that used to
   broadcast the same body to every actionable row (wrong channel + mass ACK).
   Always target exactly one identity:

   ```bash
   # Runnable example (replace channel / ts / text from peek/list):
   ./venv/bin/python pending_consume_once.py \
     --channel C0123456789 \
     --ts 1790948970.803249 \
     --text 'Here is the answer for this thread only.'

   # Positional form is equivalent:
   ./venv/bin/python pending_consume_once.py C0123456789 1790948970.803249 \
     --text 'Here is the answer for this thread only.'
   ```

   - `sent` rows: ack only — same CLI without `--text` (or empty text):
     ```bash
     ./venv/bin/python pending_consume_once.py --channel C012 --ts 1.0
     ```
   - Or: `reply_pipeline.process_one` / claim → `send.py` → `inbox_ack.py <channel> <ts>`.
   - On rate limit: wait; do not hammer.

5. **Verify**
   ```bash
   ./venv/bin/python inbox_peek.py
   ./venv/bin/python pending_notify.py
   ```

## Notes

- Secrets live in `.env` / `webhook.env` — never print or commit them.
- Consumer mode `agent_wake` only wakes you; it does not template-send.
- `REPLY_MODE=template` is opt-in only; missing mode without `webhook.env` refuses to start (messages kept).
- Identity = `channel` + `ts`.
- Wake webhook refuses HTTP redirects (auth headers must not leak). 429 Retry-After is honored by the consumer.
