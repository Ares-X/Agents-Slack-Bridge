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

Storage layout
--------------
``send_state.json`` (atomic write: tmp + fsync + replace + dir fsync, with
a ``.bak`` rotation of the previous good version).  All mutations serialize
on ``send_state.json.lock`` via ``fcntl.flock`` (EX for writes, SH for
reads), so two consumer processes can never interleave a read-modify-write.

Corruption rule: the file is NEVER silently treated as empty.
Load order is primary -> ``.tmp`` (leftover of a crashed write, itself
fsynced so it is a complete newer version) -> ``.bak``.  If none parses,
all three are quarantined to ``send_state.json.corrupt.<millis>*`` (evidence
preserved) and ``StateCorruptError`` is raised.  The consumer then refuses to
send anything (fail-closed) until an operator restores a good copy.

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


class StateCorruptError(Exception):
    """send_state.json and its backups are all unparseable.

    The damaged files were quarantined as ``send_state.json.corrupt.*``
    (evidence preserved) and a ``send_state.json.quarantined`` marker was left
    behind so that even after a restart the store refuses to come up empty.
    The consumer must NOT send while this holds: without state, every
    undelivered inbox message looks fresh and would be resent.
    Recovery: inspect the quarantined copies, restore a good one to
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
    def __init__(self, path):
        self.path = path
        self.lock_path = path + ".lock"
        self.tmp_path = path + ".tmp"
        self.bak_path = path + ".bak"
        self.quarantined_path = path + ".quarantined"
        with _locked(self.lock_path, True):
            data = self._load_locked()
            changed = False
            if self._is_v1(data):
                data = self._migrate_v1(data)
                changed = True
            # Restart rule: in-flight sends become uncertain, never sendable.
            for mid, e in data.get("sends", {}).items():
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

    def _load_locked(self):
        """Load, trying primary -> tmp -> bak.  Quarantines + raises on total
        failure; never silently returns empty."""
        paths = (self.path, self.tmp_path, self.bak_path)
        if not any(os.path.exists(p) for p in paths):
            if os.path.exists(self.quarantined_path):
                # 之前已隔离过：绝不静默变空，等人工恢复。
                raise StateCorruptError(
                    "send_state was quarantined earlier "
                    "(see send_state.json.corrupt.*); restore a good copy "
                    "to send_state.json and remove .quarantined")
            return {"v": SCHEMA_VERSION, "sends": {}, "hist_deferred": {}}
        for p in paths:
            if os.path.exists(p):
                data = _parse_file(p)
                if isinstance(data, dict):
                    if p != self.path:
                        print(f"send_state: recovered from {p}",
                              flush=True)
                    return data
        self._quarantine_locked()
        raise StateCorruptError(
            "send_state.json, .tmp and .bak are all unparseable; "
            "quarantined as send_state.json.corrupt.*")

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
        for p in (self.path, self.tmp_path, self.bak_path):
            if os.path.exists(p):
                try:
                    os.replace(p, "%s.corrupt.%d" % (self.path, stamp))
                except OSError:
                    pass
        # 隔离标记：重启后也不许静默变空（fail-closed）。
        try:
            with open(self.quarantined_path, "w") as f:
                f.write(json.dumps(
                    {"at": time.time(),
                     "reason": "primary/.tmp/.bak all unparseable; "
                               "evidence in send_state.json.corrupt.*"},
                    ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    # ---- v1 migration ----

    @staticmethod
    def _is_v1(data):
        return isinstance(data, dict) and data.get("v") != SCHEMA_VERSION

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

    # ---- public API (each call is a locked, durable transition) ----

    def ensure_usable(self):
        """Re-validate the state file.  Raises StateCorruptError while the
        store is unusable; the consumer must not send in that case."""
        with _locked(self.lock_path, False):
            self._load_locked()

    def _mutate(self, fn):
        with _locked(self.lock_path, True):
            data = self._load_locked()
            data.setdefault("v", SCHEMA_VERSION)
            data.setdefault("sends", {})
            result = fn(data["sends"])
            self._save_locked(data)
            return result

    def get(self, msg_id):
        with _locked(self.lock_path, False):
            data = self._load_locked()
            e = data.get("sends", {}).get(msg_id)
            return dict(e) if isinstance(e, dict) else None

    def claim(self, msg_id, channel, thread_ts, text_hash):
        """Durably record a send attempt BEFORE spawning send.py.

        Returns ``(entry, is_new)``.  ``is_new`` is False when another
        live consumer already holds the claim: the caller must NOT send,
        only verify.  This is the atomic mutual-exclusion primitive --
        without it two consumers could both spawn send.py for one message.
        """
        now = time.time()

        def _do(sends):
            e = sends.get(msg_id)
            if isinstance(e, dict):
                return (dict(e), False)
            e = {"status": STATUS_SENDING, "channel": channel,
                 "thread_ts": thread_ts or "", "text_hash": text_hash,
                 "attempts": 0, "claimed_at": now, "updated_at": now,
                 "owner_pid": os.getpid()}
            try:
                import socket
                e["owner_host"] = socket.gethostname()
            except Exception:
                pass
            sends[msg_id] = e
            return (dict(e), True)

        return self._mutate(_do)

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
            return int(data.get("hist_deferred", {}).get(msg_id, 0) or 0)

    def set_hist_deferred(self, msg_id, n):
        with _locked(self.lock_path, True):
            data = self._load_locked()
            data.setdefault("hist_deferred", {})
            if n:
                data["hist_deferred"][msg_id] = n
            else:
                data["hist_deferred"].pop(msg_id, None)
            self._save_locked(data)
