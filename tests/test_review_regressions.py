"""Regression tests for bugs found in the independent review (false VERIFIED, user-data safety, crashes)."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.tools.runtime import CommandRunner, LogStore, check_denied
from harness.verify.ledger import Ledger
from harness.verify.testrun import TestFramework, parse_output
from harness.verify.verifier import VERIFIED, TestExecutor, TestSpec, Verifier
from harness.workspace import Workspace
from tests.fixtures.fixture_repo import make_fixture_repo

PY = sys.executable


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name).resolve()
        self.repo, _ = make_fixture_repo(self.tmp / "repo")
        self.ws = Workspace(self.repo, self.tmp / "state")
        self.ws.init()
        self.runner = CommandRunner(self.repo, LogStore(self.tmp / "state" / "logs"), 4000, 4000, 60)
        self.ex = TestExecutor(self.ws, self.runner, TestFramework("unittest", f"{PY} -m unittest"), 60)
        self.v = Verifier(self.ws, self.ex, Ledger(None), {"flaky_reruns": 1})

    def tearDown(self):
        self._td.cleanup()

    def noop_patch(self):
        core = self.repo / "durations" / "core.py"
        core.write_text(core.read_text() + "\n# comment only\n")


class FalseVerifiedTests(Base):
    def test_diff_audit_blocker_skips_expensive_test_runs(self):
        test = self.repo / "tests" / "test_durations.py"
        test.write_text(test.read_text().replace('        self.assertEqual(parse_duration("45s"), 45)\n', '        parse_duration("45s")\n'))
        with mock.patch.object(self.ex, "run", side_effect=AssertionError("candidate test ran")), \
             mock.patch.object(self.ex, "run_base", side_effect=AssertionError("base test ran")):
            rep = self.v.verify([TestSpec(f"{PY} -m unittest -v tests.test_durations", "target")])
        self.assertEqual(rep.status, "FAILED", rep.summary())
        self.assertEqual(rep.claims["diff_scope"][0], "CONTRADICTED")
        self.assertEqual(rep.results, [])

    def test_every_declared_target_must_be_observed(self):
        core = self.repo / "durations" / "core.py"
        core.write_text(core.read_text().replace("total = int(value) * _UNITS[unit]", "total += int(value) * _UNITS[unit]"))
        test = self.repo / "tests" / "test_durations.py"
        test.write_text(test.read_text().replace(
            "    def test_empty_raises(self):",
            "    def test_compound(self):\n        self.assertEqual(parse_duration('1h30m'), 5400)\n\n    def test_empty_raises(self):",
        ))
        declared = ["tests.test_durations.TestParse.test_compound", "tests.test_durations.TestParse.test_missing"]
        rep = self.v.verify([TestSpec(f"{PY} -m unittest -v tests.test_durations", "target")], declared)
        self.assertIn("tests.test_durations.TestParse.test_compound", rep.fixed_tests)
        self.assertNotEqual(rep.status, VERIFIED, rep.summary())
        self.assertEqual(rep.claims["issue_behavior"][0], "UNKNOWN")

        test.write_text(test.read_text().replace(
            "    def test_empty_raises(self):",
            "    @unittest.skip('not implemented')\n    def test_missing(self):\n        pass\n\n    def test_empty_raises(self):",
        ))
        rep = self.v.verify([TestSpec(f"{PY} -m unittest -v tests.test_durations", "target")], declared)
        self.assertNotEqual(rep.status, VERIFIED, rep.summary())
        self.assertIn("skipped", rep.claims["issue_behavior"][1])

    def test_base_cache_tracks_scratch_contents(self):
        self.noop_patch()
        repro = self.ws.scratch / "repro.py"
        repro.write_text("raise SystemExit(1)  # typo version\n")
        self.ex.run_base(f"{PY} .harness_scratch/repro.py", [])
        repro.write_text("print('ok')\n")  # now always passes
        rep = self.v.verify([TestSpec(f"{PY} .harness_scratch/repro.py", "repro")])
        self.assertNotEqual(rep.status, VERIFIED, rep.summary())

    def test_kept_test_keeps_exec_bit(self):
        self.noop_patch()
        t = self.repo / "tests" / "check_issue.sh"
        t.write_text("#!/bin/sh\nexit 0\n")
        t.chmod(t.stat().st_mode | stat.S_IXUSR)
        rep = self.v.verify([TestSpec("tests/check_issue.sh", "target")])
        self.assertNotEqual(rep.status, VERIFIED, rep.summary())

    def test_regression_claim_needs_passing_tests(self):
        self.noop_patch()
        rep = self.v.verify([TestSpec(f"{PY} -c 'import sys; sys.exit(1)'", "related")])
        self.assertNotEqual(rep.claims["no_regressions"][0], "SUPPORTED")


class DataSafetyTests(Base):
    def test_revert_restores_mode_and_created_dirs(self):
        core = self.repo / "durations" / "core.py"
        orig = core.read_bytes()
        core.write_text("broken = 1\n")
        (self.repo / "newpkg" / "sub").mkdir(parents=True)
        (self.repo / "newpkg" / "sub" / "m.py").write_text("x = 1\n")
        self.ws.revert_paths(["durations/core.py", "newpkg"])
        self.assertEqual(core.read_bytes(), orig)
        self.assertFalse((self.repo / "newpkg").exists())
        self.assertEqual(self.ws.changed(), [])

    def test_crlf_preserved_despite_gitattributes(self):
        repo = self.tmp / "crlf"
        repo.mkdir()
        (repo / ".gitattributes").write_text("* text=auto\n")
        (repo / "a.py").write_bytes(b"x = 1\r\n")
        ws = Workspace(repo, self.tmp / "st2")
        ws.init()
        (repo / "a.py").write_bytes(b"x = 2\r\n")
        cand = ws.tree()
        with ws.base_swap():
            self.assertEqual((repo / "a.py").read_bytes(), b"x = 1\r\n")
        self.assertEqual(ws.tree(), cand)
        self.assertEqual((repo / "a.py").read_bytes(), b"x = 2\r\n")

    def test_nested_git_repo_does_not_crash_tree(self):
        os.system(f"git init -q {self.repo / 'sandbox'}")
        self.ws.tree()  # must not raise


class ParsingAndDenyTests(unittest.TestCase):
    def test_parametrized_ids_with_dash(self):
        out = ("=== short test summary info ===\nPASSED tests/t.py::test_p[a - b]\nPASSED tests/t.py::test_p[a - c]\n"
               "FAILED tests/t.py::test_q[x - y] - AssertionError: boom\n=== 1 failed, 2 passed in 0.1s ===")
        o, _, _ = parse_output("pytest -rA", out, "", 1)
        self.assertEqual(o, {"tests/t.py::test_p[a - b]": "PASS", "tests/t.py::test_p[a - c]": "PASS", "tests/t.py::test_q[x - y]": "FAIL"})

    def test_denylist_precision(self):
        for ok in ("python -m pytest -k shutdown", "grep -rn 'def shutdown' src", "rm -rf build", "git checkout -b fix"):
            self.assertIsNone(check_denied(ok), ok)
        for bad in ("git checkout -- .", "git checkout HEAD -- src/", "git restore src/x.py", "git -C . reset --hard", "rm -rf ./*", "rm -r -f /"):
            self.assertIsNotNone(check_denied(bad), bad)


if __name__ == "__main__":
    unittest.main()
