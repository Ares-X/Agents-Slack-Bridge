# AGENT_WAKE.md — Grok Bot Slack reply (woken by webhook)

You were woken because the Slack bridge deploy has **claimable** inbox rows.
Do **not** use the template consumer path. Read current context, then choose a useful reply through the durable pipeline or a quiet resolution with a recorded reason.

Short entrypoint: [wakeup.md](./wakeup.md) · Peer rules: [PEER_STANDING_RULES.md](./PEER_STANDING_RULES.md)

Deploy root: use the private path verified during setup; set `GROK_BRIDGE_DIR` to that absolute directory in the execution environment. Do not assume another deployment's path or infer access from the webhook payload alone.

Repo path: `grokbot/`  
Use venv: `./venv/bin/python`

## Steps

1. **Peek claimable**
   ```bash
   cd "${GROK_BRIDGE_DIR:?Set the verified deployment directory}"
   ./venv/bin/python pending_notify.py    # refreshes pending.json
   ./venv/bin/python inbox_peek.py        # undelivered rows (JSONL)
   ./venv/bin/python pending_consume_once.py --list
   cat pending.json                       # claimable / waiting_rate_limit / blocked
   ```
   Read related pending rows **together by channel/thread**, including new work
   received while you were asleep. Identify the current authorized task and its
   later constraints before deciding what to send. Do not drain old rows as a
   series of independent mandatory replies. Do not discard a task just for age.
   Only send **claimable** (and **sent** = ack-only). Skip `sending` / `uncertain`
   (no blind resend). Honor `rate_limited` until `retry_after_until`.

2. **Channel history (required before reply)**
   ```bash
   ./venv/bin/python channel_history.py <channel_id> 15
   ./venv/bin/python channel_history.py <channel_id> 15 --thread-ts <ROOT_TS> --all
   ```
   `--thread` aliases `--thread-ts`. Bodies are complete; if a page has more
   messages, its pagination row gives `next_cursor`. Use `--all` or `--cursor`
   as needed to read the originating task, subsequent constraints and relevant
   discussion. Read the relevant thread, even if its parent belongs to a peer.
   If history fails, record the limitation; do not pretend you read context or
   turn missing context into a speculative reply/quiet resolution.

3. **Craft reply**
   - Reply when it advances the authorized task: an answer, evidence, a useful
     question or a concrete handoff. Notifications, pure acknowledgements,
     approval reminders and instructions superseded by the current task may be
     resolved quietly with a recorded reason. Do not echo old control messages.
   - Mention a peer with `<@USER_ID>` **only when requesting their concrete next
     action**. Plain names suffice for references and acknowledgements. Do not
     @ yourself or trigger peers through quoted mentions. Authorized discussion
     can continue across multiple substantive turns; there is no one-turn cap
     for an entire task and no obligation to reply to every inbound row.
   - Without a source thread, default for `kind=mention` / `dm`: channel top-level (`REPLY_IN_THREAD=0`). Existing source threads, including in-thread mentions, stay in that thread.
   - **`kind=thread_reply`** (user replied in a thread under *your* bot message, no @ required):
     **always reply in the same thread** (`thread_ts`). `pending_consume_once.py` does this
     automatically. Use the durable pipeline for replies; do not bypass its claim and receipt handling with `send.py`.
   - **At most one reply per (channel, ts).** Decide separately for each identity;
     one current useful answer may make older related control rows quiet. Do not
     apply that decision to unrelated channels, threads or questions.

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
   - Quiet resolution of one **unsent claimable** row:
     ```bash
     ./venv/bin/python pending_consume_once.py --channel C012 --ts 1.0 \
       --no-reply --reason 'Notification already incorporated into the current task'
     ```
     The original row stays in `inbox.jsonl`, with `reply_status=no_reply`,
     `resolution_reason` and `resolved_at`. This durable decision competes with
     send under the same lock; it never clears `sending`, `sent`, `uncertain` or
     `rate_limited` (even after the wait). Inspect the retained row to review a
     mistaken decision; do not blindly replay or delete it.
   - Or: `reply_pipeline.process_one` for sending. `inbox_ack.py` only completes
     rows already marked `sent`; it is not a shortcut to discard unsent work.
   - On rate limit: wait; do not hammer.

5. **Verify**
   ```bash
   ./venv/bin/python inbox_peek.py
   ./venv/bin/python pending_notify.py
   ```

## Notes

- Inbox `kind` may be `mention`, `dm`, or `thread_reply`. Thread replies wake you the same way as @mentions.
- After updating `manifest.yaml` (adds `message.channels` / `message.groups`), **re-apply the Slack App manifest** at https://api.slack.com/apps → App Manifest → Save → reinstall/update event subscriptions. Until then, thread replies will not reach the bridge.


- Secrets live in `.env` / `webhook.env` — never print or commit them.
- Consumer mode `agent_wake` only wakes you; it does not template-send.
- `REPLY_MODE=template` is opt-in only; missing mode without `webhook.env` refuses to start (messages kept).
- Identity = `channel` + `ts`.
- Wake webhook refuses HTTP redirects (auth headers must not leak). 429 Retry-After is honored by the consumer.
