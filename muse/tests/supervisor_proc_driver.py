"""Subprocess driver for supervisor signal tests (not a unittest module).

Runs the REAL serve_forever() with an always-failing runner so the process
sits in backoff; the parent test then sends SIGTERM/SIGINT and observes
exit latency, rebuild behavior and stderr.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bridge

# bridge import configured file logging; redirect to stdout for observability.
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", force=True)

calls = {"n": 0}


def failing_runner():
    calls["n"] += 1
    print("DRIVER runner failed #%d" % calls["n"], flush=True)
    raise RuntimeError("simulated client failure")


def wait_in_backoff(seconds):
    print("DRIVER entering backoff", flush=True)
    bridge._wait_interruptible(seconds)


if __name__ == "__main__":
    backoff = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    bridge.serve_forever("x", "y", runner=failing_runner,
                         initial_backoff=backoff, max_backoff=backoff,
                         wait_fn=wait_in_backoff)
    print("DRIVER exiting cleanly", flush=True)
