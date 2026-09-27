"""The optional terminal dashboard uses the same task and verification path as the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import curses
import select
import subprocess
import sys
import tempfile
import time
import unittest

from harness.tui import TaskForm, _issue
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


class IssueEditorTests(unittest.TestCase):
    class Screen:
        def __init__(self, keys):
            self.keys = iter(keys)

        def getmaxyx(self):
            return 24, 90

        def erase(self):
            pass

        def timeout(self, value):
            pass

        def addstr(self, *args):
            pass

        def move(self, *args):
            pass

        def refresh(self):
            pass

        def get_wch(self):
            return next(self.keys)

    def test_existing_issue_can_be_edited_and_saved(self):
        from unittest import mock

        screen = self.Screen([curses.KEY_UP, curses.KEY_HOME, "X", "\x07"])
        with mock.patch("harness.tui.curses.curs_set"):
            self.assertEqual(_issue(screen, "first\nsecond"), "Xfirst\nsecond")

    def test_cancel_preserves_issue_and_dot_finishes_paste(self):
        from unittest import mock

        with mock.patch("harness.tui.curses.curs_set"):
            self.assertIsNone(_issue(self.Screen(["x", "\x1b"]), "original"))
            self.assertEqual(_issue(self.Screen(list("New issue") + ["\n", ".", "\n"]), ""), "New issue")


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
                os.write(master, b"?")
                self.assertTrue(wait_for(b"KEYBOARD HELP"), output[-1000:])
                checkpoint = len(output)
                os.write(master, b"?")
                self.assertTrue(wait_for(b"Workspace:", timeout=5, start=checkpoint), output[-1000:])
                checkpoint = len(output)
                os.write(master, b"\t")
                self.assertTrue(wait_for(b"> Issue:", timeout=5, start=checkpoint), output[-1000:])
                os.write(master, b"\r")
                self.assertTrue(wait_for(b"EDIT ISSUE"), output[-1000:])
                checkpoint = len(output)
                os.write(master, b"\x07")
                self.assertTrue(wait_for(b"Status: READY", start=checkpoint), output[-1000:])
                os.write(master, b"r")
                self.assertTrue(wait_for(b"Finished: VERIFIED"), output[-1000:])
                os.write(master, b"v")
                self.assertTrue(wait_for(b"Evidence report"), output[-1000:])
                checkpoint = len(output)
                os.write(master, b"\x1b")
                self.assertTrue(wait_for(b"Workspace:", timeout=5, start=checkpoint), output[-1000:])
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
