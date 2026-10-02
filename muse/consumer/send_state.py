"""Durable send-claim state for the consumer (anti-duplicate-send).

Why this exists
--------------
A reply may be accepted by Slack while our process dies before it can ack
the inbox message (or before the state file is written).  The next round
would then send the text AGAIN.  This module makes every send attempt a
durable, mutually-excluded state transition:

  claim (status="sending", fsynced BEFORE the subprocess is spawned)
    -> "unacked"    (sent ok, inbox ack failed: retry ack only, never resend)
    -> "uncertain"  (ambiguous: verify via history, never blind-resend)
    -> resolved     (entry removed; the inbox tombstone is the done marker)

Crash/restart rule: on load, any "sending" entry becomes "uncertain" --
NEVER back to sendable.  A restart must not restore a message to
directly-sendable.

Claim rule (stale snapshots): the claim decision also consults the durable
inbox ack tombstone, inside the same (send_state EX -> inbox SH) critical
section as the claim write.  inbox_store.ack() takes the inbox EX lock, so
the check is atomic with respect to any concurrent ack.  Checking only the
claim entry is NOT enough: a consumer working from a stale snapshot could
claim after another consumer already sent, acked and resolved (deleted) its
entry -- the tombstone is the only durable proof the message is done.
Lock order is ALWAYS send_state -> inbox; nothing takes them in reverse
order, so no deadlock is possible.

Storage layout
--------------
``send_state.json`` (atomic write: tmp + fsync + replace + dir fsync, with
a ``.bak`` rotation of the previous good version).  All mutations serialize
on ``send_state.json.lock`` via ``fcntl.flock`` (EX for writes, SH for
reads), so two consumer processes can never interleave a read-modify-write.

Corruption rule: the file is NEVER silently treated as empty.
Load order is primary -> ``.tmp`` (leftover of a crashed write, itself
fsynced so it is a complete NEWER version -- recovering from it is safe)
-> ``.bak``.  Every candidate is parsed, v1-migrated and STRICTLY validated
(version + structure); anything else is treated as unusable, never as
usable state.

``.bak`` is special: it is the version from BEFORE the last save, so it
can be missing claims that the lost primary already had.  If primary and
``.tmp`` are both unusable and only ``.bak`` parses, the restore is
potentially STALE -- a claim that existed in primary (possibly already
sent) would look fresh again and could be resent.  That cannot be proven
safe, so the store quarantines the evidence and raises instead of coming
up (fail-closed) until an operator restores a known-good copy.

If nothing usable remains, all candidates are quarantined to
``send_state.json.corrupt.<millis>.<primary|tmp|bak>`` (each source keeps
its own suffix so no evidence overwrites another) and
``StateCorruptError`` is raised.  The consumer then refuses to send
anything (fail-closed) until an operator restores a good copy.

v1 migration: files written by the old in-memory-dict consumer
(``{"sent_unacked": [...], "uncertain": {...}}``) are converted once, on
load, to v2.  Old uncertain entries have no text hash, so they can never be
positively verified -- they stay uncertain until manual review (safe side).
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager

SCHEMA_VERSION = 2

STATUS_SENDING = "sending"
STATUS_UNACKED = "unacked"
STATUS_UNCERTAIN = "uncertain"

# claim() outcomes
CLAIM_CLAIMED = "claimed"      # this caller now owns the send right
CLAIM_HELD = "held"            # another live consumer holds the claim
CLAIM_COMPLETED = "completed"  # durable inbox tombstone exists: done


class StateCorruptError(Exception):
    """send_state.json and its backups are unusable.

    The damaged files were quarantined as
    ``send_state.json.corrupt.<millis>.<primary|tmp|bak>`` (evidence
    preserved, each source keeps its own suffix) and a
    ``send_state.json.quarantined`` marker was left behind so that even
    after a restart the store refuses to come up empty.  The consumer must
    NOT send while this holds: without state, every undelivered inbox
    message looks fresh and would be resent.
    Recovery: inspect the quarantined copies; restore a copy you can prove
    safe (i.e. one that cannot predate a persisted send claim) to
    ``send_state.json``, delete the ``.quarantined`` marker, restart.
    """


@contextmanager
def _locked(lock_path, exclusive):
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _parse_file(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


class SendState:
    def __init__(self, path, inbox_path=None, inbox_lock_path=None):
        self.path = path
        self.lock_path = path + ".lock"
        self.tmp_path = path + ".tmp"
        self.bak_path = path + ".bak"
        self.quarantined_path = path + ".quarantined"
        # Durable completion marker consulted by claim().  Kept as plain
        # paths (no import of inbox_store: consumer/ must not depend on
        # muse/ being importable) -- poll_consumer wires the real paths.
        self.inbox_path = inbox_path
        self.inbox_lock_path = inbox_lock_path
        with _locked(self.lock_path, True):
            data = self._load_locked()
            changed = False
            # Restart rule: in-flight sends become uncertain, never sendable.
            for mid, e in data["sends"].items():
                if isinstance(e, dict) and e.get("status") == STATUS_SENDING:
                    e["status"] = STATUS_UNCERTAIN
                    e["attempts"] = max(1, int(e.get("attempts") or 0))
                    e["note"] = ("recovered after restart: send may have "
                                 "succeeded; verify before any action")
                    e["updated_at"] = time.time()
                    changed = True
            if changed:
                self._save_locked(data)

    # ---- persistence internals (caller holds the lock) ----

    @staticmethod
    def _valid_data(data):
        """Strict structure check.  A file that parses as JSON but does not
        match the v2 schema is corrupt, never usable state."""
        if not isinstance(data, dict):
            return False
        if data.get("v") != SCHEMA_VERSION:
            return False
        sends = data.get("sends")
        if not isinstance(sends, dict):
            return False
        for e in sends.values():
            if not isinstance(e, dict):
                return False
            if e.get("status") not in (STATUS_SENDING, STATUS_UNACKED,
                                       STATUS_UNCERTAIN):
                return False
        if not isinstance(data.get("hist_deferred", {}), dict):
            return False
        return True

    def _load_locked(self):
        """Load, trying primary -> tmp -> bak.  Never silently empty.

        ``.tmp`` is the fsynced leftover of a crashed write, i.e. a
        complete NEWER version: recovering from it is safe.  ``.bak`` is
        the version from BEFORE the last save: if it is the ONLY usable
        candidate, the restore may be missing claims the lost primary
        already had (possibly already sent) -- that cannot be proven
        safe, so quarantine + raise instead of coming up stale.
        """
        paths = (self.path, self.tmp_path, self.bak_path)
        if not any(os.path.exists(p) for p in paths):
            if os.path.exists(self.quarantined_path):
                # 之前已隔离过：绝不静默变空，等人工恢复。
                raise StateCorruptError(
                    "send_state was quarantined earlier "
                    "(see send_state.json.corrupt.*); restore a good copy "
                    "to send_state.json and remove .quarantined")
            return {"v": SCHEMA_VERSION, "sends": {}, "hist_deferred": {}}
        good = {}
        for p in paths:
            if os.path.exists(p):
                data = _parse_file(p)
                if isinstance(data, dict):
                    data = self._migrate_v1(data)  # idempotent
                    if self._valid_data(data):
                        good[p] = data
        if self.path in good or self.tmp_path in good:
            src = self.path if self.path in good else self.tmp_path
            if src != self.path:
                print(f"send_state: recovered from {src}", flush=True)
            return good[src]
        if self.bak_path in good:
            # STALE backup: primary and .tmp are both unusable, so this
            # .bak may predate persisted send claims.  Restoring it would
            # make those messages look fresh and directly sendable.
            self._quarantine_locked()
            raise StateCorruptError(
                "send_state.json and .tmp are unusable; only the older "
                ".bak parses, which may predate persisted send claims.  "
                "Restoring it could resend already-sent messages.  "
                "Evidence quarantined as send_state.json.corrupt.* -- "
                "restore a copy you can prove safe to send_state.json "
                "and remove .quarantined")
        self._quarantine_locked()
        raise StateCorruptError(
            "send_state.json, .tmp and .bak are all unparseable or "
            "structurally invalid; quarantined as "
            "send_state.json.corrupt.*")

    def _save_locked(self, data):
        tmp = self.tmp_path
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(self.path):
            # Previous good version becomes the backup (atomic rename).
            os.replace(self.path, self.bak_path)
        os.replace(tmp, self.path)
        dirfd = os.open(os.path.dirname(os.path.abspath(self.path)),
                        os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def _quarantine_locked(self):
        stamp = int(time.time() * 1000)
        # Each source gets its own suffix: one quarantine pass must never
        # overwrite another source's evidence.
        for p, tag in ((self.path, "primary"), (self.tmp_path, "tmp"),
                       (self.bak_path, "bak")):
            if os.path.exists(p):
                try:
                    os.replace(p, "%s.corrupt.%d.%s" % (self.path, stamp,
                                                        tag))
                except OSError:
                    pass
        # 隔离标记：重启后也不许静默变空（fail-closed）。
        try:
            with open(self.quarantined_path, "w") as f:
                f.write(json.dumps(
                    {"at": time.time(),
                     "reason": "primary/.tmp/.bak unusable; evidence in "
                               "send_state.json.corrupt.*"},
                    ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    # ---- v1 migration ----

    @staticmethod
    def _is_v1(data):
        # 精确判定：真正的 v1 文件没有 "v" 键（旧 consumer 只写
        # sent_unacked/uncertain）。带未知版本号（v=99 等未来版本）
        # 的文件绝不能被"迁移"成空状态——那会静默丢失数据。
        return (isinstance(data, dict) and "v" not in data
                and ("sent_unacked" in data or "uncertain" in data))

    @staticmethod
    def _migrate_v1(data):
        """Old shape -> v2.  Idempotent: v2 input is returned unchanged."""
        if not SendState._is_v1(data):
            return data
        now = time.time()
        sends = {}
        for mid in data.get("sent_unacked", []) or []:
            sends[mid] = {"status": STATUS_UNACKED, "attempts": 0,
                          "updated_at": now,
                          "note": "migrated from v1 sent_unacked"}
        for mid, u in (data.get("uncertain", {}) or {}).items():
            # v1 stored only text[:200]: no hash available, so these entries
            # can never be positively verified -> stay uncertain.
            sends[mid] = {"status": STATUS_UNCERTAIN,
                          "attempts": int((u or {}).get("attempts", 1) or 1),
                          "text_hash": None,
                          "updated_at": now,
                          "note": "migrated from v1 uncertain; "
                                  "no text hash, cannot auto-verify"}
        return {"v": SCHEMA_VERSION, "sends": sends,
                "hist_deferred": data.get("hist_deferred", {}) or {}}

    # ---- durable completion marker (caller holds send_state EX) ----

    def _inbox_tombstone_locked(self, msg_id):
        """True if the inbox already holds an ack tombstone for msg_id.

        Takes the inbox SH lock; inbox_store.ack() takes the inbox EX
        lock, so no ack can slip between this check and the claim write.
        """
        if not self.inbox_path:
            return False
        lock_path = self.inbox_lock_path or (self.inbox_path + ".lock")
        with _locked(lock_path, False):
            try:
                f = open(self.inbox_path, "r")
            except FileNotFoundError:
                return False
            except OSError:
                return False
            with f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(r, dict) and r.get("type") == "ack" \
                            and r.get("msg_id") == msg_id:
                        return True
        return False

    # ---- public API (each call is a locked, durable transition) ----

    def ensure_usable(self):
        """Re-validate the state file.  Raises StateCorruptError while the
        store is unusable; the consumer must not send in that case."""
        with _locked(self.lock_path, False):
            self._load_locked()

    def _mutate(self, fn):
        with _locked(self.lock_path, True):
            data = self._load_locked()
            result = fn(data["sends"])
            self._save_locked(data)
            return result

    def get(self, msg_id):
        with _locked(self.lock_path, False):
            data = self._load_locked()
            e = data["sends"].get(msg_id)
            return dict(e) if isinstance(e, dict) else None

    def claim(self, msg_id, channel, thread_ts, text_hash,
              client_msg_id=None):
        """Durably record a send attempt BEFORE spawning send.py.

        Returns ``(entry, outcome)``; outcome is ``"claimed"`` (this
        caller now owns the send right), ``"held"`` (another live
        consumer already holds the claim: do NOT send, only verify), or
        ``"completed"`` (the durable inbox tombstone already exists:
        another consumer sent and acked this message: do NOT send).

        The tombstone check runs inside the same critical section as the
        claim write, so a consumer working from a stale snapshot can
        never claim a message that another consumer already finished --
        even after that consumer resolved (deleted) its claim entry.
        Locking only the claim creation is NOT enough; the durable
        completion marker must participate in the claim decision.
        """
        now = time.time()
        with _locked(self.lock_path, True):
            data = self._load_locked()
            if self._inbox_tombstone_locked(msg_id):
                return (None, CLAIM_COMPLETED)
            sends = data["sends"]
            e = sends.get(msg_id)
            if isinstance(e, dict):
                return (dict(e), CLAIM_HELD)
            e = {"status": STATUS_SENDING, "channel": channel,
                 "thread_ts": thread_ts or "", "text_hash": text_hash,
                 "client_msg_id": client_msg_id,
                 "attempts": 0, "claimed_at": now, "updated_at": now,
                 "owner_pid": os.getpid()}
            try:
                import socket
                e["owner_host"] = socket.gethostname()
            except Exception:
                pass
            sends[msg_id] = e
            self._save_locked(data)
            return (dict(e), CLAIM_CLAIMED)

    def set_unacked(self, msg_id):
        """Sent ok, inbox ack failed: only the ack may be retried."""

        def _do(sends):
            e = sends.get(msg_id)
            if isinstance(e, dict):
                e["status"] = STATUS_UNACKED
                e["updated_at"] = time.time()

        self._mutate(_do)

    def set_uncertain(self, msg_id, attempts, text_hash=None):
        """Ambiguous result: hold for history verification, never resend."""

        def _do(sends):
            e = sends.get(msg_id)
            if isinstance(e, dict):
                e["status"] = STATUS_UNCERTAIN
                e["attempts"] = attempts
                if text_hash is not None:
                    e["text_hash"] = text_hash
                e["updated_at"] = time.time()

        self._mutate(_do)

    def resolve(self, msg_id):
        """Send fully processed (acked): drop the entry."""

        def _do(sends):
            sends.pop(msg_id, None)

        self._mutate(_do)

    def get_hist_deferred(self, msg_id):
        with _locked(self.lock_path, False):
            data = self._load_locked()
            return int(data["hist_deferred"].get(msg_id, 0) or 0)

    def set_hist_deferred(self, msg_id, n):
        with _locked(self.lock_path, True):
            data = self._load_locked()
            if n:
                data["hist_deferred"][msg_id] = n
            else:
                data["hist_deferred"].pop(msg_id, None)
            self._save_locked(data)
