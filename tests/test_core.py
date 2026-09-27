"""Capability-level tests (no model / API key required). Mirrors the blueprint's acceptance gates:
stale evidence after edit, FAIL->SKIP is not a fix, new regression, timeout, dirty workspace,
transactional edits, loop detection, secret hygiene, provider conversions."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness.config import load_config
from harness.controller.loopguard import LoopGuard, action_fp, obs_fp
from harness.controller.protocol import parse_text_actions, tool_specs, validate_args
from harness.provider import build_provider, detect_provider
from harness.provider.anthropic import AnthropicProvider
from harness.provider.openai_compat import OpenAICompatProvider
from harness.repo.anchors import extract_anchors
from harness.tools.editor import Editor
from harness.tools.files import ToolError, view_lines
from harness.tools.runtime import CommandRunner, LogStore, check_denied
from harness.util import REDACT, scrubbed_env
from harness.verify.ledger import STALE, Ledger
from harness.verify.testrun import TestFramework, classify_transitions, parse_output
from harness.verify.verifier import FAILED, INCONCLUSIVE, VERIFIED, TestExecutor, TestSpec, Verifier
from harness.workspace import Workspace
from tests.fixtures.fixture_repo import SCRIPTED_SOLUTION, make_fixture_repo

PY = sys.executable


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name).resolve()

    def tearDown(self):
        self._td.cleanup()


class EditorTests(TmpCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "m.py").write_text("def f():\n    return 1\n\ndef g():\n    return 1\n")
        self.ed = Editor(self.tmp)

    def test_unique_match_applies(self):
        out, paths = self.ed.edit("m.py", [{"old_text": "def f():\n    return 1", "new_text": "def f():\n    return 2"}])
        self.assertIn("return 2", (self.tmp / "m.py").read_text())
        self.assertEqual(paths, ["m.py"])
        self.assertIn("+    return 2", out)

    def test_ambiguous_match_rejected(self):
        with self.assertRaises(ToolError) as cm:
            self.ed.edit("m.py", [{"old_text": "    return 1", "new_text": "    return 3"}])
        self.assertEqual(cm.exception.code, "AMBIGUOUS_MATCH")

    def test_no_match_gives_hint_and_no_write(self):
        before = (self.tmp / "m.py").read_text()
        with self.assertRaises(ToolError) as cm:
            self.ed.edit("m.py", [{"old_text": "def f():\n  return 1", "new_text": "x"}])
        self.assertEqual(cm.exception.code, "NO_MATCH")
        self.assertIn("Closest region", cm.exception.message)
        self.assertEqual(before, (self.tmp / "m.py").read_text())

    def test_syntax_gate_reverts(self):
        before = (self.tmp / "m.py").read_text()
        with self.assertRaises(ToolError) as cm:
            self.ed.edit("m.py", [{"old_text": "def f():\n    return 1", "new_text": "def f(:\n    return 1"}])
        self.assertEqual(cm.exception.code, "SYNTAX_GATE")
        self.assertEqual(before, (self.tmp / "m.py").read_text())

    def test_transaction_all_or_nothing(self):
        before = (self.tmp / "m.py").read_text()
        with self.assertRaises(ToolError):
            self.ed.edit("m.py", [{"old_text": "def f():", "new_text": "def f2():"}, {"old_text": "nope", "new_text": "x"}])
        self.assertEqual(before, (self.tmp / "m.py").read_text())

    def test_crlf_preserved(self):
        (self.tmp / "w.py").write_bytes(b"a = 1\r\nb = 2\r\n")
        self.ed.edit("w.py", [{"old_text": "a = 1\nb = 2", "new_text": "a = 1\nb = 3"}])
        self.assertEqual((self.tmp / "w.py").read_bytes(), b"a = 1\r\nb = 3\r\n")

    def test_outside_workspace_rejected(self):
        with self.assertRaises(ToolError) as cm:
            self.ed.write("../evil.py", "x = 1\n")
        self.assertEqual(cm.exception.code, "OUTSIDE_WORKSPACE")

    def test_view_window(self):
        (self.tmp / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(300)) + "\n")
        out, meta = view_lines(self.tmp, "big.py", 10, None, 100)
        self.assertEqual(meta["range"], (10, 109))
        self.assertIn("lines 110-300 below not shown", out)


class WorkspaceTests(TmpCase):
    def setUp(self):
        super().setUp()
        self.repo, _ = make_fixture_repo(self.tmp / "repo")

    def test_dirty_initial_state_is_baseline(self):
        (self.repo / "README.md").write_text("locally modified before the run\n")
        ws = Workspace(self.repo, self.tmp / "state")
        ws.init()
        self.assertTrue(ws.initially_dirty)
        self.assertEqual(ws.changed(), [])  # pre-existing change is NOT part of our patch
        (self.repo / "durations" / "new.py").write_text("X = 1\n")
        (self.repo / ".harness_scratch" / "repro.py").write_text("print(1)\n")
        self.assertEqual(ws.changed(), [("A", "durations/new.py")])  # scratch excluded
        self.assertIn("durations/new.py", ws.diff())

    def test_restore_and_base_swap(self):
        ws = Workspace(self.repo, self.tmp / "state")
        ws.init()
        core = self.repo / "durations" / "core.py"
        orig = core.read_text()
        core.write_text(orig.replace("total = int", "total += int"))
        (self.repo / "tests" / "test_new.py").write_text("# new test\n")
        (self.repo / "durations" / "extra.py").write_text("Y = 2\n")
        cand = ws.tree()
        with ws.base_swap(keep_paths=["tests/test_new.py"]):
            self.assertEqual(core.read_text(), orig)
            self.assertFalse((self.repo / "durations" / "extra.py").exists())
            self.assertTrue((self.repo / "tests" / "test_new.py").exists())
        self.assertEqual(ws.tree(), cand)
        ws.restore(ws.base_tree)
        self.assertEqual(core.read_text(), orig)
        ws.restore(cand)
        self.assertIn("total += int", core.read_text())

    def test_non_git_repo_supported(self):
        repo, _ = make_fixture_repo(self.tmp / "plain", git=False)
        ws = Workspace(repo, self.tmp / "state2")
        ws.init()
        (repo / "durations" / "core.py").write_text("X = 1\n")
        self.assertEqual(ws.changed(), [("M", "durations/core.py")])


class TestParsing(unittest.TestCase):
    def test_pytest(self):
        out = ("tests/test_a.py::test_one PASSED [ 50%]\ntests/test_a.py::test_two FAILED [100%]\n"
               "=== short test summary info ===\nPASSED tests/test_a.py::test_one\nFAILED tests/test_a.py::test_two - assert 1 == 2\n"
               "ERROR tests/test_b.py - ImportError\n=== 1 failed, 1 passed in 0.1s ===")
        o, parser, coll = parse_output("python -m pytest -rA", out, "", 1)
        self.assertEqual(parser, "pytest")
        self.assertEqual(o["tests/test_a.py::test_one"], "PASS")
        self.assertEqual(o["tests/test_a.py::test_two"], "FAIL")
        self.assertEqual(o["tests/test_b.py"], "ERROR")
        self.assertTrue(coll)

    def test_unittest_old_and_new_formats(self):
        err = ("test_a (pkg.tests.T) ... ok\ntest_b (pkg.tests.T.test_b) ... FAIL\ntest_c (pkg.tests.T)\nDoc line ... skipped 'x'\n"
               "test_d (pkg.tests.T) ... expected failure\n")
        o, parser, _ = parse_output("python -m unittest -v", "", err, 1)
        self.assertEqual(parser, "unittest")
        self.assertEqual(o, {"pkg.tests.T.test_a": "PASS", "pkg.tests.T.test_b": "FAIL", "pkg.tests.T.test_c": "SKIP", "pkg.tests.T.test_d": "PASS"})

    def test_go_and_cargo(self):
        o, p, _ = parse_output("go test -v ./...", "=== RUN TestX\n--- PASS: TestX (0.00s)\n--- FAIL: TestY (0.01s)\nFAIL\n", "", 1)
        self.assertEqual((p, o), ("go", {"TestX": "PASS", "TestY": "FAIL"}))
        o, p, _ = parse_output("cargo test", "test a::b ... ok\ntest a::c ... FAILED\ntest a::d ... ignored\n", "", 101)
        self.assertEqual((p, o), ("cargo", {"a::b": "PASS", "a::c": "FAIL", "a::d": "SKIP"}))

    def test_transitions_identity_based(self):
        base = {"a": "FAIL", "b": "PASS", "c": "FAIL", "d": "PASS"}
        cand = {"a": "PASS", "b": "FAIL", "c": "SKIP", "e": "PASS"}
        t = classify_transitions(base, cand)
        self.assertEqual(t["fixed"], ["a"])
        self.assertEqual(t["regressed"], ["b"])
        self.assertEqual(t["to_skip"], ["c"])  # FAIL -> SKIP is NOT a fix
        self.assertEqual(t["disappeared"], ["d"])
        self.assertEqual(t["new_pass"], ["e"])


class VerifierTests(TmpCase):
    def _setup(self, fixed_code: str):
        repo, _ = make_fixture_repo(self.tmp / "repo")
        ws = Workspace(repo, self.tmp / "state")
        ws.init()
        logs = LogStore(self.tmp / "state" / "logs")
        runner = CommandRunner(repo, logs, 4000, 4000, 60)
        fw = TestFramework("unittest", f"{PY} -m unittest")
        ex = TestExecutor(ws, runner, fw, 60)
        ledger = Ledger(self.tmp / "ledger.jsonl")
        v = Verifier(ws, ex, ledger, {"flaky_reruns": 1})
        core = repo / "durations" / "core.py"
        core.write_text(core.read_text().replace("total = int(value) * _UNITS[unit]", fixed_code))
        test = repo / "tests" / "test_durations.py"
        test.write_text(test.read_text().replace(
            "    def test_empty_raises(self):",
            "    def test_compound(self):\n        self.assertEqual(parse_duration('1h30m'), 5400)\n\n    def test_empty_raises(self):"))
        return repo, ws, v, ledger

    def test_verified_fix_and_stale_after_edit(self):
        repo, ws, v, ledger = self._setup("total += int(value) * _UNITS[unit]")
        rep = v.verify([TestSpec(f"{PY} -m unittest -v tests.test_durations", "target")])
        self.assertEqual(rep.status, VERIFIED, rep.summary())
        self.assertIn("tests.test_durations.TestParse.test_compound", rep.fixed_tests)
        # Any edit after a green run makes that evidence stale for the new candidate.
        (repo / "durations" / "core.py").write_text((repo / "durations" / "core.py").read_text() + "\n# touch\n")
        self.assertEqual(ledger.status_of("issue_behavior", ws.tree()), STALE)

    def test_regression_fails(self):
        _, _, v, _ = self._setup("total += int(value) * _UNITS[unit] * (2 if unit == 'h' else 1) if False else int(value) * _UNITS[unit] + total + (1 if unit == 's' else 0)")
        rep = v.verify([TestSpec(f"{PY} -m unittest -v tests.test_durations", "target")])
        self.assertEqual(rep.status, FAILED)
        self.assertTrue(rep.regressions)

    def test_no_evidence_is_inconclusive(self):
        _, _, v, _ = self._setup("total += int(value) * _UNITS[unit]")
        rep = v.verify([])
        self.assertEqual(rep.status, INCONCLUSIVE)

    def test_timeout_is_not_success(self):
        _, _, v, _ = self._setup("total += int(value) * _UNITS[unit]")
        v.ex.timeout = 1
        rep = v.verify([TestSpec(f"{PY} -c 'import time; time.sleep(5)'", "target")])
        self.assertNotEqual(rep.status, VERIFIED)
        self.assertTrue(any("timed out" in s for s in rep.skipped))


class LoopGuardTests(unittest.TestCase):
    def test_repeat_and_errors_and_pingpong(self):
        g = LoopGuard({})
        a = action_fp("read_file", {"path": "x"})
        o = obs_fp("same")
        sigs = [g.record(a, o, False) for _ in range(4)]
        self.assertEqual(sigs[-1].kind, "repeat")
        g.reset()
        sigs = [g.record(a, obs_fp(f"err {i}"), True) for i in range(3)]
        self.assertEqual(sigs[-1].kind, "repeat_error")
        g.reset()
        b = action_fp("read_file", {"path": "y"})
        sigs = [g.record(a if i % 2 == 0 else b, obs_fp(str(i)), False) for i in range(6)]
        self.assertEqual(sigs[-1].kind, "ping_pong")
        g.reset()
        self.assertIsNone(g.record_no_action())
        self.assertIsNone(g.record_no_action())
        self.assertEqual(g.record_no_action().kind, "no_action")

    def test_oscillation(self):
        g = LoopGuard({})
        seq = ["A", "B", "A", "B", "A"]
        res = [g.record_tree(t) for t in seq]
        self.assertEqual(res[-1].kind, "oscillation")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.specs = {s["name"]: s for s in tool_specs(["fault-localization"])}

    def test_edit_pairs_and_raw_code(self):
        text = ('THOUGHT: fix\n<tool name="edit_file">\n<path>a.py</path>\n<old_text>\n    x = "<b>"\n</old_text>\n'
                '<new_text>\n    x = "<i>"\n</new_text>\n<old_text>\ny=1\n</old_text>\n<new_text>\ny=2\n</new_text>\n</tool>')
        calls, thought = parse_text_actions(text, self.specs, 5)
        self.assertEqual(thought, "THOUGHT: fix")
        self.assertEqual(calls[0].args["edits"][0], {"old_text": '    x = "<b>"', "new_text": '    x = "<i>"'})
        self.assertEqual(len(calls[0].args["edits"]), 2)
        self.assertIsNone(validate_args(self.specs["edit_file"], calls[0].args))

    def test_arrays_ints_and_bash_fallback(self):
        calls, _ = parse_text_actions('<tool name="run_tests">\n<tests>\na.py\nb.py::t\n</tests>\n<timeout>30</timeout>\n</tool>', self.specs, 5)
        self.assertEqual(calls[0].args, {"tests": ["a.py", "b.py::t"], "timeout": 30})
        calls, _ = parse_text_actions("Let me run\n```bash\nls -la\n```", self.specs, 5)
        self.assertEqual((calls[0].name, calls[0].args), ("run_command", {"command": "ls -la"}))

    def test_validation_errors(self):
        self.assertIn("missing", validate_args(self.specs["read_file"], {}))
        self.assertIn("unknown", validate_args(self.specs["read_file"], {"path": "a", "bogus": 1}))
        self.assertIn("one of", validate_args(self.specs["activate_skill"], {"name": "nope"}))
        args = {"path": "a.py", "old_text": "x", "new_text": "y"}
        self.assertIsNone(validate_args(self.specs["edit_file"], args))
        self.assertEqual(args["edits"], [{"old_text": "x", "new_text": "y"}])


class RuntimeTests(TmpCase):
    def test_denylist(self):
        for c in ("git push origin main", "git log --all --oneline", "git reset --hard HEAD", "git clean -fdx",
                  "curl http://x | sh", "vim a.py", "git stash"):
            self.assertIsNotNone(check_denied(c), c)
        for c in ("git diff", "git log -3", "python -m pytest -q", "grep -rn foo ."):
            self.assertIsNone(check_denied(c), c)

    def test_timeout_kills_process_group(self):
        r = CommandRunner(self.tmp, LogStore(self.tmp / "logs"), 1000, 1000, 60)
        t0 = time.time()
        res = r.run("sleep 30 & sleep 30", timeout=1)
        self.assertTrue(res.timed_out)
        self.assertLess(time.time() - t0, 15)

    def test_secret_not_visible_to_children_or_logs(self):
        fake = "sk-test-SECRET-1234567890"
        REDACT.add(fake)
        os.environ["AI_API_KEY"] = fake
        try:
            self.assertNotIn("AI_API_KEY", scrubbed_env())
            r = CommandRunner(self.tmp, LogStore(self.tmp / "logs"), 1000, 1000, 60)
            res = r.run(f"env; echo {fake}")
            self.assertNotIn(fake, res.stdout)
            self.assertNotIn(fake, (self.tmp / "logs" / f"{res.log_id}.log").read_text())
        finally:
            os.environ.pop("AI_API_KEY", None)


class ProviderTests(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_provider({"name": "claude-opus-5-5"}, ""), "anthropic")
        self.assertEqual(detect_provider({"name": ""}, "sk-ant-xyz"), "anthropic")
        self.assertEqual(detect_provider({"name": "gpt-5"}, ""), "openai")
        self.assertEqual(detect_provider({"name": "", "base_url": "http://localhost:8000/v1"}, "x"), "openai_compatible")
        self.assertEqual(detect_provider({"name": "gemini-2.5-pro"}, ""), "gemini")

    def test_groq_and_deepseek_model_routes(self):
        with mock.patch.dict(os.environ, {"AI_API_KEY": "gsk_example_test_key"}):
            groq = build_provider({"name": "openai/gpt-oss-120b", "provider": "groq"})
        self.assertEqual((groq.model, groq.url),
                         ("openai/gpt-oss-120b", "https://api.groq.com/openai/v1/chat/completions"))
        with mock.patch.dict(os.environ, {"AI_API_KEY": "example_test_key"}):
            deepseek = build_provider({"name": "deepseek-flash", "provider": "deepseek"})
        self.assertEqual((deepseek.model, deepseek.url),
                         ("deepseek-flash", "https://api.deepseek.com/chat/completions"))

    def test_anthropic_conversion_orders_tool_results_first(self):
        p = AnthropicProvider("claude-opus-5-5", {}, "k")
        self.assertFalse(p.send_temperature)
        msgs = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "read_file", "args": {"path": "x"}},
                                                               {"id": "b", "name": "read_file", "args": {"path": "y"}}]},
            {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "A"},
            {"role": "tool", "tool_call_id": "b", "name": "read_file", "content": "B"},
            {"role": "user", "content": "footer"},
        ]
        wire = p._convert(msgs)
        self.assertEqual([m["role"] for m in wire], ["user", "assistant", "user"])
        self.assertEqual([b["type"] for b in wire[2]["content"]], ["tool_result", "tool_result", "text"])

    def test_openai_conversion(self):
        p = OpenAICompatProvider("gpt-5", {"max_output_tokens": 100}, "k")
        self.assertNotIn("temperature", p.params)
        self.assertIn("max_completion_tokens", p.params)
        wire = p._convert("sys", [{"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "args": {"x": 1}}]},
                                  {"role": "tool", "tool_call_id": "a", "content": "r"}])
        self.assertEqual(wire[1]["tool_calls"][0]["function"]["arguments"], '{"x": 1}')
        self.assertEqual(wire[2], {"role": "tool", "tool_call_id": "a", "content": "r"})


class AnchorTests(unittest.TestCase):
    def test_traceback_and_symbols(self):
        issue = ('Crash:\n```\nTraceback (most recent call last):\n  File "/usr/lib/python3/site-packages/pkg/core.py", line 42, in parse\n'
                 '    return helper(x)\nValueError: invalid literal for int()\n```\nCalling `pkg.core.parse("1h")` fails.')
        a = extract_anchors(issue)
        self.assertEqual(a.frames[0].path, "/usr/lib/python3/site-packages/pkg/core.py")
        self.assertIn("ValueError", a.exceptions)
        self.assertIn("parse", a.symbols)


class ConfigTests(unittest.TestCase):
    def test_env_overrides_and_no_key(self):
        os.environ["AI_MODEL"] = "some-model"
        os.environ["HARNESS__BUDGET__MAX_STEPS"] = "7"
        try:
            cfg = load_config()
            self.assertEqual(cfg.model["name"], "some-model")
            self.assertEqual(cfg.budget["max_steps"], 7)
            self.assertNotIn("sk-", json.dumps(cfg.redacted()))
        finally:
            os.environ.pop("AI_MODEL")
            os.environ.pop("HARNESS__BUDGET__MAX_STEPS")


class EndToEndTests(TmpCase):
    def _run(self, script, overrides=None):
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry

        repo, issue = make_fixture_repo(self.tmp / "repo")
        cfg = load_config(None, {"run": {"runs_dir": str(self.tmp / "runs")}, **(overrides or {})})
        run_dir = cfg.runs_dir() / "r"
        tel = Telemetry(run_dir, verbose=False)
        res = Controller(cfg, repo, issue, run_dir, ScriptedProvider(list(script)), tel).run()
        tel.close()
        return res, repo, run_dir

    def test_scripted_fix_is_verified_and_applied(self):
        fake = "sk-e2e-SECRET-0987654321"
        REDACT.add(fake)
        res, repo, run_dir = self._run(SCRIPTED_SOLUTION)
        self.assertEqual(res.status, "VERIFIED", res.summary)
        self.assertIn("total += int", (repo / "durations" / "core.py").read_text())
        self.assertFalse((repo / ".harness_scratch").exists())  # scratch removed from the repo
        self.assertNotIn(".harness_scratch", res.patch)
        result = json.loads((run_dir / "result.json").read_text())
        self.assertEqual(result["status"], "VERIFIED")
        for p in run_dir.rglob("*"):
            if p.is_file() and p.suffix in (".json", ".jsonl", ".log", ".md", ".diff"):
                self.assertNotIn(fake, p.read_text(errors="replace"), p)

    def test_wrong_fix_is_not_verified(self):
        wrong = [
            '<tool name="edit_file">\n<path>durations/core.py</path>\n<old_text>\n    total = 0\n</old_text>\n<new_text>\n    total = 1\n</new_text>\n</tool>',
            '<tool name="submit">\n<summary>wrong</summary>\n<test_commands>\npython -m unittest -v tests.test_durations\n</test_commands>\n</tool>',
        ]
        res, _, _ = self._run(wrong, {"budget": {"max_attempts": 1}})
        self.assertNotEqual(res.status, "VERIFIED")

    def test_native_tool_mode_with_wire_conversion(self):
        """Native tool-calling path; every request must convert to valid Anthropic and OpenAI wire formats."""
        from harness.controller.controller import Controller
        from harness.provider.scripted import ScriptedProvider
        from harness.telemetry import Telemetry

        class CheckingProvider(ScriptedProvider):
            def chat(self, system, messages, tools):
                a = AnthropicProvider("claude-sonnet-5", {}, "k")._convert(messages)
                roles = [m["role"] for m in a]
                assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles
                pending = set()
                for m in a:
                    for b in m["content"]:
                        if b.get("type") == "tool_use":
                            pending.add(b["id"])
                        if b.get("type") == "tool_result":
                            pending.discard(b["tool_use_id"])
                assert not pending, f"tool_use without result: {pending}"
                OpenAICompatProvider("gpt-5", {}, "k")._convert(system, messages)
                return super().chat(system, messages, tools)

        script = [
            {"text": "look", "tool_calls": [{"name": "search_code", "args": {"query": "parse_duration", "mode": "symbol"}},
                                            {"name": "read_file", "args": {"path": "durations/core.py"}}]},
            {"tool_calls": [{"name": "edit_file", "args": {"path": "durations/core.py", "edits": [
                {"old_text": "        total = int(value) * _UNITS[unit]", "new_text": "        total += int(value) * _UNITS[unit]"}]}},
                {"name": "write_file", "args": {"path": ".harness_scratch/repro.py", "content":
                    "from durations import parse_duration\nassert parse_duration('1h30m') == 5400\n"}}]},
            {"tool_calls": [{"name": "submit", "args": {"summary": "sum components", "reproducer": "python .harness_scratch/repro.py"}},
                            {"name": "read_file", "args": {"path": "README.md"}}]},
        ]
        repo, issue = make_fixture_repo(self.tmp / "repo")
        cfg = load_config(None, {"run": {"runs_dir": str(self.tmp / "runs")}})
        tel = Telemetry(cfg.runs_dir() / "n", verbose=False)
        res = Controller(cfg, repo, issue, cfg.runs_dir() / "n", CheckingProvider(script, native=True), tel).run()
        tel.close()
        self.assertEqual(res.status, "VERIFIED", res.summary)
        self.assertEqual(res.metrics["tool_mode"], "native")

    def test_looping_agent_is_stopped(self):
        loop = ['<tool name="read_file">\n<path>README.md</path>\n</tool>'] * 60
        res, _, run_dir = self._run(loop, {"budget": {"max_attempts": 1, "max_steps": 40}})
        self.assertEqual(res.status, "FAILED")  # no patch at all
        trace = (run_dir / "trace.jsonl").read_text()
        self.assertIn('"event": "stuck"', trace)
        self.assertLess(res.metrics["model_calls"], 40)


if __name__ == "__main__":
    unittest.main()
