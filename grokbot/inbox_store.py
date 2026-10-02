"""Durable inbox.jsonl helpers: append / ack / status under flock.

Ack identity is (channel, ts) — same as dedupe. Rewrites use temp + os.replace
while holding an exclusive lock on a *sidecar* lock file (`inbox.jsonl.lock`).
Using a sidecar avoids the classic race where os.replace swaps the data-file
inode out from under a flock held on an older fd.

Helpers hold the lock until flush/fsync (+ rename) completes.
"""
from __future__ import annotations

import fcntl
import json
import os
from typing import Iterable, List, Optional, Sequence, Set, Tuple

AckKey = Tuple[str, str]  # (channel, ts)


def msg_key(record: dict) -> AckKey:
    return (str(record.get("channel") or ""), str(record.get("ts") or ""))


def parse_ack_argv(argv: Sequence[str]) -> Set[AckKey]:
    """Parse CLI args as channel+ts pairs.

    Accepted forms:
      channel ts [channel ts ...]
      channel:ts [channel:ts ...]
    """
    keys: Set[AckKey] = set()
    i = 0
    args = list(argv)
    while i < len(args):
        a = args[i]
        if ":" in a:
            ch, _, ts = a.partition(":")
            if not ch or not ts:
                raise ValueError(f"invalid channel:ts token: {a!r}")
            keys.add((ch, ts))
            i += 1
            continue
        if i + 1 < len(args):
            keys.add((a, args[i + 1]))
            i += 2
            continue
        raise ValueError(
            f"ack identity requires channel+ts pairs; leftover arg: {a!r}"
        )
    return keys


def _lock_path(path: str) -> str:
    return path + ".lock"


def _acquire_lock(path: str, exclusive: bool = True):
    """Open/create sidecar lock file and flock it. Caller must close/unlock."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    lf = open(_lock_path(path), "a+", encoding="utf-8")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    except Exception:
        lf.close()
        raise
    return lf


def _release_lock(lf) -> None:
    try:
        fcntl.flock(lf, fcntl.LOCK_UN)
    finally:
        lf.close()


def _read_all_records(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def _atomic_rewrite(path: str, records: List[dict]) -> None:
    """Write records via temp + os.replace (caller holds sidecar exclusive lock)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp = path + ".tmp." + str(os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            for r in records:
                out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def append_record(path: str, record: dict) -> bool:
    """Append one record under exclusive sidecar lock. Skip (channel, ts) dups.

    Returns True if newly enqueued, False if duplicate. Durably flushed before
    releasing the lock.
    """
    key = msg_key(record)
    lf = _acquire_lock(path, exclusive=True)
    try:
        for r in _read_all_records(path):
            if msg_key(r) == key:
                return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    finally:
        _release_lock(lf)


def peek_undelivered(path: str) -> List[dict]:
    """Return undelivered records (shared sidecar lock)."""
    if not os.path.exists(path) and not os.path.exists(_lock_path(path)):
        return []
    lf = _acquire_lock(path, exclusive=False)
    try:
        return [r for r in _read_all_records(path) if not r.get("delivered")]
    finally:
        _release_lock(lf)


def update_records(
    path: str,
    keys: Iterable[AckKey],
    *,
    delivered: Optional[bool] = None,
    reply_status: Optional[str] = None,
) -> Tuple[int, Set[AckKey]]:
    """Update matching (channel, ts) rows. Returns (n_matched, missing_keys).

    Durable temp+rename under exclusive sidecar lock. Matching is idempotent.
    """
    want = {(str(c), str(t)) for c, t in keys}
    if not want:
        return 0, set()

    lf = _acquire_lock(path, exclusive=True)
    try:
        if not os.path.exists(path):
            return 0, want
        rows = _read_all_records(path)
        found: Set[AckKey] = set()
        dirty = False
        for r in rows:
            k = msg_key(r)
            if k not in want:
                continue
            found.add(k)
            if delivered is not None and r.get("delivered") != delivered:
                r["delivered"] = delivered
                dirty = True
            if (
                reply_status is not None
                and r.get("reply_status") != reply_status
            ):
                r["reply_status"] = reply_status
                dirty = True
        missing = want - found
        if dirty:
            _atomic_rewrite(path, rows)
        return len(found), missing
    finally:
        _release_lock(lf)


def ack_keys(path: str, keys: Iterable[AckKey]) -> Tuple[int, Set[AckKey]]:
    """Mark delivered=True for (channel, ts) keys. Durable rewrite."""
    return update_records(path, keys, delivered=True)


def set_reply_status(
    path: str, keys: Iterable[AckKey], status: str
) -> Tuple[int, Set[AckKey]]:
    """Set reply_status without delivering (e.g. 'sent', 'uncertain')."""
    return update_records(path, keys, reply_status=status)
