"""Tests for the evaluation-flow and efficiency features: task input (session / piped / test cases),
test-case targets, self-review gate, post-edit diagnostics, repository guidance, code map, tool profiles,
token accounting."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import cli
from harness.config import load_config
from harness.controller.controller import Controller
from harness.provider.scripted import ScriptedProvider
from harness.repo.codemap import CodeMap
from harness.repo.manifest import read_guidance
from harness.telemetry import Telemetry
from harness.tools.diagnostics import introduced_problems, undefined_names
from harness.tools.editor import Editor
from tests.fixtures.fixture_repo import SCRIPTED_SOLUTION, make_fixture_repo

FIX = '<tool name="edit_file">\n<path>durations/core.py</path>\n<old_text>\n        total = int(value) * _UNITS[unit]\n</old_text>\n<new_text>\n        total += int(value) * _UNITS[unit]\n</new_text>\n</tool>'
ADD_TEST = ('<tool name="edit_file">\n<path>tests/test_durations.py</path>\n<old_text>\n    def test_empty_raises(self):\n</old_text>\n'
            '<new_text>\n    def test_compound(self):\n        self.assertEqual(parse_duration("1h30m"), 5400)\n\n    def test_empty_raises(self):\n</new_text>\n</tool>')
SUBMIT = '<tool name="submit">\n<summary>sum all components</summary>\n</tool>'
TARGET = "tests.test_durations.TestParse.test_compound"


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name).resolve()

    def tearDown(self):
        self._td.cleanup()

    def run_ctrl(self, script, targets=None, overrides=None, native=False):
        repo, issue = make_fixture_repo(self.tmp / "repo")
        cfg = load_config(None, {"run": {"runs_dir": str(self.tmp / "runs")}, **(overrides or {})})
        run_dir = cfg.runs_dir() / "r"
        tel = Telemetry(run_dir, verbose=False)
        prov = ScriptedProvider(list(script), native=native)
        res = Controller(cfg, repo, issue, run_dir, prov, tel, target_tests=targets).run()
        tel.close()
        return res, prov, run_dir


def _args(**kw):
    base = dict(issue=None, issue_file=None, issue_url=None, repo=None, test=None, commit=None, headless=False)
    base.update(kw)
    return SimpleNamespace(**base)


class TaskInputTests(Tmp):
    def test_piped_json_with_tests(self):
        repo, _ = make_fixture_repo(self.tmp / "r", git=False)
        payload = json.dumps({"repo": str(repo), "issue": "compound durations are wrong", "tests": [TARGET]})
        fake = io.StringIO(payload)
        fake.isatty = lambda: False
        with mock.patch.object(sys, "stdin", fake), mock.patch.dict("os.environ", {}, clear=False):
            task = cli.task_from_sources(_args())
        self.assertEqual(task.root, repo)
        self.assertEqual(task.tests, [TARGET])
        self.assertIn("compound", task.issue)

    def test_tests_only_task_gets_generated_issue(self):
        repo, _ = make_fixture_repo(self.tmp / "r", git=False)
        fake = io.StringIO("")
        fake.isatty = lambda: False
        with mock.patch.object(sys, "stdin", fake):
            task = cli.task_from_sources(_args(repo=str(repo), test=[f"{TARGET}, tests.test_durations"]))
        self.assertEqual(task.tests, [TARGET, "tests.test_durations"])
        self.assertIn("failing test case", task.issue)

    def test_nothing_given_returns_none(self):
        fake = io.StringIO("")
        fake.isatty = lambda: False
        with mock.patch.object(sys, "stdin", fake):
            self.assertIsNone(cli.task_from_sources(_args()))

    def test_bare_run_without_tty_explains_instead_of_crashing(self):
        fake = io.StringIO("")
        fake.isatty = lambda: False
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", fake), mock.patch("sys.stdout", out):
            code = cli.main(["run"])
        self.assertEqual(code, 2)
        self.assertIn("No issue provided", out.getvalue())

    def test_interactive_task_prompts(self):
        repo, _ = make_fixture_repo(self.tmp / "r", git=False)
        answers = iter(["compound durations are wrong", "second line", "EOF", str(repo), TARGET])
        with mock.patch("builtins.input", lambda *a: next(answers)), mock.patch("sys.stdout", io.StringIO()):
            task = cli.interactive_task("")
        self.assertEqual(task.root, repo)
        self.assertEqual(task.issue, "compound durations are wrong\nsecond line")
        self.assertEqual(task.tests, [TARGET])

    def test_interactive_quit_and_eof(self):
        with mock.patch("builtins.input", lambda *a: "quit"), mock.patch("sys.stdout", io.StringIO()):
            self.assertIsNone(cli.interactive_task(""))

        def eof(*a):
            raise EOFError

        with mock.patch("builtins.input", eof), mock.patch("sys.stdout", io.StringIO()):
            self.assertIsNone(cli.interactive_task(""))  # Ctrl-D on an empty prompt ends the session cleanly


class TestCaseTargetTests(Tmp):
    def test_supplied_test_case_must_go_fail_to_pass(self):
        res, prov, _ = self.run_ctrl([FIX, ADD_TEST, SUBMIT], targets=[TARGET])
        self.assertEqual(res.status, "VERIFIED", res.summary)
        self.assertIn(TARGET, res.report.fixed_tests)
        self.assertIn("TEST CASE", prov.calls[0]["messages"][0]["content"])

    def test_supplied_test_case_still_failing_blocks_success(self):
        wrong = FIX.replace("total += int(value)", "total += 2 * int(value)")
        res, _, _ = self.run_ctrl([wrong, ADD_TEST, SUBMIT], targets=[TARGET], overrides={"budget": {"max_attempts": 1}})
        self.assertNotEqual(res.status, "VERIFIED")


class ReviewGateTests(Tmp):
    def test_review_once_then_verify_with_remembered_evidence(self):
        submit = '<tool name="submit">\n<summary>fix</summary>\n<reproducer>python .harness_scratch/repro.py</reproducer>\n</tool>'
        repro = ('<tool name="write_file">\n<path>.harness_scratch/repro.py</path>\n<content>\nfrom durations import parse_duration\n'
                 'assert parse_duration("1h30m") == 5400\n</content>\n</tool>')
        res, prov, run_dir = self.run_ctrl([repro, FIX, submit, SUBMIT])
        self.assertEqual(res.status, "VERIFIED", res.summary)
        obs = prov.calls[3]["messages"][-1]["content"]
        self.assertIn("SELF-REVIEW", obs)
        self.assertIn("total += int", obs)
        trace = (run_dir / "trace.jsonl").read_text()
        self.assertEqual(trace.count('"event": "verification"'), 1)  # reviewed once, verified once
        ev = json.loads((run_dir / "result.json").read_text())["evidence"]
        self.assertTrue(any(e["kind"] == "repro" for e in ev))  # reproducer from the first submit was kept

    def test_viewing_the_diff_counts_as_review(self):
        diff = '<tool name="repo_changes">\n<action>diff</action>\n</tool>'
        res, _, run_dir = self.run_ctrl([FIX, ADD_TEST, diff, SUBMIT])
        self.assertEqual(res.status, "VERIFIED", res.summary)
        self.assertNotIn("SELF-REVIEW", (run_dir / "trace.jsonl").read_text())


class DiagnosticsTests(Tmp):
    def test_undefined_name_reported_only_when_new(self):
        before = "import os\n\ndef f(x):\n    return missing_before(x)\n"
        after = "import os\n\ndef f(x):\n    return missing_before(Path(x))\n"
        msg = introduced_problems(Path("m.py"), before, after)
        self.assertIn("`Path`", msg)
        self.assertNotIn("missing_before", msg)

    def test_scope_and_dynamic_cases_are_quiet(self):
        src = ("from x import *\nprint(anything)\n")
        self.assertIsNone(undefined_names(src))
        ok = "class A:\n    def m(self, v):\n        return [w for w in v if w]\ntry:\n    pass\nexcept E as err:\n    print(err)\n"
        self.assertEqual(undefined_names(ok), {"E": 6})

    def test_editor_appends_warning_but_applies_edit(self):
        (self.tmp / "m.py").write_text("def f(x):\n    return x\n")
        out, _ = Editor(self.tmp).edit("m.py", [{"old_text": "    return x", "new_text": "    return helper(x)"}])
        self.assertIn("[diagnostics]", out)
        self.assertIn("helper(x)", (self.tmp / "m.py").read_text())


class GuidanceTests(Tmp):
    def test_commands_and_excerpt(self):
        (self.tmp / "AGENTS.md").write_text("[![ci](b)](c)\n\nRun `python -m pytest -q tests` first.\n")
        (self.tmp / "CONTRIBUTING.md").write_text("x\n" * 80 + "Testing:\n    tox -e py311\n" + "y\n" * 80)
        g = read_guidance(self.tmp)
        self.assertEqual(g.sources, ["AGENTS.md", "CONTRIBUTING.md"])
        self.assertEqual(g.test_commands, ["python -m pytest -q tests", "tox -e py311"])
        self.assertNotIn("[![", g.excerpt)
        self.assertLess(len(g.excerpt), 1700)

    def test_absent(self):
        self.assertEqual(read_guidance(self.tmp).render(), "")


class CodeMapTests(Tmp):
    def _repo(self):
        pkg = self.tmp / "pkg"
        (pkg / "tests").mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "units.py").write_text("UNIT_TABLE = {'h': 3600}\n\ndef unit_seconds(u):\n    return UNIT_TABLE[u]\n")
        (pkg / "parser.py").write_text("from pkg.units import unit_seconds\n\nclass DurationParser:\n    def parse_text(self, t):\n        return unit_seconds(t)\n")
        (pkg / "cli.py").write_text("from pkg.parser import DurationParser\n\ndef run_cli(a):\n    return DurationParser().parse_text(a)\n")
        for i in range(12):
            (pkg / f"noise{i}.py").write_text(f"def unrelated_helper_{i}(x):\n    return x\n")
        (pkg / "tests" / "test_parser.py").write_text("from pkg.parser import DurationParser\n\ndef test_p():\n    assert DurationParser()\n")
        return [p.relative_to(self.tmp).as_posix() for p in self.tmp.rglob("*.py")]

    def test_ranking_follows_dependencies_and_tests(self):
        cm = CodeMap(self.tmp, self._repo())
        text = cm.render({"pkg/parser.py": 10}, {"DurationParser"}, 800)
        order = [ln[:-1] for ln in text.splitlines() if not ln.startswith(" ")]
        self.assertEqual(order[0], "pkg/parser.py")
        self.assertIn("pkg/units.py", order)
        self.assertFalse(any(o.startswith("pkg/noise") for o in order))
        self.assertIn("class DurationParser", text)
        self.assertEqual(cm.tests_using(["pkg/parser.py"]), ["pkg/tests/test_parser.py"])

    def test_budget_and_time_cap(self):
        files = self._repo()
        cm = CodeMap(self.tmp, files)
        self.assertLessEqual(len(cm.render({"pkg/parser.py": 1}, set(), 60)) / 3.5, 90)
        partial = CodeMap(self.tmp, files, time_budget_s=0.0)
        self.assertFalse(partial.complete)


class ProfileAndAccountingTests(Tmp):
    def test_bash_profile_limits_tools(self):
        res, prov, _ = self.run_ctrl([], overrides={"tools": {"profile": "bash"}}, native=True)
        names = sorted(t["name"] for t in prov.calls[0]["tools"])
        self.assertEqual(names, ["repo_changes", "run_command", "submit", "update_plan"])

    def test_token_breakdown_and_model_config_recorded(self):
        res, _, run_dir = self.run_ctrl(SCRIPTED_SOLUTION)
        r = json.loads((run_dir / "result.json").read_text())
        self.assertGreater(r["metrics"]["token_breakdown_est"]["fixed_prompt_and_tools"], 0)
        self.assertIn("name", r["model_config"])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(sys.platform != "win32", "needs a POSIX pseudo-terminal")
class TerminalSessionTest(Tmp):
    """`make run` in a real pseudo-terminal: the harness waits for the task, then solves and verifies it."""

    def test_interactive_session_end_to_end(self):
        import os
        import pty
        import re
        import select
        import subprocess
        import time

        repo, _ = make_fixture_repo(self.tmp / "repo")
        script = self.tmp / "s.json"
        script.write_text(json.dumps(SCRIPTED_SOLUTION))
        master, slave = pty.openpty()
        env = dict(os.environ, HARNESS_RUNS_DIR=str(self.tmp / "runs"))
        proc = subprocess.Popen([sys.executable, "-m", "harness", "run", "--plain", "--scripted", str(script)], stdin=slave, stdout=slave,
                                stderr=slave, cwd=str(Path(__file__).resolve().parent.parent), env=env, close_fds=True)
        os.close(slave)
        buf = b""

        def wait_for(pattern: str, timeout: float = 90) -> bool:
            nonlocal buf
            end = time.time() + timeout
            while time.time() < end:
                ready, _, _ = select.select([master], [], [], 0.3)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        return False
                    if not chunk:
                        return False
                    buf += chunk
                    if re.search(pattern.encode(), buf):
                        return True
            return False

        try:
            for pattern, line in (("finish with a line", "parse_duration('1h30m') returns 1800, expected 5400"), (None, "EOF"),
                                  ("Repository path", str(repo)), ("Failing test", TARGET), ("Solve another issue", "n")):
                if pattern:
                    self.assertTrue(wait_for(pattern), f"no prompt {pattern!r}; got: {buf[-500:]!r}")
                os.write(master, (line + "\n").encode())
            self.assertEqual(proc.wait(timeout=120), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            os.close(master)
        text = buf.decode(errors="replace")
        self.assertIn("STATUS: VERIFIED", text)
        self.assertIn(TARGET, text)
        self.assertIn("total += int", (repo / "durations" / "core.py").read_text())
