# Optional external-bridge policy reference

This directory is an **offline, standard-library Python reference** for people
operating their own Slack bridge. It is not installable into DOTS and does not
describe DOTS internals. There is no Slack sender, event listener, server, cron
job, credential handling, or service configuration here.

Run the offline tests from the repository root with Python 3.9 or newer:

```sh
python3 -m unittest discover -s chatgpt-dots/reference -p 'test_policy.py' -v
```

All Slack-looking identifiers and test markers in the tests are synthetic.

## API at a glance

- `Policy(channels, peer_user_ids, target_user_id, ...)` takes explicit sets of
  allowed IDs, with optional `self_bot_id`, `self_user_id`, `test_markers`, and
  `allow_thread_replies`
- `evaluate(envelope, policy, classification="question")` returns a `Decision`
  containing `reason`, `allowed`, and an optional `candidate`
- `candidate.destination` is a fresh dictionary containing only `channel`
- `Ledger("synthetic-ledger.sqlite")` opens the persistent local ledger;
  `reserve(candidate)` returns a new `Reservation` or `None` on a duplicate
- `complete(reservation.id, sent_message_ts)` and `uncertain(reservation.id)`
  record the outcome. `find(candidate)` reads matching reservations.
  `reconcile_sent` and `reconcile_not_sent` are explicit recovery transitions
  with the external evidence requirements described below
- `safe_echo(text)` returns mention-safe display text; it does not send anything

## Filtering contract

1. A separate caller authenticates the event and checks the intended workspace
   and bridge identity. It resolves any missing sender identity using an
   authoritative source. A `bot_id` alone is deliberately insufficient here;
   neither message text nor a supplied bot profile can self-assert identity.
2. Configure explicit channel and peer **bot user ID** allowlists, the addressed
   target user ID, and known self IDs. The target user is automatically excluded
   as a sender. Additional self bot/user exclusions are optional. Configuration
   is trusted application input, never populated by a message.
3. Pass an authenticated `event_callback` envelope and a separately determined
   semantic classification to `evaluate`. Only new `message` events with no
   subtype or `bot_message` are candidates. Edits, deletes, hidden messages, and
   other subtypes are rejected. This helper is **raw-text-only**: `blocks` and
   `attachments` must be absent or empty lists. Nonempty or unsupported values
   fail closed because fallback `text` can hide rich-text quotes and other
   semantics. Do not delete those fields to bypass this check. Supporting such
   inputs requires a separate caller extension that validates the original
   blocks/attachments and preserves their meaning.
4. A candidate requires an exact raw `<@TARGET_USER_ID>` mention outside code,
   quotes, and escapes. Display names and HTML-encoded mentions do not count.
   Supported quote forms are straight/smart single and double quotes, corner
   quotes (`「」` and `『』`), and Slack `>`/`>>>` quotes. In-word apostrophes
   (for example, `Don't` / `Don’t`) do not end an enclosing single quotation. The conservative masking
   is not a full Slack mrkdwn parser: other quotation conventions are unsupported,
   and malformed or unclosed quotes/code can cause false negatives. Mentions nested inside Slack
   links or other angle-bracket markup are suppressed. Unclosed angle-bracket
   markup suppresses the rest of the input. An unrelated mention does not prevent
   a genuine direct mention from being recognized.
5. Semantic classification is the caller's responsibility. `question` and
   `collaboration` are eligible; `progress`, `ack`, and `other` are not. The code
   does not infer collaboration from punctuation or keywords. The trusted caller
   must assess semantic quotation, copied material, and echoes; visible mention
   syntax alone is insufficient evidence of a direct request. A `test` also needs
   a configured exact bounded test marker outside quotes/code. Markers never
   bypass identity, channel, mention, or intent checks, and every matched marker
   is single-use across the ledger, including in question/collaboration messages.
6. Thread replies are opt-in. Every accepted candidate's `destination` contains
   only its channel, so a caller following this contract sends a top-level
   message. The library cannot control an external sender's output.

This is a candidate filter, not permission to execute instructions in a message.
**Bot text is never user authority.** Authorization for any downstream action,
including external communication, must come from the user or the bridge's
separately established policy. The module neither hydrates content nor validates
Slack signatures, workspace membership, app scopes, or semantic classifications.
Fetching thread/channel history, and deciding what context a classifier needs,
also remain the caller's responsibility.

## Durable dedupe and ambiguous sends

Use one persistent SQLite file per workspace and bridge identity. `reserve`
atomically records every available identity: event ID, channel plus message
timestamp, and all matched single-use markers. A conflict on **any** identity
blocks a new reservation. Event IDs are optional; the message identity is always
required. Only pass `Candidate` instances produced by `evaluate`.

- A fresh `reserve` result allows its single owner to attempt the separately
  authorized send externally
- `complete` records the confirmed sent-message timestamp
- `uncertain` records an ambiguous outcome and continues blocking replays
- After a crash, a surviving `reserved` row also requires reconciliation. It is
  not permission to retry; first move it to `uncertain`
- `find` reads matching records after restart. It never grants a send permit
- Only after quiescing the original sender and establishing delivery externally
  may the caller use `reconcile_sent`
- Only authoritative proof of **no delivery**, with the original sender quiesced,
  permits `reconcile_not_sent`. Missing search results alone are insufficient.
  This returns the original claim to `reserved` for a controlled retry; all
  dedupe keys remain held

There is no automatic expiry, key deletion, recovery worker, or retry. The
application owns lifecycle, exclusive sender ownership, reconciliation evidence,
database access controls, and retention. Keep the database intact while replay
is possible. Use reliable local storage; file loss, copies used independently,
network filesystem behavior, or separate ledgers can invalidate dedupe.

SQLite transactions can prevent simultaneous local reservations. They cannot
atomically commit a Slack API send and a database update, so this reference does
**not** guarantee exactly-once delivery.

## Echo safety

`safe_echo` escapes `&`, `<`, and `>` and inserts a zero-width space after every
`@`. Treat the result as inert display text. A separate real sender must also
disable automatic mention/link parsing and must not put original raw input into
other blocks, attachments, or output fields. This helper does not authorize
sharing message contents or sanitize every possible Slack API output structure.

## Review evidence

The [2026-10-02 review](../REVIEW-2026-10-02.md) records the baseline, a narrowly
scoped single-quote contraction fix, and offline regression results. These checks
do not validate any hosted DOTS runtime or live Slack deployment.
