"""Bound startup observation and cleanup of isolated test processes."""
import os
import selectors
import subprocess
import time


def start_driver(test, argv, marker, timeout=30):
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)

    def cleanup():
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        else:
            for stream in (proc.stdout, proc.stderr):
                stream.close()

    test.addCleanup(cleanup)
    output = b""
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            remaining = max(0, deadline - time.monotonic())
            if not selector.select(remaining):
                break
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            output += chunk
            if marker.encode() in output:
                return proc
    cleanup()
    test.fail("driver did not reach %r within %ss (rc=%s, output=%r)"
              % (marker, timeout, proc.returncode, output[-500:]))
