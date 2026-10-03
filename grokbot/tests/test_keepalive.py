"""keepalive.sh process identity and flock. No live Slack. No live PIDs killed.

The fake interpreter is a symlink to the system python, and bridge.py /
consumer/poll_consumer.py under the temp root only record their PID and sleep.
Cleanup signals only PIDs those scripts recorded.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "keepalive.sh"


def _py_body(startlog: Path) -> str:
    return (
        "import os, sys, time\n"
        f"open({str(startlog)!r}, 'a').write(str(os.getpid()) + ' ' + sys.argv[0] + '\\n')\n"
        "time.sleep(60)\n"
    )


def _write_fake_python(root: Path) -> None:
    py = root / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    if py.exists() or py.is_symlink():
        py.unlink()
    os.symlink(shutil.which("python3"), py)
    body = _py_body(root / "starts.txt")
    (root / "bridge.py").write_text(body)
    (root / "consumer").mkdir(exist_ok=True)
    (root / "consumer" / "poll_consumer.py").write_text(body)


def _fake_proc(proc: Path, pid: str, argv0: str, args: list[str], cwd: Path) -> None:
    d = proc / pid
    d.mkdir(parents=True)
    payload = argv0.encode() + b"\0" + b"\0".join(a.encode() for a in args) + b"\0"
    (d / "cmdline").write_bytes(payload)
    os.symlink(os.path.realpath(cwd), d / "cwd")


def _recorded_pids(root: Path) -> list[int]:
    path = root / "starts.txt"
    if not path.exists():
        return []
    pids = []
    for line in path.read_text().splitlines():
        part = line.split(" ", 1)[0]
        if part.isdigit():
            pids.append(int(part))
    return pids


def _kill_recorded(root: Path) -> None:
    for pid in _recorded_pids(root):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    time.sleep(0.05)
    for pid in _recorded_pids(root):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _live_matches(root: Path) -> list[tuple[str, list[str]]]:
    root_real = os.path.realpath(root)
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/cmdline", "rb") as fh:
                raw = fh.read().split(b"\0")
            cwd = os.path.realpath(f"/proc/{name}/cwd")
        except OSError:
            continue
        args = [p.decode() for p in raw if p]
        if not args:
            continue
        if cwd == root_real and args[0].endswith("/venv/bin/python"):
            found.append((name, args[1:]))
    return found


class KeepaliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "root"
        self.root.mkdir()
        _write_fake_python(self.root)
        self.proc = Path(self.tmp.name) / "proc"
        self.proc.mkdir()

    def tearDown(self):
        _kill_recorded(self.root)
        self.tmp.cleanup()

    def _run(self, *, real_proc: bool = False) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["KEEPALIVE_ROOT"] = str(self.root)
        if not real_proc:
            env["KEEPALIVE_PROC"] = str(self.proc)
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def test_starts_when_absent(self):
        # Real /proc so started children are visible. cwd is the temp root,
        # so the live deploy processes do not match.
        proc = self._run(real_proc=True)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("started bridge.py pid=", proc.stdout)
        self.assertIn("started consumer/poll_consumer.py pid=", proc.stdout)
        scripts = sorted(args[0] for _pid, args in _live_matches(self.root) if args)
        self.assertEqual(scripts, ["bridge.py", "consumer/poll_consumer.py"])
        proc2 = self._run(real_proc=True)
        self.assertIn("bridge.py already running", proc2.stdout)
        self.assertIn("consumer/poll_consumer.py already running", proc2.stdout)
        self.assertNotIn("not running, starting", proc2.stdout)
        self.assertEqual(len(_live_matches(self.root)), 2)
        self.assertEqual(len(_recorded_pids(self.root)), 2)

    def test_fake_proc_same_cwd_skips_start(self):
        _fake_proc(
            self.proc,
            "41001",
            str(self.root / "venv" / "bin" / "python"),
            ["bridge.py"],
            self.root,
        )
        _fake_proc(
            self.proc,
            "41002",
            "./venv/bin/python",
            ["consumer/poll_consumer.py"],
            self.root,
        )
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("bridge.py already running", proc.stdout)
        self.assertIn("consumer/poll_consumer.py already running", proc.stdout)
        self.assertFalse((self.root / "starts.txt").exists())

    def test_other_cwd_or_shell_cmdline_does_not_count(self):
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        _fake_proc(
            self.proc,
            "41003",
            str(self.root / "venv" / "bin" / "python"),
            ["bridge.py"],
            other,
        )
        _fake_proc(
            self.proc,
            "41004",
            "/usr/bin/bash",
            ["-c", f"{self.root}/venv/bin/python bridge.py"],
            self.root,
        )
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("bridge.py not running, starting", proc.stdout)
        self.assertIn("consumer/poll_consumer.py not running, starting", proc.stdout)
        # Fake /proc cannot show the children, so the script warns. The
        # children themselves are real and recorded; exactly one of each.
        started = (self.root / "starts.txt").read_text().splitlines()
        kinds = sorted(line.split(" ", 1)[1] for line in started)
        self.assertEqual(kinds, ["bridge.py", "consumer/poll_consumer.py"])

    def test_parallel_callers_start_once(self):
        env = os.environ.copy()
        env["KEEPALIVE_ROOT"] = str(self.root)
        procs = [
            subprocess.Popen(
                ["bash", str(SCRIPT)],
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err + out)
            outs.append(out)
        combined = "\n".join(outs)
        self.assertEqual(combined.count("started bridge.py pid="), 1, combined)
        self.assertEqual(
            combined.count("started consumer/poll_consumer.py pid="), 1, combined
        )
        self.assertIn("already running", combined)
        scripts = sorted(args[0] for _pid, args in _live_matches(self.root) if args)
        self.assertEqual(scripts, ["bridge.py", "consumer/poll_consumer.py"])
        self.assertEqual(len(_recorded_pids(self.root)), 2)

    def test_missing_python_does_not_start(self):
        py = self.root / "venv" / "bin" / "python"
        py.unlink()
        py.write_text("not a binary\n")
        py.chmod(0o644)
        proc = self._run()
        self.assertIn("python not executable", proc.stdout)
        self.assertNotIn("not running, starting", proc.stdout)
        self.assertFalse((self.root / "starts.txt").exists())


if __name__ == "__main__":
    unittest.main()
