"""Append-only inbox with tombstone acknowledgements.

Concurrency model
-----------------
ALL operations coordinate through ``fcntl.flock`` on a dedicated lock file
(``inbox.lock``), never on the data file itself.  Locking the data file is
unsafe across atomic replace: a writer that opened the path before the
replace would keep appending to the stale inode and lose data.

- append : EX lock -> single ``write()`` + ``flush()`` + ``os.fsync()`` -> unlock
- ack    : EX lock -> append tombstone lines (no in-place rewrite in hot path)
- peek   : SH lock -> read all, filter out tombstoned msg_ids
- compact: EX lock -> temp file + fsync + ``os.replace`` + dir fsync
           (maintenance only; safe because every writer serializes on the
           lock file, so nobody can hold the stale inode)

Message identity (unified)
--------------------------
``msg_id = "<channel>:<ts>"``.  Enqueue dedup AND ack both use ``msg_id``,
so two channels may share a ``ts`` without one ack clobbering the other.

Record format (v2)
------------------
Message: ``{"msg_id": ..., "channel": ..., "user": ..., "text": ...,
"kind": "dm"|"mention", "ts": ..., "thread_ts": ..., "received_at": ...}``

Ack tombstone: ``{"type": "ack", "msg_id": ..., "at": ...}``
Name resolution (channel/user display names) is intentionally NOT done here;
see ``resolve.py``.  The hot path must stay free of slow API calls.
"""
import fcntl
import json
import os
import time
from contextlib import contextmanager

BASE = os.path.dirname(os.path.abspath(__file__))
INBOX_PATH = os.path.join(BASE, "inbox.jsonl")
LOCK_PATH = os.path.join(BASE, "inbox.lock")


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


def append_record(record):
    """Append one record. Returns False when msg_id already present
    (message or tombstone) -- i.e. a duplicate redelivery."""
    mid = record.get("msg_id") or msg_id(record.get("channel"), record.get("ts"))
    record["msg_id"] = mid
    with _locked(exclusive=True):
        seen = set()
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            if r.get("type") == "ack":
                seen.add(r.get("msg_id"))
            elif r.get("msg_id"):
                seen.add(r["msg_id"])
        if mid in seen:
            return False  # duplicate redelivery
        with open(INBOX_PATH, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()                       # user-space buffer -> kernel
            os.fsync(f.fileno())            # kernel -> durable storage
        return True


def read_undelivered():
    """Return message records whose msg_id has no tombstone, oldest first."""
    with _locked(exclusive=False):
        acked = set()
        msgs = []
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            if r.get("type") == "ack":
                if r.get("msg_id"):
                    acked.add(r["msg_id"])
            elif r.get("msg_id"):
                msgs.append(r)
    return [m for m in msgs if m["msg_id"] not in acked]


def ack(msg_ids):
    """Append one tombstone per msg_id. Atomic: single locked section,
    one write() + fsync. Never rewrites the file in place, so an
    interruption cannot corrupt already-stored messages."""
    if not msg_ids:
        return
    now = time.time()
    with _locked(exclusive=True):
        with open(INBOX_PATH, "a") as f:
            for mid in msg_ids:
                f.write(json.dumps(
                    {"type": "ack", "msg_id": mid, "at": now},
                    ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def compact():
    """Drop acked messages and their tombstones (maintenance).

    Runs under the lock file, writes temp + fsync + os.replace + dir fsync,
    so concurrent appenders can never write to a stale inode.
    Returns {"kept": n, "dropped": m}.
    """
    with _locked(exclusive=True):
        acked = set()
        msgs = []
        for line in _read_all_lines():
            r = _parse(line)
            if not r:
                continue
            if r.get("type") == "ack":
                if r.get("msg_id"):
                    acked.add(r["msg_id"])
            elif r.get("msg_id"):
                msgs.append(r)
        kept = [m for m in msgs if m["msg_id"] not in acked]
        tmp = INBOX_PATH + ".compact.tmp"
        with open(tmp, "w") as t:
            for m in kept:
                t.write(json.dumps(m, ensure_ascii=False) + "\n")
            t.flush()
            os.fsync(t.fileno())
        os.replace(tmp, INBOX_PATH)
        dirfd = os.open(BASE, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)  # make the rename itself durable
        finally:
            os.close(dirfd)
        return {"kept": len(kept), "dropped": len(msgs) - len(kept)}
