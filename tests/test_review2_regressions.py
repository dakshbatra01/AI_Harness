"""Regression tests for the second review: test-case ids, task input robustness, clone hygiene, code-map
resilience, guidance performance."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness import cli
from harness.config import HARNESS_ROOT
from harness.repo import codemap
from harness.repo.manifest import read_guidance
from harness.verify.testrun import (
    TestFramework, build_command, canonical_test_id, is_test_command, split_test_list, test_id_matches,
)
from tests.fixtures.fixture_repo import make_fixture_repo


class TestIdTests(unittest.TestCase):
    def test_split_keeps_parametrized_ids_whole(self):
        self.assertEqual(split_test_list("a.py::t[1,2], b.py::u\nc.py::v; test_x (pkg.mod.C)"),
                         ["a.py::t[1,2]", "b.py::u", "c.py::v", "pkg.mod.C.test_x"])

    def test_commands_vs_ids(self):
        for cmd in ("python -m pytest -k x", "pytest tests", "./run_tests.sh", "npm test", "DJANGO_SETTINGS=x python t.py"):
            self.assertTrue(is_test_command(cmd), cmd)
        for tid in ("tests/t.py::test_a", "pkg.mod.C.test_x", "test_compound (tests.m.C)"):
            self.assertFalse(is_test_command(tid), tid)

    def test_matching_is_exact_not_substring(self):
        self.assertFalse(test_id_matches("t.py::test_parse", "t.py::test_parse_legacy"))
        self.assertTrue(test_id_matches("t.py::test_p", "t.py::test_p[a - b]"))
        self.assertTrue(test_id_matches("tests/m.py::C::t", "tests.m.C.t"))
        self.assertTrue(test_id_matches("C.t", "tests.m.C.t"))
        self.assertTrue(test_id_matches("tests/m.py", "tests/m.py::C::t"))
        self.assertEqual(canonical_test_id("a/b.py::C::t"), "a.b.C.t")

    def test_unittest_command_from_pytest_style_id(self):
        cmd = build_command(TestFramework("unittest", "python -m unittest"), ["tests/test_d.py::TestP::test_c"])
        self.assertEqual(cmd, "python -m unittest -v tests.test_d.TestP.test_c")


class LogReadBudgetTests(unittest.TestCase):
    def test_line_and_total_output_are_bounded(self):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.tools.runtime import LogStore

        with tempfile.TemporaryDirectory() as td:
            logs = LogStore(Path(td))
            log_id = logs.new_id()
            logs.write(log_id, "\n".join("needle " + "x" * 5000 for _ in range(100)))
            controller = Controller.__new__(Controller)
            controller.logs = logs
            controller.cfg = load_config(None, {"tools": {"obs_head_chars": 300, "obs_tail_chars": 300}})
            for args in ({"log_id": log_id, "grep": "needle"}, {"log_id": log_id, "start": 1, "end": 100}):
                output, error = controller._t_read_log(args)
                self.assertFalse(error)
                self.assertLess(len(output), 800)
                self.assertIn("omitted", output)


class VerificationPlanTests(unittest.TestCase):
    def test_agent_commands_cannot_displace_independent_regression_checks(self):
        from harness.config import load_config
        from harness.controller.controller import Controller

        controller = Controller.__new__(Controller)
        controller.cfg = load_config()
        controller.framework = TestFramework("pytest", "python -m pytest")
        controller.target_tests = []
        controller.state = SimpleNamespace(observed_tests=[], observed_scripts=[], route="DIRECT")
        controller.ws = SimpleNamespace(changed=lambda: [("M", "durations/core.py")])
        controller.manifest = SimpleNamespace(files=["durations/core.py", "tests/test_core.py"],
                                              test_files=["tests/test_core.py"])
        controller.codemap = None
        specs = controller._test_plan({"test_commands": [f"python -m pytest tests/t{i}.py" for i in range(10)]})
        self.assertEqual(len(specs), 10)
        self.assertIn("related", [s.kind for s in specs])
        self.assertIn("full", [s.kind for s in specs])


def _args(**kw):
    base = dict(issue=None, issue_file=None, issue_url=None, repo=None, test=None, commit=None, headless=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _stdin(text):
    f = io.StringIO(text)
    f.isatty = lambda: False
    return f


class InputTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name).resolve()
        self.repo, _ = make_fixture_repo(self.tmp / "r", git=False)

    def tearDown(self):
        self._td.cleanup()

    def test_piped_url_repo_and_tests_lines(self):
        piped = f"https://github.com/o/r/issues/7/\n{self.repo}\ntests/t.py::test_a, tests/t.py::test_b[1,2]\n"
        with mock.patch.object(sys, "stdin", _stdin(piped)), \
             mock.patch.object(cli, "_fetch_github_issue", return_value=("Title\n\nbody", "https://github.com/o/r.git")):
            task = cli.task_from_sources(_args())
        self.assertEqual((task.root, task.issue), (self.repo, "Title\n\nbody"))
        self.assertEqual(task.tests, ["tests/t.py::test_a", "tests/t.py::test_b[1,2]"])

    def test_swebench_shaped_json(self):
        spec = {"repo": str(self.repo), "problem_statement": "broken thing", "FAIL_TO_PASS": json.dumps(["t.py::test_a"]),
                "instance_id": "x__y-1"}
        with mock.patch.object(sys, "stdin", _stdin(json.dumps(spec))):
            task = cli.task_from_sources(_args())
        self.assertEqual((task.issue, task.tests), ("broken thing", ["t.py::test_a"]))

    def test_null_issue_json_does_not_crash(self):
        spec = {"repo": str(self.repo), "issue": None, "tests": ["t.py::test_a"]}
        with mock.patch.object(sys, "stdin", _stdin(json.dumps(spec))):
            task = cli.task_from_sources(_args())
        self.assertIn("failing test case", task.issue)

    def test_url_forms_recognised(self):
        for u in ("https://github.com/o/r/issues/12/", "https://www.github.com/o/r/issues/12",
                  "https://github.com/o/r/issues/12#issuecomment-1", "https://github.com/o/r/pull/3?x=1"):
            self.assertTrue(cli._is_issue_url(u), u)

    def test_fetch_failure_reprompts_instead_of_crashing(self):
        answers = iter(["https://github.com/o/r/issues/1", "quit"])
        with mock.patch("builtins.input", lambda *a: next(answers)), mock.patch("sys.stdout", io.StringIO()) as out, \
             mock.patch.object(cli, "_fetch_github_issue", side_effect=ValueError("could not fetch: HTTP 403 (rate limit)")):
            self.assertIsNone(cli.interactive_task(""))
        self.assertIn("rate limit", out.getvalue())

    def test_bad_repo_reasks_only_repo(self):
        answers = iter(["the issue", "EOF", "/definitely/missing", str(self.repo), ""])
        with mock.patch("builtins.input", lambda *a: next(answers)), mock.patch("sys.stdout", io.StringIO()):
            task = cli.interactive_task("")
        self.assertEqual((task.root, task.issue), (self.repo, "the issue"))


class CloneHygieneTest(unittest.TestCase):
    def test_next_task_gets_clean_clone_without_discarding_previous_patch(self):
        with tempfile.TemporaryDirectory() as td:
            src, _ = make_fixture_repo(Path(td) / "src_repo_for_clone_test")
            clones = []
            try:
                with mock.patch("sys.stdout", io.StringIO()):
                    dest = cli._clone(str(src), None)
                    clones.append(dest)
                    (dest / "durations" / "core.py").write_text("patched = True\n")
                    (dest / "stray.txt").write_text("x")
                    dest2 = cli._clone(str(src), None)
                    clones.append(dest2)
                self.assertNotEqual(dest, dest2)
                self.assertIn("patched", (dest / "durations" / "core.py").read_text())
                self.assertTrue((dest / "stray.txt").exists())
                self.assertNotIn("patched", (dest2 / "durations" / "core.py").read_text())
                self.assertFalse((dest2 / "stray.txt").exists())
            finally:
                for dest in clones:
                    if HARNESS_ROOT / "workspaces" in dest.parents:
                        shutil.rmtree(dest, ignore_errors=True)


class ResilienceTests(unittest.TestCase):
    def test_codemap_survives_parser_blowups(self):
        with mock.patch.object(codemap.ast, "parse", side_effect=RecursionError("too deep")):
            defs, imports = codemap._python_index("def f():\n    pass\n", "m.py")
        self.assertEqual([d.name for d in defs], ["f"])  # pattern fallback
        self.assertEqual(imports, [])

    def test_guarded_module_level_defs_are_indexed(self):
        src = "try:\n    from fast import speed\nexcept ImportError:\n    def speed(x):\n        return x\nif True:\n    class Cfg:\n        pass\n"
        defs, _ = codemap._python_index(src, "m.py")
        self.assertEqual({(d.name, d.depth) for d in defs}, {("speed", 0), ("Cfg", 0)})

    def test_guidance_is_fast_on_blobs(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "CONTRIBUTING.md").write_text("Run `pytest -q`.\n" + "A" * 60000 + "\n")
            t0 = time.time()
            g = read_guidance(Path(td))
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(g.test_commands, ["pytest -q"])


if __name__ == "__main__":
    unittest.main()


class SpecialCaseDetectorTests(unittest.TestCase):
    def test_flags_hardcoded_issue_example(self):
        from harness.verify.verifier import issue_literals, special_cased_literals
        lits = issue_literals("One item at 100.00, discount 10.00, tax 0.20 gives 110.00; expected 108.00. Quote \"1,200\" breaks.")
        line = 'if base == Decimal("100.00") and invoice.discount == Decimal("10.00"):'
        self.assertEqual(special_cased_literals(line, lits), ["10.00", "100.00"])
        self.assertEqual(special_cased_literals("return money((base - discount) * (1 + rate))", lits), [])

    def test_identifier_strings_and_line_numbers_are_not_literals(self):
        from harness.verify.verifier import issue_literals, special_cased_literals
        lits = issue_literals('{"name": "deploy", "deps": ["compile"]}\n  File "cli.py", line 37, in main')
        self.assertNotIn("deps", lits)
        self.assertNotIn("37,", lits)
        self.assertEqual(special_cased_literals('if key in ("deps", "aliases"):', lits), [])

    def test_failing_siblings(self):
        from harness.verify.verifier import SpecResult, TestSpec, Verifier
        from harness.verify.testrun import TestRun
        r = SpecResult(TestSpec("x", "related"), TestRun("x", 1), TestRun("x", 1),
                       {"still_failing": ["tests/test_s.py::test_max", "tests/test_other.py::test_z"], "fixed": ["tests/test_s.py::test_a"]})
        self.assertEqual(Verifier._failing_siblings([r], ["tests/test_s.py::test_a"]), ["tests/test_s.py::test_max"])
        self.assertEqual(Verifier._failing_siblings([r], []), [])

    def test_partial_target_run_does_not_support_issue_claim(self):
        from harness.verify.ledger import UNKNOWN
        from harness.verify.verifier import SpecResult, TestSpec, VerificationReport, Verifier
        from harness.verify.testrun import FAIL, PASS, TestRun

        base = TestRun("pytest tests/test_s.py", 1, {"test_first": FAIL, "test_second": FAIL})
        cand = TestRun("pytest tests/test_s.py", 1, {"test_first": PASS, "test_second": FAIL})
        transitions = {"fixed": ["test_first"], "still_failing": ["test_second"], "new_fail": [],
                       "regressed": [], "new_pass": [], "still_passing": []}
        result = SpecResult(TestSpec("pytest tests/test_s.py", "target"), base, cand, transitions)
        report = VerificationReport("candidate", "environment", "manifest", "INCONCLUSIVE")
        claim = Verifier.__new__(Verifier)._issue_claim([result], [], report)
        self.assertEqual(claim[0], UNKNOWN)
        self.assertIn("test_second", claim[1])

    def test_nonzero_or_unavailable_checks_cannot_support_claims(self):
        from harness.verify.ledger import UNKNOWN
        from harness.verify.verifier import SpecResult, TestSpec, VerificationReport, Verifier
        from harness.verify.testrun import FAIL, PASS, TestRun

        verifier = Verifier.__new__(Verifier)
        report = VerificationReport("candidate", "environment", "manifest", "INCONCLUSIVE")
        transition = {"fixed": ["test_issue"], "still_failing": [], "new_fail": [], "regressed": [],
                      "new_pass": [], "still_passing": []}
        base = TestRun("pytest", 1, {"test_issue": FAIL})
        candidate = TestRun("pytest", 1, {"test_issue": PASS})
        result = SpecResult(TestSpec("pytest", "target"), base, candidate, transition)
        self.assertEqual(verifier._issue_claim([result], [], report)[0], UNKNOWN)

        candidate.exit_code = 0
        candidate.timed_out = True
        self.assertEqual(verifier._issue_claim([result], [], report)[0], UNKNOWN)

        candidate.timed_out = False
        result.spec.kind = "full"
        result.transitions = {"fixed": [], "regressed": [], "still_failing": [], "still_passing": ["test_issue"],
                              "new_pass": [], "new_fail": [], "disappeared": [], "to_skip": []}
        candidate.exit_code = 1
        self.assertEqual(verifier._regression_claim([result], report)[0], UNKNOWN)


class ImplicitSubmitTest(unittest.TestCase):
    def test_prose_final_answer_with_changes_is_verified(self):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry
        with tempfile.TemporaryDirectory() as td:
            repo, issue = make_fixture_repo(Path(td) / "repo")
            fixed = (repo / "durations" / "core.py").read_text().replace("total = int(value)", "total += int(value)")
            script = [
                {"tool_calls": [{"name": "write_file", "args": {"path": ".harness_scratch/r.py", "content":
                    "from durations import parse_duration\nassert parse_duration('1h30m') == 5400\n"}},
                    {"name": "run_command", "args": {"command": "python .harness_scratch/r.py"}}]},
                {"tool_calls": [{"name": "write_file", "args": {"path": "durations/core.py", "content": fixed, "overwrite": True}}]},
                {"text": "Fixed: components are now summed. All tests pass."},   # prose, no tool call
                {"text": "Reviewed the diff; it is complete."},                    # prose again after self-review
            ]
            cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "budget": {"max_attempts": 1}})
            prov = ScriptedProvider(script, native=True)
            res = Controller(cfg, repo, issue, Path(td) / "runs" / "x", prov, Telemetry(None, verbose=False)).run()
            self.assertEqual(res.status, "VERIFIED")
            self.assertEqual(len(prov.calls), 4)  # no extra nudge/re-read cycles


class FunctionTagDialectTest(unittest.TestCase):
    def test_text_protocol_accepts_function_tags(self):
        from harness.controller.protocol import parse_text_actions, tool_specs
        specs = {s["name"]: s for s in tool_specs([])}
        text = ("Let me look.\n<function=read_file>\n<parameter=path>\nslugkit/slug.py\n</parameter>\n"
                "<parameter=start>\n5\n</parameter>\n</function>")
        calls, thought = parse_text_actions(text, specs, 5)
        self.assertEqual((calls[0].name, calls[0].args), ("read_file", {"path": "slugkit/slug.py", "start": 5}))
        self.assertEqual(thought, "Let me look.")


class JsonStringArgsTest(unittest.TestCase):
    def test_json_encoded_arrays_are_decoded(self):
        from harness.controller.protocol import tool_specs, validate_args
        specs = {s["name"]: s for s in tool_specs([])}
        a = {"path": "x.py", "edits": '[{"old_text": "a", "new_text": "b"}]'}
        self.assertIsNone(validate_args(specs["edit_file"], a))
        self.assertEqual(a["edits"], [{"old_text": "a", "new_text": "b"}])
        b = {"tests": '["tests/test_a.py", "tests/test_b.py"]'}
        self.assertIsNone(validate_args(specs["run_tests"], b))
        self.assertEqual(b["tests"], ["tests/test_a.py", "tests/test_b.py"])
        c = {"tests": "tests/test_a.py"}
        self.assertIsNone(validate_args(specs["run_tests"], c))
        self.assertEqual(c["tests"], ["tests/test_a.py"])


class ContextOverflowKeepsWorkTest(unittest.TestCase):
    def _run(self, script, overflow_at):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.provider.base import ContextOverflow
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry

        class Overflowing(ScriptedProvider):
            def chat(self, system, messages, tools):
                if len(self.calls) + 1 == overflow_at:
                    self.calls.append({"messages": messages})
                    raise ContextOverflow("simulated: context full")
                return super().chat(system, messages, tools)

        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        repo, issue = make_fixture_repo(Path(td) / "repo")
        cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "model": {"mask_observations": "off"}})
        prov = Overflowing(script, native=True)
        res = Controller(cfg, repo, issue, Path(td) / "runs" / "x", prov, Telemetry(None, verbose=False)).run()
        return res, prov, repo

    def test_pending_fix_is_verified_instead_of_discarded(self):
        from tests.fixtures.fixture_repo import FILES
        fixed = FILES["durations/core.py"].replace("total = int(value)", "total += int(value)")
        script = [
            {"tool_calls": [{"name": "write_file", "args": {"path": ".harness_scratch/r.py", "content":
                "from durations import parse_duration\nassert parse_duration('1h30m') == 5400\n"}},
                {"name": "run_command", "args": {"command": "python .harness_scratch/r.py"}}]},
            {"tool_calls": [{"name": "write_file", "args": {"path": "durations/core.py", "content": fixed, "overwrite": True}}]},
        ]
        res, prov, _ = self._run(script, overflow_at=3)
        self.assertEqual(res.status, "VERIFIED")
        self.assertEqual(res.metrics["attempts"], 1)

    def test_unproven_work_is_kept_for_the_next_attempt(self):
        from tests.fixtures.fixture_repo import FILES
        half = FILES["durations/core.py"].replace('"""Parse strings', '"""Parse (work in progress) strings')
        script = [{"tool_calls": [{"name": "write_file", "args": {"path": "durations/core.py", "content": half, "overwrite": True}}]}]
        res, prov, repo = self._run(script, overflow_at=2)
        attempt2_first = prov.calls[2]["messages"][0]["content"]
        self.assertIn("still contains your changes", attempt2_first)


class FileViewPinningTest(unittest.TestCase):
    def test_current_views_stay_visible_and_rereads_are_answered(self):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry
        from tests.fixtures.fixture_repo import FILES

        def read(path, **kw):
            return {"tool_calls": [{"name": "read_file", "args": {"path": path, **kw}}]}

        edited = FILES["durations/core.py"].replace('"""Duration helpers."""', '"""Duration helpers (edited)."""')
        script = [read("durations/core.py"), read("durations/__init__.py"), read("README.md"), read("tests/test_durations.py"),
                  read("tests/__init__.py"), read("README.md", start=2), read("durations/core.py"),
                  {"tool_calls": [{"name": "write_file", "args": {"path": "durations/core.py", "content": edited, "overwrite": True}}]},
                  read("durations/core.py"), read("README.md", start=3)]
        with tempfile.TemporaryDirectory() as td:
            repo, issue = make_fixture_repo(Path(td) / "repo")
            cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "budget": {"max_attempts": 1},
                                     "model": {"mask_observations": "on"}, "tools": {"keep_full_observations": 1}})
            prov = ScriptedProvider(script, native=True)
            Controller(cfg, repo, issue, Path(td) / "runs" / "x", prov, Telemetry(None, verbose=False)).run()

        def tool_results(call_no):
            return [m["content"] for m in prov.calls[call_no]["messages"] if m.get("role") == "tool"]

        after_reread = tool_results(7)  # sent with call 8: includes the result of the 2nd core.py read
        self.assertIn("unchanged since you last read", after_reread[-1])
        self.assertIn("def parse_duration", after_reread[0])  # first view kept although it is old (pinned)
        after_edit_read = tool_results(9)
        self.assertIn("(edited)", after_edit_read[-1])  # real content after the file changed
        self.assertTrue(any("outdated view of durations/core.py" in c for c in tool_results(10)))

    def test_no_match_hint_is_copy_ready(self):
        from harness.tools.editor import Editor
        from harness.tools.files import ToolError
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "m.py").write_text("def f(x):\n    return x + 1\n")
            with self.assertRaises(ToolError) as cm:
                Editor(Path(td)).edit("m.py", [{"old_text": "def f(x):\n  return x + 1", "new_text": "def f(x):\n    return x"}])
        msg = cm.exception.message
        self.assertIn("<<<\ndef f(x):\n    return x + 1\n>>>", msg)
        self.assertNotIn("|", msg.split("<<<")[1])


class AutoCheckAndCacheTest(unittest.TestCase):
    def test_edit_reruns_failing_reproducer_and_repeat_is_cached(self):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry
        from tests.fixtures.fixture_repo import FILES

        fixed = FILES["durations/core.py"].replace("total = int(value)", "total += int(value)")
        script = [
            {"tool_calls": [{"name": "write_file", "args": {"path": ".harness_scratch/r.py", "content":
                "from durations import parse_duration\nassert parse_duration('1h30m') == 5400\n"}},
                {"name": "run_command", "args": {"command": "python .harness_scratch/r.py"}}]},
            {"tool_calls": [{"name": "write_file", "args": {"path": "durations/core.py", "content": fixed, "overwrite": True}}]},
            {"tool_calls": [{"name": "run_command", "args": {"command": "python .harness_scratch/r.py"}}]},
        ]
        with tempfile.TemporaryDirectory() as td:
            repo, issue = make_fixture_repo(Path(td) / "repo")
            cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "budget": {"max_attempts": 1}})
            prov = ScriptedProvider(script, native=True)
            res = Controller(cfg, repo, issue, Path(td) / "runs" / "x", prov, Telemetry(None, verbose=False)).run()
        tool_msgs = [m["content"] for m in prov.calls[3]["messages"] if m.get("role") == "tool"]
        self.assertIn("auto-check after this edit: `python .harness_scratch/r.py` -> exit 0", tool_msgs[-2])
        self.assertIn("already ran on this exact code", tool_msgs[-1])
        self.assertEqual(res.status, "VERIFIED")


class RequirementChecksTest(unittest.TestCase):
    def _run(self, issue, script, **verify):
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry
        td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, td, True)
        repo, _ = make_fixture_repo(Path(td) / "repo")
        cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "budget": {"max_attempts": 1}, "verify": verify})
        prov = ScriptedProvider(script, native=True)
        Controller(cfg, repo, issue, Path(td) / "runs" / "x", prov, Telemetry(None, verbose=False)).run()
        return prov

    MULTI = ("parse_duration problems\n\n- `parse_duration('1h30m')` must return 5400\n- whitespace between parts must be allowed\n"
             "- an empty string should still raise ValueError\n")

    def test_multi_behaviour_issue_asks_for_requirements_and_review_lists_them(self):
        from tests.fixtures.fixture_repo import FILES
        fixed = FILES["durations/core.py"].replace("total = int(value)", "total += int(value)")
        acc = "1. 1h30m -> 5400\n2. whitespace allowed\n3. empty raises"
        script = [{"tool_calls": [{"name": "update_plan", "args": {"acceptance": acc}},
                                  {"name": "write_file", "args": {"path": "durations/core.py", "content": fixed, "overwrite": True}}]},
                  {"tool_calls": [{"name": "submit", "args": {"summary": "fix"}}]}]
        prov = self._run(self.MULTI, script)
        self.assertIn("list each required behaviour", prov.calls[0]["messages"][0]["content"])
        review = [m["content"] for m in prov.calls[2]["messages"] if m.get("role") == "tool"][-1]
        self.assertIn("SELF-REVIEW", review)
        self.assertIn("confirm each is implemented AND checked", review)
        self.assertIn("2. whitespace allowed", review)

    def test_single_bug_and_off_switch_skip_it(self):
        from tests.fixtures.fixture_repo import ISSUE
        prov = self._run(ISSUE, [])
        self.assertNotIn("list each required behaviour", prov.calls[0]["messages"][0]["content"])
        prov = self._run(self.MULTI, [], requirement_checks="off")
        self.assertNotIn("list each required behaviour", prov.calls[0]["messages"][0]["content"])

    def test_early_test_tip_names_a_related_test(self):
        from tests.fixtures.fixture_repo import ISSUE
        prov = self._run(ISSUE, [])
        self.assertIn("run_tests(tests=['tests/test_durations.py'])", prov.calls[0]["messages"][0]["content"])
