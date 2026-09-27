"""The optional terminal dashboard uses the same task and verification path as the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest

from harness.tui import TaskForm
from tests.fixtures.fixture_repo import SCRIPTED_SOLUTION, make_fixture_repo


TARGET = "tests.test_durations.TestParse.test_compound"


class TaskFormTests(unittest.TestCase):
    def test_issue_file_and_test_id_use_cli_resolution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, issue = make_fixture_repo(root / "repo")
            issue_file = root / "issue.md"
            issue_file.write_text(issue, encoding="utf-8")
            task = TaskForm(str(repo), str(issue_file), TARGET).task()
            self.assertEqual(task.root, repo.resolve())
            self.assertEqual(task.issue, issue)
            self.assertEqual(task.tests, [TARGET])

    def test_requires_issue_or_test(self):
        with self.assertRaisesRegex(ValueError, "issue or at least one failing test"):
            TaskForm().task()


@unittest.skipUnless(sys.platform != "win32", "needs a POSIX pseudo-terminal")
class DashboardTerminalTests(unittest.TestCase):
    def test_make_run_opens_dashboard_in_terminal(self):
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 90, 0, 0))
        env = dict(os.environ, TERM="xterm-256color", AI_API_KEY="local-placeholder-key")
        proc = subprocess.Popen(["make", "run"], stdin=slave, stdout=slave, stderr=slave,
                                cwd=str(Path(__file__).resolve().parent.parent), env=env, close_fds=True)
        os.close(slave)
        output = b""
        try:
            deadline = time.monotonic() + 10
            while b"Evaluator dashboard" not in output and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    output += os.read(master, 65536)
            self.assertIn(b"Evaluator dashboard", output[-2000:])
            os.write(master, b"q")
            deadline = time.monotonic() + 10
            while proc.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        break
            self.assertEqual(proc.wait(timeout=2), 0, output[-1000:])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            os.close(master)

    def test_scripted_run_shows_result_and_patch(self):
        import fcntl
        import pty
        import struct
        import termios

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, issue = make_fixture_repo(root / "repo")
            script = root / "script.json"
            script.write_text(json.dumps(SCRIPTED_SOLUTION), encoding="utf-8")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 100, 0, 0))
            env = dict(os.environ, TERM="xterm-256color", HARNESS_RUNS_DIR=str(root / "runs"))
            proc = subprocess.Popen(
                [sys.executable, "-m", "harness", "tui", "--repo", str(repo), "--issue", issue,
                 "--test", TARGET, "--scripted", str(script)],
                stdin=slave, stdout=slave, stderr=slave, cwd=str(Path(__file__).resolve().parent.parent), env=env,
                close_fds=True,
            )
            os.close(slave)
            output = b""

            def wait_for(needle: bytes, timeout: float = 90, start: int = 0) -> bool:
                nonlocal output
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.3)
                    if ready:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            return False
                        if needle in output[start:]:
                            return True
                return False

            try:
                self.assertTrue(wait_for(b"Status: READY"), output[-1000:])
                os.write(master, b"r")
                self.assertTrue(wait_for(b"Finished: VERIFIED"), output[-1000:])
                os.write(master, b"d")
                self.assertTrue(wait_for(b"Patch viewer"), output[-1000:])
                checkpoint = len(output)
                os.write(master, b"d")
                self.assertTrue(wait_for(b"Workspace:", timeout=5, start=checkpoint), output[-1000:])
                os.write(master, b"q")
                deadline = time.monotonic() + 10
                while proc.poll() is None and time.monotonic() < deadline:
                    ready, _, _ = select.select([master], [], [], 0.2)
                    if ready:
                        try:
                            output += os.read(master, 65536)
                        except OSError:
                            break
                self.assertEqual(proc.wait(timeout=2), 0, output[-1000:])
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                os.close(master)
            self.assertIn("total += int", (repo / "durations" / "core.py").read_text())
            self.assertTrue(list((root / "runs").glob("*/result.json")))


if __name__ == "__main__":
    unittest.main()
