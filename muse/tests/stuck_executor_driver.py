"""Subprocess driver: stuck SDK executor task + final shutdown deadline.

Real SocketModeClient (capturing subclass so the driver can reach the
instance), real run_client_once()/serve_forever() path; only the network
is stubbed (WebClient.auth_test ok, connect() faked as instant success).
A task that blocks forever is submitted to the REAL SDK ThreadPoolExecutor;
the parent then sends SIGTERM. _close_bounded() times out, the supervisor
returns, and the final-deadline watchdog must os._exit() the process --
the parent asserts the PROCESS actually dies (not just "printed exit").

Queue recovery: before the stuck task, one record is appended to a temp
inbox (patched INBOX_PATH) and deliberately left un-ACKed, simulating a
kill between persist and ACK. The parent later verifies the record is
still undelivered, the file is intact, and a redelivery is deduped.

Usage: stuck_executor_driver.py <inbox_dir> <close_timeout> <final_deadline>
"""
import logging
import os
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge
import inbox_store
from slack_sdk.socket_mode import SocketModeClient as RealSocketModeClient


class _FlushHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()


logging.basicConfig(handlers=[_FlushHandler(sys.stdout)], level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", force=True)


class FakeWebClient:
    def __init__(self, *a, **kw):
        pass

    def auth_test(self):
        return {"user_id": "U_TEST"}


captured = {}


class CapturingSocketModeClient(RealSocketModeClient):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        captured["smc"] = self

    def connect(self):
        # Fake an established connection without network: run_client_once
        # proceeds to its listen loop; pool and threads are all real.
        print("DRIVER connect() faked as established", flush=True)


def _block_forever():
    threading.Event().wait()  # never set: stuck pool task


if __name__ == "__main__":
    inbox_dir = sys.argv[1]
    close_timeout = float(sys.argv[2])
    final_deadline = float(sys.argv[3])

    # isolated queue for the recovery assertions
    inbox_store.INBOX_PATH = os.path.join(inbox_dir, "inbox.jsonl")
    inbox_store.LOCK_PATH = os.path.join(inbox_dir, "inbox.lock")
    bridge.CLOSE_TIMEOUT_SECS = close_timeout
    bridge.FINAL_DEADLINE_SECS = final_deadline

    # 1) persist one record, deliberately leave it un-ACKed: in production
    #    the kill can land between persist and ACK.
    rec = {"kind": "dm", "channel": "D_TEST", "ts": "1234.5678",
           "text": "unacked-probe"}
    is_new = inbox_store.append_record(dict(rec))
    print("DRIVER persisted unacked record (new=%s)" % is_new, flush=True)

    # 2) real supervisor path with a reachable client instance
    def _inject_stuck_task():
        while "smc" not in captured:
            time.sleep(0.05)
        smc = captured["smc"]
        smc.message_workers.submit(_block_forever)
        time.sleep(0.5)  # let a worker pick it up
        print("DRIVER stuck task submitted to SDK executor", flush=True)

    threading.Thread(target=_inject_stuck_task, daemon=True).start()
    # serve_forever() 本身不启动看门狗 (由 main() 负责); 本测试显式启动,
    # 以验证进程级最终停机上限。
    bridge._start_final_deadline_watchdog()
    with mock.patch("slack_sdk.web.WebClient", FakeWebClient), \
         mock.patch("slack_sdk.socket_mode.SocketModeClient",
                    CapturingSocketModeClient):
        bridge.serve_forever("x", "y")
    print("DRIVER supervisor returned", flush=True)
    # 3) the watchdog should have os._exit()d by now; reaching here with
    #    the process still alive past deadline+margin means the bound failed.
    time.sleep(final_deadline + 10)
    print("DRIVER STILL ALIVE AFTER FINAL DEADLINE -- BUG", flush=True)
