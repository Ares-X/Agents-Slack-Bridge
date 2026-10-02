"""Optional, offline reference policy for a separately operated Slack bridge.

This is neither a DOTS extension nor DOTS' implementation. There is no receiver,
sender, network client, scheduler, authentication, or authorization mechanism.
The caller must authenticate events, resolve identities/hydrate data externally,
and supply a trustworthy semantic classification. Bot text is never user authority.
Syntax checks below deliberately prefer false negatives over quoted/code triggers.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import html
import re
import sqlite3
from typing import Optional
import uuid


_USER = re.compile(r"[UW][A-Z0-9]{4,}\Z", re.ASCII)
_CHANNEL = re.compile(r"[CG][A-Z0-9]{4,}\Z", re.ASCII)
_BOT = re.compile(r"B[A-Z0-9]{4,}\Z", re.ASCII)
_TS = re.compile(r"[0-9]{1,20}\.[0-9]{6}\Z", re.ASCII)
_EVENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z", re.ASCII)
_MARKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}\Z", re.ASCII)
_KINDS = frozenset({"question", "collaboration", "test"})


def _valid(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(frozen=True)
class Policy:
    """Explicit allowlists; all example IDs in the tests are synthetic.

    target_user_id is the required mention and is automatically excluded as a
    sender. self_user_id can exclude another known self identity, if applicable.
    A peer's bot_id is NOT a replacement for its resolved Slack user ID.
    """

    channels: frozenset[str]
    peer_user_ids: frozenset[str]
    target_user_id: str
    self_bot_id: Optional[str] = None
    self_user_id: Optional[str] = None
    allow_thread_replies: bool = False
    test_markers: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        for name, pattern in (("channels", _CHANNEL), ("peer_user_ids", _USER),
                              ("test_markers", _MARKER)):
            values = getattr(self, name)
            if not isinstance(values, (set, frozenset)):
                raise ValueError(f"{name} must be an explicit set")
            if any(not _valid(pattern, value) for value in values):
                raise ValueError(f"invalid {name}")
            object.__setattr__(self, name, frozenset(values))
        if not self.channels or not self.peer_user_ids:
            raise ValueError("nonempty channel and peer allowlists are required")
        for name, pattern in (("target_user_id", _USER), ("self_user_id", _USER),
                              ("self_bot_id", _BOT)):
            value = getattr(self, name)
            if (name == "target_user_id" or value is not None) and not _valid(pattern, value):
                raise ValueError(f"invalid {name}")
        if type(self.allow_thread_replies) is not bool:
            raise ValueError("allow_thread_replies must be a boolean")


def _visible_text(text):
    """Mask code, quotes, and escapes without decoding HTML or Slack markup.

    This is intentionally NOT a complete mrkdwn parser. Supported quote forms
    are straight/smart single and double quotes, Japanese/Chinese corner quotes,
    Slack line quotes (>), and multi-line quotes (>>>). Unclosed code/quotes
    suppress the remainder. Apostrophes inside words are treated as contractions.
    Semantic quotation, copied material, and echoes remain the caller's concern.
    """
    characters = list(text)

    def mask(start, end):
        for position in range(start, end):
            if characters[position] not in "\r\n":
                characters[position] = " "

    offset = 0
    quote_rest = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" \t")
        quote_rest = quote_rest or stripped.startswith(">>>")
        if quote_rest or stripped.startswith(">"):
            mask(offset, offset + len(line))
        offset += len(line)
    source = "".join(characters)
    position = 0
    while position < len(source):
        char = source[position]
        if char == "\\":
            end = position + 1
            while end < len(source) and source[end] == "\\":
                end += 1
            mask(position, min(end + 1, len(source)))
            position = end + 1
            continue
        if char == "`":
            end = position + 1
            while end < len(source) and source[end] == "`":
                end += 1
            delimiter = source[position:end]
            closing = source.find(delimiter, end)
            end = len(source) if closing < 0 else closing + len(delimiter)
            mask(position, end)
            position = end
            continue
        closing_quote = {"\"": "\"", "'": "'", "“": "”", "‘": "’",
                         "「": "」", "『": "』"}.get(char)
        contraction = (char == "'" and position > 0 and
                       position + 1 < len(source) and source[position - 1].isalnum()
                       and source[position + 1].isalnum())
        if closing_quote and not contraction:
            closing = position + 1
            while closing < len(source):
                if source[closing] == "\\":
                    closing += 2
                elif source[closing] == closing_quote:
                    break
                else:
                    closing += 1
            end = min(closing + 1, len(source))
            mask(position, end)
            position = end
            continue
        if char == "<":
            # Preserve only a standalone raw user-mention token. In particular,
            # never count a mention nested in a Slack link label or other markup.
            end, depth = position + 1, 1
            while end < len(source) and depth:
                if source[end] == "<":
                    depth += 1
                elif source[end] == ">":
                    depth -= 1
                end += 1
            token = source[position:end]
            if depth or not re.fullmatch(r"<@[UW][A-Z0-9]{4,}>", token, re.ASCII):
                mask(position, end)
            position = end
            continue
        position += 1
    return "".join(characters)


@dataclass(frozen=True)
class Candidate:
    channel: str
    message_ts: str
    sender_user_id: str
    event_id: Optional[str]
    input_thread_ts: Optional[str]
    test_markers: tuple[str, ...]

    @property
    def destination(self):
        """A top-level channel destination, even for an accepted thread reply."""
        return {"channel": self.channel}  # Deliberately no thread_ts.

    @property
    def dedupe_keys(self):
        keys = [f"message:{self.channel}:{self.message_ts}"]
        if self.event_id is not None:
            keys.append(f"event:{self.event_id}")
        keys.extend(f"test:{marker}" for marker in self.test_markers)
        return tuple(keys)


@dataclass(frozen=True)
class Decision:
    reason: str
    candidate: Optional[Candidate] = None

    @property
    def allowed(self):
        return self.candidate is not None


def evaluate(envelope, policy: Policy, *, classification: str) -> Decision:
    """Filter an authenticated event_callback envelope, without reserving it.

    classification is supplied by trusted caller logic, NEVER taken from the
    message or envelope. It must describe semantic intent, not punctuation or a
    substring heuristic. It must also assess semantic quotation, copied material,
    and echoes. question/collaboration/test are the only eligible kinds. A test
    additionally needs a configured exact marker outside code/quotes. This helper
    admits raw-text-only input: nonempty/unsupported blocks or attachments fail
    closed because fallback text can misrepresent their semantics. Never strip
    those fields merely to pass this check.
    """
    if not isinstance(envelope, dict) or envelope.get("type") != "event_callback":
        return Decision("invalid_envelope")
    event = envelope.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return Decision("not_message")
    if event.get("subtype") not in (None, "bot_message"):
        return Decision("not_new_message")
    if any(key in event for key in ("edited", "deleted_ts", "previous_message", "message")):
        return Decision("not_new_message")
    if event.get("hidden", False) is not False:
        return Decision("hidden_message")
    for field_name in ("blocks", "attachments"):
        if field_name in event and (not isinstance(event[field_name], list) or event[field_name]):
            return Decision("unsupported_rich_content")
    channel, user, bot_id = event.get("channel"), event.get("user"), event.get("bot_id")
    if not _valid(_CHANNEL, channel) or channel not in policy.channels:
        return Decision("channel_not_allowed")
    if not _valid(_USER, user):
        return Decision("unresolved_user")
    if bot_id is not None and not _valid(_BOT, bot_id):
        return Decision("invalid_bot_id")
    if user in (policy.target_user_id, policy.self_user_id) or (
            policy.self_bot_id is not None and bot_id == policy.self_bot_id):
        return Decision("self_message")
    if user not in policy.peer_user_ids:
        return Decision("peer_not_allowed")
    message_ts, event_id = event.get("ts"), envelope.get("event_id")
    thread_ts = event.get("thread_ts")
    if not _valid(_TS, message_ts) or (event_id is not None and not _valid(_EVENT_ID, event_id)):
        return Decision("invalid_message_identity")
    if thread_ts is not None:
        if not _valid(_TS, thread_ts):
            return Decision("invalid_thread")
        if thread_ts != message_ts and not policy.allow_thread_replies:
            return Decision("thread_reply_disabled")
    text = event.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 40000:
        return Decision("invalid_text")
    visible = _visible_text(text)
    if f"<@{policy.target_user_id}>" not in visible:
        return Decision("missing_direct_mention")
    if not isinstance(classification, str) or classification not in _KINDS:
        return Decision("intent_not_eligible")
    markers = tuple(sorted(marker for marker in policy.test_markers if
                           re.search(r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])", visible)))
    if classification == "test" and not markers:
        return Decision("missing_test_marker")
    return Decision("candidate", Candidate(channel, message_ts, user, event_id, thread_ts, markers))


def safe_echo(text: str) -> str:
    """Return inert display text: escape markup and break every @ with U+200B.

    Defense in depth only. A real sender must disable automatic mention/link
    parsing and keep raw input out of blocks/attachments/other output fields.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return html.escape(text, quote=False).replace("@", "@\u200b")


@dataclass(frozen=True)
class Reservation:
    id: str
    state: str
    sent_message_ts: Optional[str]


class Ledger:
    """Durable local dedupe, NOT an exactly-once Slack-send guarantee.

    Use one persistent file per workspace/bridge identity. BEGIN IMMEDIATE and
    unique keys atomically claim ALL event/message/test identities. Any matching
    identity blocks reserve(), including completed or uncertain reservations.

    reserve -> attempt send externally -> complete, or uncertain on ambiguity.
    A crash can leave 'reserved'; treat that as uncertain, not permission to retry.
    Quiesce the original sender and reconcile against authoritative delivery data
    before reconcile_sent() or reconcile_not_sent(). No expiry or automatic retry.
    Storage retention, sender ownership, reconciliation, and authorization belong
    to the calling application. Do not delete keys while replay remains possible.
    """

    def __init__(self, path):
        self.path = str(path)
        if not self.path or self.path == ":memory:":
            raise ValueError("a persistent SQLite file is required")
        with self._transaction() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY, state TEXT NOT NULL
                CHECK (state IN ('reserved', 'complete', 'uncertain')),
                sent_message_ts TEXT)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS dedupe_keys (
                key TEXT PRIMARY KEY, reservation_id TEXT NOT NULL
                REFERENCES reservations(id))""")

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(self, candidate: Candidate) -> Optional[Reservation]:
        """Return a new claim, or None if ANY identity is already recorded.

        Only pass candidates returned by evaluate(). Caller must not send unless
        this call returns a new claim. A lookup result is not a new send permit.
        """
        with self._transaction() as connection:
            for key in candidate.dedupe_keys:
                if connection.execute("SELECT 1 FROM dedupe_keys WHERE key = ?", (key,)).fetchone():
                    return None
            identifier = uuid.uuid4().hex
            connection.execute("INSERT INTO reservations VALUES (?, 'reserved', NULL)", (identifier,))
            connection.executemany("INSERT INTO dedupe_keys VALUES (?, ?)",
                                   [(key, identifier) for key in candidate.dedupe_keys])
            return Reservation(identifier, "reserved", None)

    def find(self, candidate: Candidate) -> tuple[Reservation, ...]:
        """Read matching claims for reconciliation; this never grants a retry."""
        with self._transaction() as connection:
            matches = {}
            for key in candidate.dedupe_keys:
                row = connection.execute("""SELECT r.id, r.state, r.sent_message_ts
                    FROM reservations r JOIN dedupe_keys d ON r.id = d.reservation_id
                    WHERE d.key = ?""", (key,)).fetchone()
                if row:
                    matches[row[0]] = Reservation(*row)
            return tuple(matches[key] for key in sorted(matches))

    def _transition(self, identifier, source, target, sent_message_ts=None):
        if target == "complete" and not _valid(_TS, sent_message_ts):
            raise ValueError("completion requires the confirmed sent-message timestamp")
        with self._transaction() as connection:
            changed = connection.execute("""UPDATE reservations SET state = ?, sent_message_ts = ?
                WHERE id = ? AND state = ?""", (target, sent_message_ts, identifier, source)).rowcount
            if changed != 1:
                raise ValueError("unknown reservation or invalid state transition")
        return Reservation(identifier, target, sent_message_ts)

    def complete(self, identifier, sent_message_ts):
        return self._transition(identifier, "reserved", "complete", sent_message_ts)

    def uncertain(self, identifier):
        return self._transition(identifier, "reserved", "uncertain")

    def reconcile_sent(self, identifier, sent_message_ts):
        """Caller has established delivery externally after an ambiguous send."""
        return self._transition(identifier, "uncertain", "complete", sent_message_ts)

    def reconcile_not_sent(self, identifier):
        """Caller proved no delivery and quiesced the sender; permit its retry.

        Missing search results alone are NOT proof. Keeps every dedupe key so a
        concurrent/replayed input still cannot get a new reservation.
        """
        return self._transition(identifier, "uncertain", "reserved")
