"""Append-only inbox with tombstone acknowledgements.

Concurrency model
-----------------
ALL operations coordinate through ``fcntl.flock`` on a dedicated lock file
(``inbox.lock``), never on the data file itself.  Locking the data file is
unsafe across atomic replace: a writer that opened the path before the
replace would keep appending to the stale inode and lose data.

- append : EX lock -> write + flush + file fsync + dir fsync -> unlock
- ack    : EX lock -> append tombstone lines (no in-place rewrite in hot path)
- peek   : SH lock -> read all, filter out tombstoned msg_ids
- compact: EX lock -> temp file + fsync + ``os.replace`` + dir fsync
           (maintenance only; safe because every writer serializes on the
           lock file, so nobody can hold the stale inode)

Message identity (unified)
--------------------------
``msg_id = "<channel>:<ts>"``.  Enqueue dedup AND ack both use ``msg_id``,
so two channels may share a ``ts`` without one ack clobbering the other.

Legacy queue upgrade (v1 -> v2)
-------------------------------
Queues written by the pre-msg_id bridge have records WITHOUT ``msg_id``
and mark completion with an in-place ``"delivered": true`` flag.  The first
operation in each process runs a one-time migration (under the EX lock):

- legacy ``delivered=true``  -> ack tombstone (never replayed as new)
- legacy ``delivered=false`` -> v2 message with computed ``msg_id``
- the rewrite is temp + fsync + ``os.replace`` + dir fsync, so a crash
  leaves either the old or the new file; re-running converges (idempotent).
- a ``{"type": "schema", "v": 2}`` marker line records completion.

Reads additionally tolerate unmigrated legacy records (identity computed on
the fly, ``delivered=true`` treated as acked) so no message is ever silently
invisible.

Torn tail handling
------------------
If the file does not end with a newline (torn write from a crash), the
partial bytes are moved to ``inbox.jsonl.corrupt.<millis>`` (evidence kept)
and the file truncated BEFORE any new append.  Appending onto a torn line
would fuse two records into one unparseable line and silently swallow the
new message while reporting success.

Duplicate-append durability
---------------------------
When ``append_record`` hits an existing ``msg_id`` it fsyncs the data file
BEFORE returning ``False``.  Rationale: the first append may have failed its
fsync (record on disk but not durable); confirming durability before
reporting "already stored" is what lets the caller safely ACK to Slack.
An fsync failure here raises instead of returning success.

Compaction & retention
----------------------
``compact()`` drops acked messages but KEEPS their tombstones for
``TOMBSTONE_RETENTION_SECONDS`` (7 days).  Slack's at-least-once redelivery
happens within minutes; the retention window is a generous upper bound.

Recovery rules (explicit):
1. Redelivery inside the retention window -> tombstone present ->
   ``append_record`` dedups -> never re-queued.
2. Redelivery after the tombstone expired (and was compacted away) ->
   treated as a new message and re-queued.  This is the documented
   at-least-once trade-off; raise ``TOMBSTONE_RETENTION_SECONDS`` if your
   redelivery source needs longer.
3. Legacy ``delivered=true`` records met by ``compact`` are converted to
   tombstones (dedup info preserved, never silently dropped).

Record format (v2)
------------------
Message: ``{"msg_id": ..., "channel": ..., "user": ..., "text": ...,
"kind": "dm"|"mention", "ts": ..., "thread_ts": ..., "received_at": ...}``

Ack tombstone: ``{"type": "ack", "msg_id": ..., "at": ...}``
Deliberate quiet tombstones additionally retain ``disposition="no-reply"``,
``reason`` and ``source`` (original text, identity, thread and text hash).
Compaction retains the complete tombstone for the same retention window.
Schema marker: ``{"type": "schema", "v": 2}``
Name resolution (channel/user display names) is intentionally NOT done here;
see ``resolve.py``.  The hot path must stay free of slow API calls.
"""
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX_PATH = os.path.join(BASE, "inbox.jsonl")
LOCK_PATH = os.path.join(BASE, "inbox.lock")

SCHEMA_VERSION = 2
TOMBSTONE_RETENTION_SECONDS = 7 * 24 * 3600

# process-local: migration already checked for this path
_MIGRATED = {}


def msg_id(channel, ts):
    """Unified message identity used by enqueue dedup and ack."""
    return "%s:%s" % (channel, ts)


@contextmanager
def _locked(exclusive):
    # The lock file is opened separately from the data file so that
    # atomic replace of the data file can never strand a writer.
    with open(LOCK_PATH, "a+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_all_lines():
    if not os.path.exists(INBOX_PATH):
        return []
    with open(INBOX_PATH, "r") as f:
        return f.readlines()


def _parse(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def _identity(r):
    """Best-effort identity for any record, v2 or legacy.

    Never returns None: records that lack channel/ts (unidentifiable)
    get a content-hash identity so they stay visible instead of being
    silently dropped.
    """
    mid = r.get("msg_id")
    if mid:
        return mid
    ch, ts = r.get("channel"), r.get("ts")
    if ch and ts:
        return msg_id(ch, ts)
    digest = hashlib.sha256(
        json.dumps(r, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return "legacy:" + digest


def _is_legacy_done(r):
    """Legacy completion marker: no v2 type, in-place delivered=true."""
    return not r.get("type") and bool(r.get("delivered"))


def _classify(r):
    t = r.get("type")
    if t == "ack":
        return "ack"
    if t == "schema":
        return "schema"
    if r.get("msg_id"):
        return "msg"
    return "legacy"


def _fsync_dir():
    dirfd = os.open(BASE, os.O_DIRECTORY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _fsync_data_file():
    """Confirm the data file AND its directory entry are durable.

    Raises on failure.  The directory fsync matters for files whose
    creation was never confirmed (e.g. a previous append reported
    success without it); confirming it on the duplicate path closes
    that hole at negligible cost.
    """
    if not os.path.exists(INBOX_PATH):
        return
    with open(INBOX_PATH, "a") as f:
        os.fsync(f.fileno())
    _fsync_dir()


def _quarantine_torn_tail():
    """Move a torn last line to an evidence file and truncate.

    MUST be called with the EX lock held, before any append-style write.
    A crash mid-append can leave the file without a trailing newline; the
    next append would otherwise fuse its record onto the partial line,
    producing one unparseable line and silently swallowing the new message.
    """
    if not os.path.exists(INBOX_PATH):
        return
    with open(INBOX_PATH, "r+b") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return
        # Find the start of the last line: scan back for b"\n".
        # The torn line itself is bounded in practice; fall back to a full
        # read only if the tail window has no newline at all.
        scan = min(size, 65536)
        f.seek(size - scan)
        tail = f.read()
        if tail.endswith(b"\n"):
            return  # clean tail
        nl = tail.rfind(b"\n")
        if nl == -1 and scan < size:
            f.seek(0)
            tail = f.read()
            nl = tail.rfind(b"\n")
            torn_start = nl + 1
        elif nl == -1:
            torn_start = 0
        else:
            torn_start = size - scan + nl + 1
        f.seek(torn_start)
        torn = f.read()
        if not torn:
            return
        ev_path = "%s.corrupt.%d" % (INBOX_PATH, int(time.time() * 1000))
        with open(ev_path, "ab") as ev:
            ev.write(b"--- torn tail quarantined %s, %d bytes ---\n"
                     % (time.strftime("%Y-%m-%dT%H:%M:%S").encode(),
                        len(torn)))
            ev.write(torn)
            if not torn.endswith(b"\n"):
                ev.write(b"\n")
            ev.flush()
            os.fsync(ev.fileno())
        f.truncate(torn_start)
        f.flush()
        os.fsync(f.fileno())


def _has_schema_marker(lines):
    for line in lines:
        r = _parse(line)
        if r and r.get("type") == "schema" and r.get("v") == SCHEMA_VERSION:
            return True
    return False


def _legacy_identity(r):
    """Identity for a legacy record, ignoring the delivered flag so a
    pending body and its done marker always share one identity."""
    r2 = {k: v for k, v in r.items() if k != "delivered"}
    return _identity(r2)


def _migrate_lines(lines, now):
    """Convert legacy records to v2.  Returns the new line list.

    Two passes.  The first collects every identity that carries a legacy
    delivered=true marker ANYWHERE in the file; the second emits.  A done
    marker always wins over a pending body regardless of line order --
    with a single pass, a "false then true" pair for the same identity
    would keep the body and skip the marker, replaying a finished message.
    """
    parsed = [(_parse(l), l if l.endswith("\n") else l + "\n")
              for l in lines]
    done = set()
    for r, _line in parsed:
        if r is not None and _classify(r) == "legacy" and _is_legacy_done(r):
            done.add(_legacy_identity(r))
    out = []
    seen = set()
    tombstoned = set()
    for r, line in parsed:
        if r is None:
            out.append(line)
            continue
        kind = _classify(r)
        if kind in ("ack", "schema"):
            if r.get("msg_id"):
                seen.add(r["msg_id"])
            out.append(json.dumps(r, ensure_ascii=False) + "\n")
        elif kind == "msg":
            mid = r["msg_id"]
            if mid in seen:
                continue
            seen.add(mid)
            if mid in done:
                # legacy done 标记全局优先：v2 正文也转 tombstone。
                if mid not in tombstoned:
                    tombstoned.add(mid)
                    out.append(json.dumps(
                        {"type": "ack", "msg_id": mid, "at": now,
                         "migrated": "legacy-delivered"},
                        ensure_ascii=False) + "\n")
                continue
            out.append(json.dumps(r, ensure_ascii=False) + "\n")
        else:  # legacy
            mid = _legacy_identity(r)
            if mid in done:
                # 完成标记全局优先：只出一个 tombstone，绝不保留正文。
                if mid not in tombstoned:
                    tombstoned.add(mid)
                    seen.add(mid)
                    out.append(json.dumps(
                        {"type": "ack", "msg_id": mid, "at": now,
                         "migrated": "legacy-delivered"},
                        ensure_ascii=False) + "\n")
                continue
            if mid in seen:
                continue
            seen.add(mid)
            rec = dict(r)
            rec.pop("delivered", None)
            rec["msg_id"] = mid
            out.append(json.dumps(rec, ensure_ascii=False) + "\n")
    if not any(_parse(l) and _parse(l).get("type") == "schema" for l in out):
        out.append(json.dumps({"type": "schema", "v": SCHEMA_VERSION},
                              ensure_ascii=False) + "\n")
    return out


def _ensure_migrated():
    """One-time v1->v2 migration per process (idempotent, crash-safe)."""
    if _MIGRATED.get(INBOX_PATH):
        return
    with _locked(exclusive=True):
        if _MIGRATED.get(INBOX_PATH):
            return
        if not os.path.exists(INBOX_PATH):
            _MIGRATED[INBOX_PATH] = True
            return
        _quarantine_torn_tail()
        lines = _read_all_lines()
        if _has_schema_marker(lines):
            # 已迁移：但上一次迁移的目录 fsync 可能没完成（例如 EIO
            # 导致 replace 后 _fsync_dir 抛异常）。目录项不确认就不能
            # 报成功，否则崩溃后新文件可能丢失而调用方已经 ACK。
            # 持续失败时这里持续抛异常 -> 调用方持续拒绝 ACK。
            _fsync_dir()
            _MIGRATED[INBOX_PATH] = True
            return
        parsed = [_parse(l) for l in lines]
        if not any(r is not None and _classify(r) == "legacy" for r in parsed):
            # No legacy records: just stamp the marker (cheap append).
            with open(INBOX_PATH, "a") as f:
                f.write(json.dumps({"type": "schema", "v": SCHEMA_VERSION},
                                   ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            _fsync_dir()
        else:
            now = time.time()
            new_lines = _migrate_lines(lines, now)
            tmp = INBOX_PATH + ".migrate.tmp"
            with open(tmp, "w") as t:
                t.writelines(new_lines)
                t.flush()
                os.fsync(t.fileno())
            os.replace(tmp, INBOX_PATH)
            _fsync_dir()
        _MIGRATED[INBOX_PATH] = True


def append_record(record):
    """Append one record. Returns False when msg_id already present
    (message or tombstone) -- i.e. a duplicate redelivery.

    Durability contract: on a duplicate hit the data file is fsynced
    BEFORE returning False, so the caller may safely treat the record as
    stored.  An fsync failure raises instead of returning success.
    Directory durability is confirmed on every success boundary
    (migration recovery, new records, duplicate append): a persistent
    directory-sync failure keeps raising, so the bridge keeps refusing
    to ACK instead of confirming messages that a crash could lose.
    """
    _ensure_migrated()
    mid = _identity(record)
    record["msg_id"] = mid
    with _locked(exclusive=True):
        _quarantine_torn_tail()
        seen = set()
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            seen.add(_identity(r))
        if mid in seen:
            # Duplicate redelivery: the record is already on disk, but a
            # previous append may have failed its fsync.  Confirm durability
            # now; a failure raises and the caller must NOT ack to Slack.
            _fsync_data_file()
            return False
        with open(INBOX_PATH, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()                       # user-space buffer -> kernel
            os.fsync(f.fileno())            # kernel -> durable storage
        # An existing file may come from a creation or compaction whose
        # directory fsync failed. Confirm the entry before every successful
        # append, including later messages with different identities.
        _fsync_dir()
        return True


def read_undelivered():
    """Return message records whose msg_id has no tombstone, oldest first.

    Legacy records (no msg_id) are tolerated: identity is computed on the
    fly and ``delivered=true`` counts as acked, so nothing is silently
    invisible.  The one-time migration normally converts them first.
    """
    _ensure_migrated()
    with _locked(exclusive=False):
        acked = set()
        msgs = []
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            if _classify(r) == "ack":
                acked.add(_identity(r))
            elif _classify(r) in ("msg", "legacy"):
                if _is_legacy_done(r):
                    acked.add(_legacy_identity(r))
                else:
                    r = dict(r)
                    r["msg_id"] = _legacy_identity(r)
                    msgs.append(r)
    return [m for m in msgs if m["msg_id"] not in acked]


def ack(msg_ids):
    """Append one tombstone per msg_id. Atomic: single locked section,
    one write() + fsync. Never rewrites the file in place, so an
    interruption cannot corrupt already-stored messages."""
    if not msg_ids:
        return
    _ensure_migrated()
    now = time.time()
    with _locked(exclusive=True):
        _quarantine_torn_tail()
        created = not os.path.exists(INBOX_PATH)
        with open(INBOX_PATH, "a") as f:
            for mid in msg_ids:
                f.write(json.dumps(
                    {"type": "ack", "msg_id": mid, "at": now},
                    ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if created:
            # 新文件：目录项也必须落盘，否则崩溃后 tombstone 可能丢失。
            _fsync_dir()


def complete_no_reply(msg_id, reason):
    """Append a quiet completion with source evidence; caller holds send EX.

    This is deliberately separate from raw ack: unknown sources are refused,
    and repeated completion confirms file AND directory durability before
    returning success. The original tombstone/reason is never overwritten.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("no-reply requires a reason")
    if not os.path.exists(INBOX_PATH):
        raise ValueError("no-reply source inbox is missing")
    _ensure_migrated()
    with _locked(exclusive=True):
        _quarantine_torn_tail()
        source, completed = None, None
        for line in _read_all_lines():
            record = _parse(line)
            if not isinstance(record, dict):
                raise ValueError("no-reply source inbox contains an invalid record")
            if record.get("type") == "ack" and record.get("msg_id") == msg_id:
                completed = completed or record
            elif _classify(record) in ("msg", "legacy") \
                    and _identity(record) == msg_id:
                source = source or record
        if completed:
            _fsync_data_file()
            return ("no-reply" if completed.get("disposition") == "no-reply"
                    else "already-acked")
        if source is None:
            raise ValueError("no-reply source message is unknown: " + msg_id)
        channel, ts = msg_id.rsplit(":", 1)
        if any(key in source and str(source[key]) != expected
               for key, expected in (("channel", channel), ("ts", ts))):
            raise ValueError("no-reply source identity conflicts with msg_id: " + msg_id)
        evidence = {key: source[key] for key in (
            "msg_id", "channel", "ts", "thread_ts", "user", "bot_id", "kind",
            "text", "received_at", "client_msg_id") if key in source}
        evidence["text_sha256"] = hashlib.sha256(
            (source.get("text") or "").encode("utf-8")).hexdigest()
        tombstone = {"type": "ack", "msg_id": msg_id, "at": time.time(),
                     "disposition": "no-reply", "reason": reason.strip(),
                     "source": evidence}
        with open(INBOX_PATH, "a") as f:
            f.write(json.dumps(tombstone, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir()
        return "no-reply"


def compact(now=None):
    """Drop acked messages; keep tombstones for the retention window.

    Retention: tombstones live ``TOMBSTONE_RETENTION_SECONDS`` (7 days).
    Recovery rules:
      1. redelivery inside the window  -> deduped, never re-queued;
      2. redelivery after expiry        -> treated as a new message;
      3. legacy ``delivered=true``      -> converted to a tombstone here
         if migration somehow missed it (dedup info is never dropped).

    Runs under the lock file, writes temp + fsync + os.replace + dir fsync,
    so concurrent appenders can never write to a stale inode.
    Returns {"kept", "dropped", "tombstones_kept", "tombstones_expired"}.
    """
    _ensure_migrated()
    if now is None:
        now = time.time()
    cutoff = now - TOMBSTONE_RETENTION_SECONDS
    with _locked(exclusive=True):
        _quarantine_torn_tail()
        acked_all = set()
        tombstones = []   # (record, keep: bool)
        msgs = []
        schema_lines = []
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            kind = _classify(r)
            if kind == "ack":
                mid = r.get("msg_id")
                if not mid:
                    continue
                acked_all.add(mid)
                at = r.get("at")
                # A tombstone without a timestamp is conservatively kept:
                # we cannot prove it expired.
                keep = (at is None) or (at >= cutoff)
                tombstones.append((r, keep))
            elif kind == "schema":
                schema_lines.append(r)
            elif kind == "legacy" and _is_legacy_done(r):
                mid = _identity(r)
                acked_all.add(mid)
                tombstones.append(
                    ({"type": "ack", "msg_id": mid, "at": now,
                      "migrated": "legacy-delivered-compact"}, True))
            elif kind in ("msg", "legacy"):
                r = dict(r)
                r["msg_id"] = _identity(r)
                msgs.append(r)
        kept_msgs = [m for m in msgs if m["msg_id"] not in acked_all]
        kept_tombstones = [t for t, keep in tombstones if keep]
        expired = len(tombstones) - len(kept_tombstones)
        tmp = INBOX_PATH + ".compact.tmp"
        with open(tmp, "w") as t:
            for s in schema_lines:
                t.write(json.dumps(s, ensure_ascii=False) + "\n")
            for m in kept_msgs:
                t.write(json.dumps(m, ensure_ascii=False) + "\n")
            for tb in kept_tombstones:
                t.write(json.dumps(tb, ensure_ascii=False) + "\n")
            t.flush()
            os.fsync(t.fileno())
        os.replace(tmp, INBOX_PATH)
        _fsync_dir()
        return {"kept": len(kept_msgs),
                "dropped": len(msgs) - len(kept_msgs),
                "tombstones_kept": len(kept_tombstones),
                "tombstones_expired": expired}
