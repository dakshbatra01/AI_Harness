"""Adaptive Evidence-Gated Controller.

deterministic outer state machine (ORIENT -> LOCATE -> REPRODUCE -> EDIT -> CHECK -> FINAL_VERIFY -> DONE,
with RECOVER back-edges) + one adaptive model-driven action loop per attempt (L4) + edit->check backpressure
(L3) + verification-driven phase loop (L2) + bounded fresh-context attempts (L1). Only the verifier may
recommend VERIFIED; budgets always keep a verification reserve."""
from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from harness.controller import prompts
from harness.controller.loopguard import LoopGuard, StuckSignal, action_fp, obs_fp
from harness.controller.protocol import (
    parse_text_actions,
    render_call_as_text,
    text_protocol_doc,
    tool_specs,
    validate_args,
)
from harness.controller.state import (
    CHECK, DIRECT, EDIT, FINAL_VERIFY, LIGHT_PLAN, LOCATE, RECOVER, REPRODUCE, STRUCTURED,
    Budget, FailureRecord, TaskContract, TaskState,
)
from harness.memory.store import MemoryStore
from harness.provider.base import ContextOverflow, ModelTurn, Provider, ProviderError, ToolCall, ToolsUnsupported
from harness.repo.anchors import extract_anchors
from harness.repo.codemap import CodeMap
from harness.repo.localize import localize
from harness.repo.manifest import build_manifest, is_test_path, read_guidance
from harness.skills import catalog, load_skills, render
from harness.telemetry import Telemetry
from harness.tools.editor import Editor
from harness.tools.files import ToolError, resolve_path, skeleton, view_lines
from harness.tools.runtime import CommandRunner, LogStore, format_cmd_result
from harness.tools.search import Searcher
from harness.util import REDACT, estimate_tokens, head_tail, new_id, normalize_signature, short_hash
from harness.verify.ledger import SUPPORTED, UNKNOWN, Ledger
from harness.verify.testrun import (
    build_command, classify_transitions, detect_framework, is_test_command, normalize_command, normalize_test_id,
    related_test_files,
)
from harness.verify.verifier import (
    FAILED, INCONCLUSIVE, VERIFIED, TestExecutor, TestSpec, Verifier, VerificationReport, issue_literals,
)
from harness.workspace import SCRATCH_DIR, Workspace

NON_EVIDENCE_ENDINGS = {"context_overflow", "attempt_step_cap", "stuck"}
READ_ONLY_TOOLS = {"search_code", "read_file", "read_log", "activate_skill", "update_plan"}
MULTI_COMPONENT_RE = re.compile(
    r"\b(across (the|all)|throughout|public api|deprecat\w*|renam\w*|refactor\w*|migrat\w*|backward.compat\w*|"
    r"propagat\w*|everywhere|all (callers|backends|subclasses|modules))\b",
    re.I,
)


@dataclass
class RunResult:
    status: str
    patch: str
    report: VerificationReport | None
    run_dir: Path
    metrics: dict = field(default_factory=dict)
    summary: str = ""


class Controller:
    def __init__(self, cfg, repo_root: Path, issue: str, run_dir: Path, provider: Provider, tel: Telemetry,
                 target_tests: list[str] | None = None) -> None:
        self.cfg = cfg
        self.target_tests = [t.strip() for t in (target_tests or []) if t.strip()]
        self.root = Path(repo_root).resolve()
        self.issue = issue
        self.run_dir = run_dir
        self.provider = provider
        self.tel = tel
        self.task_id = run_dir.name
        tcfg = cfg.tools
        self.ws = Workspace(self.root, run_dir)
        self.logs = LogStore(run_dir / "logs")
        self.runner = CommandRunner(self.root, self.logs, int(tcfg["obs_head_chars"]), int(tcfg["obs_tail_chars"]), int(tcfg["cmd_timeout_s"]))
        self.searcher = Searcher(self.root, self.ws)
        self.editor = Editor(self.root)
        self.memory = MemoryStore(run_dir / "memory.sqlite")
        self.ledger = Ledger(run_dir / "ledger.jsonl")
        self.skills = load_skills()
        self.profile = str(cfg.tools.get("profile", "full")).lower()
        self.specs = tool_specs(list(self.skills))
        if self.profile == "bash":  # minimal interface (shell + submit + bookkeeping) behind the same verifier
            self.specs = [s for s in self.specs if s["name"] in ("run_command", "submit", "update_plan", "repo_changes")]
            self.skills = {}
        self.spec_by_name = {s["name"]: s for s in self.specs}
        self.tok_fixed = 0  # estimated tokens: system prompt + tool schemas, summed over calls
        self.tok_history = 0  # estimated tokens: conversation (task, observations), summed over calls
        self._tools_tok: int | None = None
        self._red_check: tuple | None = None  # (kind, command) of the last check that showed the problem
        self._check_cache: dict = {}  # (kind, command, tree, scratch) -> (step, short result)
        self._single_reads = 0
        self._req_hint_given = False
        self._batch_tip_given = False
        self.reviewed_tree = ""  # patch version the model has already looked at in full
        self.pending_submit: dict = {}  # evidence arguments remembered across the self-review round-trip
        self.pending_tree = ""
        b = cfg.budget
        self.budget = Budget(int(b["max_steps"]), int(b["max_model_calls"]), int(b["max_total_tokens"]), float(b["max_wall_s"]), float(b["verification_reserve"]))
        self.state = TaskState()
        self.guard = LoopGuard(cfg.loop)
        mode = str(cfg.model.get("tool_mode", "auto")).lower()
        self.native = mode in ("auto", "native") and getattr(provider, "supports_native_tools", True)
        mask = str(cfg.model.get("mask_observations", "auto")).lower()
        # Anthropic's newest models require append-only history (thinking blocks), and have 1M context.
        # Off where history must stay append-only (Anthropic thinking blocks) or where the provider's prefix cache
        # makes re-sent history far cheaper than rewriting it (DeepSeek: cache hits ~50x cheaper than misses).
        self.masking = mask == "on" or (mask == "auto" and getattr(provider, "name", "") not in ("anthropic", "deepseek", "scripted"))
        self.aggressive_mask = False
        self.messages: list[dict] = []
        self.obs_count = 0
        self.seen_calls: dict[str, tuple[int, int]] = {}
        self.activated_skills: set[str] = set()
        self.native_arg_errors = 0
        self.tree = ""
        self.executor: TestExecutor | None = None
        self.verifier: Verifier | None = None
        self.framework = None
        self.manifest = None
        self.contract: TaskContract | None = None
        self.orientation = ""
        self.test_info = ""
        self.prev_report_key = ""

    # ================================================================================= setup
    def orient(self) -> None:
        t0 = time.time()
        self.ws.init()
        self.tree = self.ws.base_tree
        files = self.searcher.list_files()
        self.manifest = build_manifest(self.root, files)
        anchors = extract_anchors(self.issue)
        self.contract = TaskContract(self.task_id, self.issue, str(self.root), self.ws.real_git_head or self.ws.base_commit, anchors)
        loc = localize(anchors, self.manifest, self.searcher)
        self._loc = loc
        self.searcher.set_prior(loc.prior())
        self.framework = detect_framework(self.root, files)
        self.executor = TestExecutor(self.ws, self.runner, self.framework, int(self.cfg.tools["test_timeout_s"]), self.tel)
        self.verifier = Verifier(self.ws, self.executor, self.ledger, self.cfg.verify, self.tel, self.editor.authored)
        self.verifier.issue_literals = issue_literals(self.issue)
        self.state.route = self._route(anchors, loc)
        self.state.set_phase(LOCATE)
        if self.framework.base_cmd:
            self.memory.add_fact(f"detected test framework {self.framework.name}: `{self.framework.base_cmd}`", "orient", "repo")

        self.codemap = self._build_codemap(files)
        parts = [f"Repository: {self.root.name}", self.manifest.summary(), "", "Layout:",
                 self.manifest.tree(40 if self.codemap else 60)]
        if self.ws.initially_dirty:
            parts.append("\nNote: the repository had uncommitted changes before this run; they are part of the baseline.")
        self.guidance = read_guidance(self.root)
        if self.guidance.sources:
            parts += ["", self.guidance.render()]
            self.memory.add_fact(f"repository guidance files: {', '.join(self.guidance.sources)}", "orient", "repo")
        parts += ["", "Issue anchors (extracted automatically):", anchors.brief(), "", "Candidate locations (lexical prior):", loc.render(8)]
        code_map = self._render_codemap(anchors, loc)
        self._codemap_text = code_map
        if code_map:
            parts += ["", "Code map (issue-ranked outline, signatures only - read a file for bodies):", code_map]
        elif loc.sources and loc.confidence in ("high", "medium"):
            top = loc.sources[0].path
            try:
                sk = skeleton(self.root, top, max_lines=60)
                parts += ["", f"Outline of top candidate {top}:", sk]
            except ToolError:
                pass
        self.orientation = "\n".join(parts)
        example_targets = [c.path for c in loc.tests[:1]] or self.manifest.test_files[:1]
        ex = build_command(self.framework, example_targets) if self.framework.base_cmd and example_targets else ""
        self.test_info = (
            f"Detected test framework: {self.framework.name} ({self.framework.note}).\n"
            + (f"Example: run_tests(tests=[{example_targets[0]!r}])  ->  `{ex}`\n" if ex else "")
            + f"Test files in repo: {len(self.manifest.test_files)}. Scratch directory for reproducers: {SCRATCH_DIR}/ "
            "(excluded from the patch). The environment is already set up; avoid installing packages unless a check "
            "cannot run otherwise."
        )
        if self.target_tests:
            self.test_info += (
                "\n\nTEST CASE - these must pass after your change (the harness checks them on the original and patched "
                "code): " + ", ".join(self.target_tests) + "\nRun them first to see how they fail. If one does not exist yet, "
                "create it as the issue describes."
            )
        if self.profile == "bash":
            self.test_info += ("\n\nTool profile: shell only. Inspect with grep/sed -n/cat, edit with python or sed "
                               "scripts, and run tests with run_command.")
        self.tel.event(
            "orient", seconds=round(time.time() - t0, 2), route=self.state.route, framework=self.framework.name,
            files=len(files), localization_confidence=loc.confidence, candidates=[(c.path, round(c.score, 1)) for c in loc.sources[:8]],
            anchors=anchors.to_dict(), base_tree=self.ws.base_tree, real_head=self.ws.real_git_head,
        )
        self.tel.say(
            f"orient: {len(files)} files, {self.manifest.primary_language}, tests={self.framework.name}, route={self.state.route}, "
            f"top candidates: {', '.join(c.path for c in loc.sources[:3]) or '-'}"
        )

    def _build_codemap(self, files: list[str]) -> CodeMap | None:
        ccfg = self.cfg.data.get("context", {})
        mode = str(ccfg.get("codemap", "auto")).lower()
        if mode == "off" or (mode == "auto" and len(self.manifest.source_files) < int(ccfg.get("codemap_min_files", 15))):
            return None  # small repos: the layout + candidates already show everything
        try:
            cm = CodeMap(self.root, files, time_budget_s=float(ccfg.get("codemap_time_s", 8)),
                         max_files=int(ccfg.get("codemap_max_files", 6000)))
        except Exception as e:  # an optimisation must never cost the run
            self.tel.event("codemap_error", error=repr(e))
            return None
        self.tel.event("codemap", files=len(cm.files), seconds=round(cm.build_seconds, 2), complete=cm.complete)
        return cm if cm.files else None

    def _render_codemap(self, anchors, loc) -> str:
        if not self.codemap:
            return ""
        seeds = {c.path: c.score for c in loc.sources[:6] if c.score > 0}
        terms = {s.split(".")[-1] for s in anchors.symbols}
        budget = int(self.cfg.data.get("context", {}).get("codemap_tokens", 1200))
        if not seeds:  # no evidence: a small overview of the most central files only
            budget = min(budget, 600)
        try:
            return self.codemap.render(seeds, terms, budget)
        except Exception as e:
            self.tel.event("codemap_error", error=repr(e))
            return ""

    def _needs_requirement_list(self) -> bool:
        """Issues that describe more than one behaviour are where partial fixes happen: ask the model to list
        each required behaviour and check every one (requirement-level reproduction). One-line bugs skip it."""
        mode = str(self.cfg.verify.get("requirement_checks", "auto")).lower()
        if mode in ("on", "always"):
            return True
        if mode != "auto" or (self.target_tests and self.issue.startswith("Make the following failing test case")):
            return False
        text = self.issue
        bullets = len(re.findall(r"^\s*(?:[-*\u2022]|\d+[.)])\s+\S", text, re.M))
        cues = len(re.findall(r"\b(should|must|expected|also|as well|same happens|instead of|otherwise)\b", text, re.I))
        return bullets >= 2 or cues >= 3 or len(text) >= 500

    def _task_instructions(self) -> list:
        out = []
        if self._needs_requirement_list():
            out.append(
                "This issue may state several behaviours. Before editing, list each required behaviour (at most "
                f"{int(self.cfg.verify.get('max_requirements', 4))}, one line each) with update_plan(acceptance=...), and make "
                "your reproducer check EVERY one of them (one assertion per behaviour). A fix that satisfies only some of "
                "them is a partial fix and will be caught by hidden tests."
            )
        loc = getattr(self, "_loc", None)
        if self.framework and self.framework.base_cmd and loc is not None and loc.tests and not self.target_tests:
            out.append(f"Tip: running the most related existing tests early (e.g. run_tests(tests=[{loc.tests[0].path!r}])) "
                       "often pinpoints the faulty code faster than reading.")
        return out

    def _route(self, anchors, loc) -> str:
        score = {"low": 2, "medium": 1, "high": 0}[loc.confidence]
        if len(self.issue) > 3000:
            score += 1
        if len({p for p, _ in anchors.paths}) >= 3:
            score += 1
        if len({m.group(0).lower() for m in MULTI_COMPONENT_RE.finditer(self.issue)}) >= 2:
            score += 1
        if anchors.frames or anchors.tests:
            score -= 1
        return DIRECT if score <= 1 else LIGHT_PLAN if score <= 3 else STRUCTURED

    # ================================================================================= run
    def run(self) -> RunResult:
        max_attempts = max(1, int(self.cfg.budget["max_attempts"]))
        end_reason = ""
        try:
            self.orient()
            while True:
                end_reason = self._attempt()
                self.tel.event("attempt_end", attempt=self.state.attempt, reason=end_reason, budget=self.budget.to_dict())
                self.tel.say(f"attempt {self.state.attempt} ended: {end_reason}")
                if end_reason != "verified" and end_reason in NON_EVIDENCE_ENDINGS:
                    end_reason = self._verify_pending(end_reason)
                if end_reason == "verified":
                    break
                if self.state.attempt >= max_attempts or self.budget.work_exhausted() or self.budget.remaining_steps() < 12:
                    break
                self._prepare_next_attempt(end_reason)
        except ProviderError as e:
            end_reason = f"provider_error: {e}"
            self.tel.event("provider_error", error=str(e)[:500])
            self.tel.say(f"model provider error: {str(e)[:300]}")
        except Exception as e:  # never lose the run: always finalize (patch, result.json, cleanup)
            end_reason = f"internal_error: {type(e).__name__}: {e}"
            self.tel.event("internal_error", error=repr(e), tb=traceback.format_exc()[-3000:])
            self.tel.say(f"internal error: {e!r}; finalizing with the best available candidate")
        return self._finalize(end_reason)

    # ================================================================================= attempt loop
    def _verify_pending(self, end_reason: str) -> str:
        """An attempt stopped for a resource reason (context/steps), not because its approach was refuted.
        Its pending changes may already be right: verify them (no model calls) before anything else."""
        self.tree = self.ws.tree()
        if not self.ws.changed(None, self.tree) or any(c[0] == self.tree and c[2] is not None for c in self.state.candidates):
            return end_reason
        self.tel.say(f"attempt ended by {end_reason}: verifying the pending changes before deciding how to continue")
        rep = self.verifier.verify(self._test_plan(self.pending_submit), self._target_split()[1])
        self.state.candidates.append((rep.candidate_hash, self.ws.commit(rep.candidate_hash, f"cand{len(self.state.candidates) + 1}"), rep))
        self.state.last_report = rep
        self.tel.event("verification", round="pending", status=rep.status, report=rep.to_dict(), summary=f"auto ({end_reason})")
        self.tel.say(f"verify: {rep.status} (fixed={len(rep.fixed_tests)}, regressions={len(rep.regressions)})")
        return "verified" if rep.status == VERIFIED else end_reason

    def _attempt(self) -> str:
        st = self.state
        st.attempt_step = 0
        st.verify_rounds = 0
        self.guard.reset()
        self.seen_calls.clear()
        self.activated_skills.clear()
        self.obs_count = 0
        self.aggressive_mask = False
        prior = ""
        if st.failures:
            prior = prompts.previous_attempts_text(st.failures, st.plan.render(), self._start_note)
        self.messages = [{"role": "user", "content": prompts.initial_message(self.issue, self.orientation, st.route, self.test_info,
                                                                             prior, self._task_instructions())}]
        remaining_attempts = max(1, int(self.cfg.budget["max_attempts"]) - st.attempt + 1)
        work_steps = self.budget.remaining_steps()
        cap = work_steps if remaining_attempts == 1 else max(15, int(work_steps * 0.65))
        stuck_level = 0
        pending_nudge = ""
        while True:
            if self.budget.work_exhausted():
                return "budget_exhausted"
            if st.attempt_step >= cap:
                return "attempt_step_cap"
            try:
                turn = self._call_model()
            except ContextOverflow:
                if self.masking and not self.aggressive_mask:
                    self.aggressive_mask = True
                    continue
                return "context_overflow"
            except ToolsUnsupported:
                if not self.native:
                    raise
                self._switch_to_text_mode("provider rejected native tools")
                continue
            calls, thought = self._extract_calls(turn)
            if not calls and self._implicit_submit_ok(turn):
                # The model ended with a final answer in prose while there are changes: that IS a request to
                # finish. Route it through the normal submit path (self-review + verifier) instead of nudging,
                # which otherwise costs several re-read/re-test cycles per task.
                calls = [ToolCall(id=f"implicit_submit_{st.step}", name="submit", args={"summary": turn.text.strip()[:2000]})]
                self.tel.event("implicit_submit", step=st.step)
            self._append_assistant(turn, calls)
            if thought:
                self.tel.event("thought", step=st.step, text=thought[:2000])
            if not calls:
                sig = self.guard.record_no_action()
                msg = ("No tool call was found in your reply. Continue by calling a tool; when the change is made and "
                       "checked, call `submit` with your evidence.")
                if turn.truncated:
                    msg = ("Your reply was cut off by the output token limit before a complete tool call. Use smaller steps "
                           "(e.g. split large edits, fewer tool calls per reply).")
                elif not self.native:
                    msg += ' Use the <tool name="...">...</tool> format exactly.'
                self._append_user(msg)
                if sig:
                    stuck_level += 1
                    r = self._handle_stuck(sig, stuck_level)
                    if r:
                        return r
                continue

            limit = int(self.cfg.tools["max_actions_per_turn"])
            results: list[tuple[ToolCall, str, bool]] = []
            signals: list[StuckSignal] = []
            final: str | None = None
            for i, call in enumerate(calls):
                if i >= limit or final is not None:
                    results.append((call, "SKIPPED: not executed (too many actions in one reply or after submit).", True))
                    continue
                st.step += 1
                st.attempt_step += 1
                st.steps_in_phase += 1
                self.budget.steps += 1
                if call.name.rsplit(".", 1)[-1] == "submit":
                    call.name = "submit"
                    try:
                        out, final_reason = self._handle_submit(call)
                    except Exception as e:
                        self.tel.event("submit_exception", error=repr(e), tb=traceback.format_exc()[-3000:])
                        out, final_reason = f"ERROR INTERNAL during verification: {type(e).__name__}: {e}", None
                    results.append((call, out, False))
                    final = final_reason or ""
                    continue
                t0 = time.time()
                out, is_err = self._dispatch(call)
                self.tel.event(
                    "tool", step=st.step, phase=st.phase, tool=call.name, args=_brief_args(call.args), error=is_err,
                    seconds=round(time.time() - t0, 2), chars=len(out), **({"error_text": out[:600]} if is_err else {}),
                )
                self.tel.say(f"step {st.step:>3} [{st.phase:<9}] {call.name} {_brief_args(call.args, 90)}{'  -> ERROR' if is_err else ''}")
                if is_err:
                    st.tool_errors += 1
                sig = self.guard.record(action_fp(call.name, call.args), obs_fp(out, self.tree), is_err)
                if sig:
                    signals.append(sig)
                results.append((call, out, is_err))
            single_read = len(calls) == 1 and calls[0].name in ("read_file", "search_code")
            self._single_reads = self._single_reads + 1 if single_read else 0
            hint = pending_nudge or self._hint()
            pending_nudge = ""
            self._append_observations(results, hint)
            if final:
                return final
            for sig in signals:
                stuck_level += 1
                r = self._handle_stuck(sig, stuck_level)
                if r:
                    return r

    # ---------------------------------------------------------------------------- model io
    def _system(self) -> str:
        return prompts.system_prompt(None if self.native else text_protocol_doc(self.specs), catalog(self.skills))

    def _call_model(self) -> ModelTurn:
        msgs = self._compiled_messages()
        system = self._system()
        fixed = estimate_tokens(system) + (self._tools_tokens() if self.native else 0)
        history = sum(estimate_tokens(str(m.get("content", ""))) + estimate_tokens(json.dumps(m.get("tool_calls", []))) for m in msgs)
        est = fixed + history
        self.tok_fixed += fixed
        self.tok_history += history
        window = int(self.cfg.model["context_window"])
        if est > 0.7 * window and self.masking and not self.aggressive_mask:
            self.aggressive_mask = True
            msgs = self._compiled_messages()
        elif est > 0.85 * window:
            raise ContextOverflow(f"estimated {est} tokens exceeds 85% of context window")
        t0 = time.time()
        turn = self.provider.chat(system, msgs, self.specs if self.native else None)
        b = self.budget
        b.model_calls += 1
        b.tokens_in += turn.input_tokens
        b.tokens_out += turn.output_tokens
        b.tokens_cached += turn.cached_tokens
        self.tel.event(
            "model_call", n=b.model_calls, seconds=round(time.time() - t0, 2), tokens_in=turn.input_tokens,
            tokens_out=turn.output_tokens, cached=turn.cached_tokens, stop=turn.stop_reason, tool_calls=len(turn.tool_calls),
            est_context=est,
        )
        return turn

    def _implicit_submit_ok(self, turn: ModelTurn) -> bool:
        if not self.cfg.loop.get("implicit_submit", True) or turn.truncated or not (turn.text or "").strip():
            return False
        if not self.ws.changed(None, self.tree):
            return False
        last = self.state.last_report
        return not (last is not None and last.candidate_hash == self.tree)  # this exact patch was already judged

    def _tools_tokens(self) -> int:
        if self._tools_tok is None:
            self._tools_tok = estimate_tokens(json.dumps(self.specs))
        return self._tools_tok

    def _extract_calls(self, turn: ModelTurn) -> tuple[list[ToolCall], str]:
        limit = int(self.cfg.tools["max_actions_per_turn"]) + 2
        if self.native:
            if not turn.tool_calls and ("<function=" in turn.text or "<tool name=" in turn.text):
                return parse_text_actions(turn.text, self.spec_by_name, limit)  # tool call written as text
            return turn.tool_calls, turn.text
        return parse_text_actions(turn.text, self.spec_by_name, limit)

    def _switch_to_text_mode(self, why: str) -> None:
        self.tel.event("tool_mode_switch", to="text", reason=why)
        self.tel.say(f"switching to text tool protocol ({why})")
        self.native = False
        converted: list[dict] = []
        for m in self.messages:
            if m["role"] == "assistant":
                text = m.get("content") or ""
                for tc in m.get("tool_calls") or []:
                    text += "\n" + render_call_as_text(tc["name"], tc["args"])
                converted.append({"role": "assistant", "content": text.strip() or "(no content)"})
            elif m["role"] == "tool":
                c = {"role": "user", "content": f"<observation tool={m.get('name')}>\n{m['content']}\n</observation>", "_obs": True, "_stub": m.get("_stub", "")}
                if converted and converted[-1]["role"] == "user" and converted[-1].get("_obs"):
                    converted[-1]["content"] += "\n" + c["content"]
                else:
                    converted.append(c)
            else:
                converted.append(dict(m))
        self.messages = converted

    def _append_assistant(self, turn: ModelTurn, calls: list[ToolCall]) -> None:
        m: dict = {"role": "assistant", "content": turn.text}
        if self.native:
            m["tool_calls"] = [c.to_dict() for c in calls]
        if turn.raw is not None:
            m["_raw"] = turn.raw
        self.messages.append(m)

    def _append_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def _append_observations(self, results: list[tuple[ToolCall, str, bool]], hint: str) -> None:
        foot = prompts.footer(self.state, self.budget, self._diffstat(), hint)
        n = len(results)
        if self.native:
            for i, (call, out, is_err) in enumerate(results):
                content = REDACT(out) + ("\n\n" + foot if i == n - 1 else "")
                self.obs_count += 1
                self.messages.append({
                    "role": "tool", "tool_call_id": call.id, "name": call.name, "content": content, "is_error": is_err,
                    "_obs": True, "_stub": _stub(call, out), "_views": [v for v in [getattr(call, "view", None)] if v],
                })
        else:
            blocks = []
            stubs = []
            for call, out, _ in results:
                blocks.append(f'<observation tool="{call.name}">\n{REDACT(out)}\n</observation>')
                stubs.append(_stub(call, out))
            self.obs_count += 1
            views = [v for v in (getattr(c, "view", None) for c, _, _ in results) if v]
            self.messages.append({"role": "user", "content": "\n".join(blocks) + "\n\n" + foot, "_obs": True,
                                  "_stub": "\n".join(stubs) + "\n" + foot, "_views": views})

    def _file_sha(self, rel: str, cache: dict) -> str:
        if rel not in cache:
            try:
                cache[rel] = short_hash((self.root / rel).read_bytes().decode("utf-8", "surrogateescape"), 16)
            except OSError:
                cache[rel] = ""
        return cache[rel]

    def _mask_plan(self) -> tuple[set, set, set]:
        """(masked, pinned, outdated) message indices. Old observations are hidden to save tokens, EXCEPT the
        latest still-current view of each file (within a size budget): hiding those makes the model re-read
        them, which costs more than keeping them. Views of files edited since are marked outdated."""
        if not self.masking:
            return set(), set(), set()
        keep = 2 if self.aggressive_mask else int(self.cfg.tools["keep_full_observations"])
        obs_idx = [i for i, m in enumerate(self.messages) if m.get("_obs")]
        n_mask = max(0, len(obs_idx) - keep)
        if not self.aggressive_mask:
            n_mask = (n_mask // 4) * 4  # move the mask boundary in chunks: keeps the cached prefix stable
        masked = set(obs_idx[:n_mask])
        cache: dict = {}
        budget = int(float(self.cfg.tools.get("pin_views_fraction", 0.3)) * int(self.cfg.model["context_window"]) * 3.5)
        budget = min(budget, 60000) // (3 if self.aggressive_mask else 1)
        pinned, outdated, seen = set(), set(), set()
        for i in reversed(obs_idx):
            views = self.messages[i].get("_views") or []
            if not views:
                continue
            current = [v for v in views if self._file_sha(v[0], cache) == v[1]]
            if not current:
                outdated.add(i)
                continue
            key = tuple((v[0], v[2], v[3]) for v in current)
            size = len(self.messages[i].get("content", ""))
            if i in masked and key not in seen and size <= budget:
                pinned.add(i)
                budget -= size
            seen.add(key)
        return masked - pinned, pinned, outdated

    def _compiled_messages(self) -> list[dict]:
        masked, _, outdated = self._mask_plan()
        out = []
        for i, m in enumerate(self.messages):
            if i in masked:
                if i in outdated:
                    files = ", ".join(sorted({v[0] for v in m.get("_views") or []}))
                    m = dict(m, content=f"[outdated view of {files}: the file changed after this read; re-read it if needed]")
                else:
                    m = dict(m, content=m.get("_stub") or "[older output elided]")
            out.append(m)
        return out

    def _visible_view(self, view) -> bool:
        masked, _, _ = self._mask_plan()
        for i, m in enumerate(self.messages):
            if m.get("_obs") and i not in masked and view in (m.get("_views") or []):
                return True
        return False

    def _view_of(self, args: dict):
        if (args.get("mode") or "lines") == "skeleton":
            return None
        try:
            fp = resolve_path(self.root, args["path"])
            if not fp.is_file():
                return None
            rel = fp.relative_to(self.root).as_posix()
            return (rel, short_hash(fp.read_bytes().decode("utf-8", "surrogateescape"), 16), args.get("start"), args.get("end"))
        except (ToolError, OSError, ValueError):
            return None

    # ---------------------------------------------------------------------------- dispatch
    def _dispatch(self, call: ToolCall) -> tuple[str, bool]:
        spec = self.spec_by_name.get(call.name) or self.spec_by_name.get(call.name.rsplit(".", 1)[-1])
        if spec is not None:
            call.name = spec["name"]
        if spec is None:
            return f"ERROR UNKNOWN_TOOL: '{call.name}' is not a tool. Available: {', '.join(self.spec_by_name)}", True
        if call.parse_error:
            if self.native:
                self.native_arg_errors += 1
                if self.native_arg_errors >= 3:
                    self._switch_to_text_mode("repeated malformed tool arguments")
            return f"ERROR INVALID_ARGS: {call.parse_error}", True
        args = dict(call.args)
        err = validate_args(spec, args)
        if err:
            return f"ERROR INVALID_ARGS: {err}", True
        if call.name == "read_file":
            view = self._view_of(args)
            if view is not None and self._visible_view(view):
                return (f"({view[0]} is unchanged since you last read these lines and that output is still shown above; "
                        "use it instead of re-reading. Ask for a different range if you need other lines.)"), False
        if call.name in ("search_code", "read_file"):
            key = call.name + json.dumps(args, sort_keys=True) + self.tree
            try:  # scratch/ignored files are not in the tree hash: key on their content too
                fp = resolve_path(self.root, args.get("path") or ".", must_exist=False)
                if fp.is_file():
                    st_ = fp.stat()
                    key += f"|{st_.st_mtime_ns}|{st_.st_size}"
                elif call.name == "search_code":
                    key += "|" + str(self.ws.scratch.stat().st_mtime_ns if self.ws.scratch.exists() else 0)
            except (ToolError, OSError):
                pass
            if key in self.seen_calls:
                step, idx = self.seen_calls[key]
                visible = (not self.masking) or (self.obs_count - idx) < int(self.cfg.tools["keep_full_observations"])
                if visible:
                    return (f"(identical to your {call.name} at step {step}; the code has not changed since, so the output is "
                            "the same - see above. Use a different query/range if you need new information.)"), False
            self.seen_calls[key] = (self.state.step, self.obs_count)
        try:
            handler = getattr(self, f"_t_{call.name}")
            out, is_err = handler(args)
            if call.name == "read_file" and not is_err:
                call.view = self._view_of(args)  # lets masking keep the latest current view of this file
            return out, is_err
        except ToolError as e:
            return f"ERROR {e.code}: {e.message}", True
        except Exception as e:  # never crash the run on a tool bug; report it actionably
            self.tel.event("tool_exception", tool=call.name, error=repr(e), tb=traceback.format_exc()[-2000:])
            return f"ERROR INTERNAL: {type(e).__name__}: {e}", True

    def _diffstat(self) -> str:
        if not self.tree or self.tree == self.ws.base_tree:
            return ""
        out = self.ws.git("diff", "--shortstat", self.ws.base_tree, self.tree, check=False).stdout.decode().strip()
        return re.sub(r" changed|insertions?|deletions?", "", out).replace("  ", " ").strip()

    def _after_mutation(self) -> list[str]:
        old = self.tree
        self.tree = self.ws.tree()
        if old == self.tree:
            return []
        sig = self.guard.record_tree(self.tree)
        if sig:
            self._pending_signal = sig
        return [p for _, p in self.ws.changed(old, self.tree)]

    # ---- tools
    def _t_search_code(self, a: dict) -> tuple[str, bool]:
        out = self.searcher.search(a["query"], a.get("path") or ".", a.get("mode") or "regex", int(self.cfg.tools["search_max_hits"]), a.get("glob"))
        return out, out.startswith("ERROR")

    def _t_read_file(self, a: dict) -> tuple[str, bool]:
        if (a.get("mode") or "lines") == "skeleton":
            return skeleton(self.root, a["path"]), False
        out, _meta = view_lines(self.root, a["path"], a.get("start"), a.get("end"), int(self.cfg.tools["read_window_lines"]))
        return out, False

    def _t_edit_file(self, a: dict) -> tuple[str, bool]:
        out, paths = self.editor.edit(a["path"], a["edits"])
        return out + self._post_edit(paths), False

    def _t_write_file(self, a: dict) -> tuple[str, bool]:
        out, paths = self.editor.write(a["path"], a["content"], bool(a.get("overwrite")))
        return out + self._post_edit(paths), False

    def _post_edit(self, paths: list[str]) -> str:
        self._after_mutation()
        scratch_only = all(p.startswith(SCRATCH_DIR + "/") for p in paths)
        st = self.state
        if scratch_only:
            if st.phase == LOCATE:
                st.set_phase(REPRODUCE)
            return ""
        st.edits_since_check += 1
        st.set_phase(EDIT)
        return self._auto_check()

    def _scratch_sig(self) -> str:
        sc = self.ws.scratch
        if not sc.is_dir():
            return ""
        return short_hash("|".join(f"{p}:{p.stat().st_mtime_ns}:{p.stat().st_size}" for p in sorted(sc.rglob("*")) if p.is_file()), 12)

    def _check_key(self, kind: str, cmd: str) -> str:
        if not self.cfg.tools.get("cache_checks", True):
            return f"nocache|{time.time()}"
        return f"{kind}|{normalize_command(self.framework, cmd)}|{self.tree}|{self._scratch_sig()}"

    def _auto_check(self) -> str:
        """After a source edit, re-run the check that last showed the problem (a failing reproducer or test)
        and attach a short result. Saves the model a whole round-trip, and gives feedback sooner."""
        if not self.cfg.tools.get("auto_check_after_edit", True) or not self._red_check:
            return ""
        kind, cmd, took = self._red_check
        if took > float(self.cfg.tools.get("auto_check_max_s", 30)):
            return ""  # a slow check (e.g. a whole suite) is not worth re-running after every edit
        timeout = min(int(self.cfg.tools["test_timeout_s"]), int(self.cfg.tools.get("auto_check_timeout_s", 120)))
        if kind == "tests":
            run = self.executor.run(cmd, timeout)
            self._after_mutation()
            failing = run.failing()
            counts = ", ".join(f"{k}={v}" for k, v in sorted(run.counts().items())) or "none parsed"
            summary = f"auto-check after this edit: `{run.command}` -> {counts}"
            if run.timed_out:
                summary += " (TIMEOUT)"
            elif failing:
                summary += f"; still failing: {', '.join(failing[:6])}\n" + head_tail(run.failure_excerpt, 700, 300)[0]
            else:
                summary += " - all passing now"
            self.state.observed_tests.append({"command": cmd, "failing": bool(failing), "tree": self.tree, "base_failing": False})
        else:
            r = self.runner.run(cmd, timeout=timeout)
            self._after_mutation()
            summary = f"auto-check after this edit: `{cmd}` -> exit {r.exit_code if not r.timed_out else 'TIMEOUT'}"
            if r.exit_code not in (0, None) or r.timed_out:
                summary += "\n" + head_tail((r.stdout + "\n" + r.stderr).strip(), 400, 400)[0]
            self.state.observed_scripts.append({"command": cmd, "exit": r.exit_code, "tree": self.tree})
        self._note_check()
        self._check_cache[self._check_key(kind, cmd)] = (self.state.step, summary)
        self.tel.event("auto_check", kind=kind, command=cmd, summary=summary[:300])
        return "\n\n" + summary

    def _t_run_command(self, a: dict) -> tuple[str, bool]:
        cmd = a["command"]
        if SCRATCH_DIR in cmd:
            hit = self._check_cache.get(self._check_key("script", cmd))
            if hit:
                return (f"(this exact reproducer already ran on this exact code at step {hit[0]}; result unchanged)\n"
                        f"{hit[1]}"), False
        r = self.runner.run(cmd, cwd=a.get("cwd"), timeout=min(int(a.get("timeout") or self.cfg.tools["cmd_timeout_s"]), int(self.cfg.tools["test_timeout_s"])))
        r.changed_paths = self._after_mutation()
        self._note_check()
        if SCRATCH_DIR in cmd and not r.denied:
            self.state.observed_scripts.append({"command": cmd, "exit": r.exit_code, "tree": self.tree})
            if r.exit_code not in (0, None) and not r.launch_error:
                self._red_check = ("script", cmd, r.duration_s)
            self._check_cache[self._check_key("script", cmd)] = (self.state.step, format_cmd_result(r)[:1500])
        return format_cmd_result(r), bool(r.denied or r.launch_error or r.timed_out)

    def _note_check(self) -> None:
        st = self.state
        st.edits_since_check = 0
        if st.phase in (EDIT, RECOVER):
            st.set_phase(CHECK)

    def _t_run_tests(self, a: dict) -> tuple[str, bool]:
        fw = self.framework
        if a.get("command"):
            cmd = a["command"]
        elif a.get("tests"):
            if not fw.base_cmd:
                return "ERROR NO_FRAMEWORK: no test framework detected; pass an explicit `command`.", True
            cmd = build_command(fw, a["tests"])
        else:
            return "ERROR INVALID_ARGS: give `tests` (files/ids) or a `command`. Running the whole suite is rarely needed.", True
        timeout = min(int(a.get("timeout") or self.cfg.tools["test_timeout_s"]), int(self.cfg.tools["test_timeout_s"]))
        hit = self._check_cache.get(self._check_key("tests", cmd))
        if hit:
            return (f"(these tests already ran on this exact code at step {hit[0]}; result unchanged - edit the code or "
                    f"choose other tests)\n{hit[1]}"), False
        cand = self.executor.run(cmd, timeout)
        changed_by_tests = self._after_mutation()
        self._note_check()
        lines = [f"$ {cand.command}", f"[exit={cand.exit_code}{' TIMEOUT' if cand.timed_out else ''} | {cand.duration_s:.1f}s | parser={cand.parser} | log_id={cand.log_id}]"]
        counts = cand.counts()
        lines.append("outcomes: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none parsed"))
        compare_note = ""
        base = None
        if self.ws.changed(None, self.tree) and not cand.timed_out:
            keep = [p for s, p in self.ws.changed(None, self.tree) if not s.startswith("D") and is_test_path(p)]
            base = self.executor.run_base(cmd, keep, timeout)
            tr = classify_transitions(base.outcomes, cand.outcomes)
            parts = []
            for k in ("fixed", "regressed", "still_failing", "new_fail", "new_pass", "to_skip"):
                if tr[k]:
                    parts.append(f"{k} ({len(tr[k])}): {', '.join(tr[k][:8])}{' ...' if len(tr[k]) > 8 else ''}")
            parts.append(f"still_passing: {len(tr['still_passing'])}")
            compare_note = "compared with ORIGINAL code -> " + " | ".join(parts)
            lines.append(compare_note)
        failing = cand.failing()
        head_c = min(3000, int(self.cfg.tools["obs_head_chars"]))
        tail_c = min(1500, int(self.cfg.tools["obs_tail_chars"]))
        if failing:
            lines.append(f"failing on current code ({len(failing)}): {', '.join(failing[:12])}{' ...' if len(failing) > 12 else ''}")
            self._red_check = ("tests", cmd, cand.duration_s)
            summary_lines = list(lines)
            lines.append("--- failure excerpt ---")
            lines.append(head_tail(cand.failure_excerpt, head_c, tail_c)[0])
        else:
            summary_lines = list(lines)
            if not cand.outcomes or cand.coarse:
                lines.append("--- output tail ---")
                lines.append(cand.output_tail[-min(2500, head_c):])
        if changed_by_tests:
            lines.append("note: running tests modified tracked files: " + ", ".join(changed_by_tests[:8]))
        self.state.observed_tests.append({"command": cmd, "failing": bool(failing), "tree": self.tree, "base_failing": bool(base and base.failing())})
        if not cand.timed_out:
            self._check_cache[self._check_key("tests", cmd)] = (self.state.step, "\n".join(summary_lines[1:]))
        if cand.parser != "none" and cand.outcomes:
            self.memory.add_fact(f"`{cand.command}` runs and reports per-test outcomes ({cand.parser})", cand.log_id, "test")
        self.tel.event("tests", command=cmd, counts=counts, parser=cand.parser, log_id=cand.log_id, compare=compare_note[:500])
        return "\n".join(lines), cand.timed_out

    def _t_read_log(self, a: dict) -> tuple[str, bool]:
        from harness.util import clip_line

        try:
            text = self.logs.read(a["log_id"])
        except (FileNotFoundError, ValueError) as e:
            return f"ERROR NOT_FOUND: {e}", True
        lines = text.splitlines()
        head = int(self.cfg.tools["obs_head_chars"])
        tail = int(self.cfg.tools["obs_tail_chars"])
        if a.get("grep"):
            try:
                rx = re.compile(a["grep"])
            except re.error:
                rx = re.compile(re.escape(a["grep"]))
            hits = [f"{i + 1:>6}| {clip_line(ln, 800)}" for i, ln in enumerate(lines) if rx.search(ln)]
            body, _ = head_tail("\n".join(hits[:200]), head, tail)
            return f"[{a['log_id']}] {len(hits)} matching line(s):\n" + body, False
        s = max(1, int(a.get("start") or 1))
        e = min(len(lines), int(a.get("end") or s + 199), s + 399)
        body, _ = head_tail("\n".join(f"{i:>6}| {clip_line(lines[i - 1], 800)}" for i in range(s, e + 1)), head, tail)
        return f"[{a['log_id']}] lines {s}-{e} of {len(lines)}:\n" + body, False

    def _t_repo_changes(self, a: dict) -> tuple[str, bool]:
        action = a.get("action") or "diff"
        if action == "revert":
            paths = a.get("paths") or []
            if not paths:
                return "ERROR INVALID_ARGS: list the paths to revert (use ['*'] to revert everything).", True
            if paths == ["*"]:
                paths = [p for _, p in self.ws.changed()]
            rel_paths = []
            for p in paths:
                full = resolve_path(self.root, p, must_exist=False)
                rel_paths.append(full.relative_to(self.root).as_posix())
            done = self.ws.revert_paths(rel_paths)
            self._after_mutation()
            return f"Reverted to original: {', '.join(done) or '(nothing to revert)'}", False
        changed = self.ws.changed(None, self.tree)
        if not changed:
            return "No changes yet (relative to the original code).", False
        if not a.get("paths"):
            self.reviewed_tree = self.tree
        stat = self.ws.diff(None, self.tree, stat=True)
        diff = self.ws.diff(None, self.tree, paths=a.get("paths") or None)
        body, _ = head_tail(diff, 9000, 3000)
        return f"{stat.strip()}\n\n{body}", False

    def _t_update_plan(self, a: dict) -> tuple[str, bool]:
        p = self.state.plan
        for k in ("hypothesis", "acceptance", "plan", "notes"):
            if a.get(k):
                setattr(p, k, a[k].strip()[:3000])
        self.memory.add_note("plan", p.render())
        self.tel.event("plan", plan=p.render()[:3000])
        return "Plan recorded (pinned).", False

    def _t_activate_skill(self, a: dict) -> tuple[str, bool]:
        name = a["name"]
        if name in self.activated_skills:
            return f"Skill '{name}' is already loaded above.", False
        sk = self.skills.get(name)
        if not sk:
            return f"ERROR UNKNOWN_SKILL: {name}", True
        self.activated_skills.add(name)
        return render(sk), False

    # ---------------------------------------------------------------------------- hints/stuck
    def _hint(self) -> str:
        st, b = self.state, self.budget
        sig = getattr(self, "_pending_signal", None)
        if sig:
            self._pending_signal = None
            return f"Loop warning: {sig.detail}. Stop and reconsider the approach instead of re-trying the same versions."
        rem = b.remaining_steps()
        if rem <= 4:
            return "Budget nearly exhausted: submit now (with your best evidence) or the harness will verify the current state."
        if st.phase == LOCATE and st.steps_in_phase >= int(self.cfg.loop["locate_nudge_steps"]):
            return (f"You have explored for {st.steps_in_phase} steps without editing. Record your hypothesis with update_plan, "
                    "then reproduce the bug or make the fix.")
        if st.edits_since_check >= int(self.cfg.loop["edit_without_check_nudge"]):
            return f"{st.edits_since_check} edits without any check: run your reproducer or the nearest tests now."
        if (st.phase in (EDIT, CHECK) and not st.plan.acceptance and not self._req_hint_given
                and self._needs_requirement_list()):
            self._req_hint_given = True
            return ("List the issue's required behaviours with update_plan(acceptance=...) and make sure your reproducer "
                    "checks each one.")
        if st.route in (LIGHT_PLAN, STRUCTURED) and not st.plan.plan and not st.plan.hypothesis and st.attempt_step >= 8:
            return "Record your hypothesis and plan with update_plan before editing further."
        if rem <= 12:
            return f"About {rem} steps left: converge on a fix and submit with evidence."
        if self._single_reads >= 2 and not self._batch_tip_given and self.native:
            self._batch_tip_given = True
            return "Tip: request independent reads/searches together (several tool calls in one reply) to save steps."
        return ""

    def _handle_stuck(self, sig: StuckSignal, level: int) -> str | None:
        st = self.state
        st.stuck_events += 1
        self.tel.event("stuck", kind=sig.kind, detail=sig.detail, level=level)
        self.tel.say(f"stuck detector: {sig.kind} (level {level})")
        if level == 1:
            self._append_user(
                f"[harness] Loop detected: {sig.detail}. Repeating will not produce new information. State what you "
                "know, what is still unknown, and choose a materially different next action."
            )
            return None
        if level == 2:
            st.route = STRUCTURED
            body = render(self.skills["failure-recovery"]) if "failure-recovery" in self.skills else ""
            self.activated_skills.add("failure-recovery")
            self._append_user(
                f"[harness] Loop detected again: {sig.detail}. Your current strategy is not working. Follow this procedure, "
                f"and record a NEW hypothesis with update_plan before any further edit.\n{body}"
            )
            return None
        return "stuck"

    # ---------------------------------------------------------------------------- verification
    def _test_plan(self, args: dict) -> list[TestSpec]:
        fw = self.framework
        specs: list[TestSpec] = []
        seen: set[str] = set()

        def add(cmd: str, kind: str, reason: str) -> None:
            key = normalize_command(fw, cmd).strip()
            if key and key not in seen and len(specs) < 10:
                seen.add(key)
                specs.append(TestSpec(cmd, kind, reason))

        tc_cmds, tc_ids = self._target_split()
        for c in tc_cmds:
            add(c, "target", "test case supplied with the task")
        if tc_ids and fw.base_cmd:
            add(build_command(fw, tc_ids), "target", "test case supplied with the task")
        if (args.get("reproducer") or "").strip():
            add(args["reproducer"].strip(), "repro", "declared reproducer")
        changed = [p for s, p in self.ws.changed() if not s.startswith("D")]
        if fw.base_cmd:
            src = [p for p in changed if not is_test_path(p)]
            limit = int(self.cfg.verify["related_test_limit"])
            rel = related_test_files(changed, self.manifest.files, self.manifest.test_files, limit)
            if self.codemap:  # tests that actually use the changed code (usage graph), not just similar names
                for t in self.codemap.tests_using([p for p in changed if not is_test_path(p)], limit=4):
                    if t not in rel and len(rel) < limit + 2:
                        rel.append(t)
            for p in changed:
                if is_test_path(p) and p not in rel and p.rsplit("/", 1)[-1] not in ("conftest.py", "__init__.py"):
                    rel.append(p)
            if fw.name == "go":
                dirs = sorted({"./" + str(Path(p).parent) for p in src + rel if p.endswith(".go")})
                if dirs:
                    add(build_command(fw, dirs[:4]), "related", "packages of changed files")
            elif rel:
                add(build_command(fw, rel), "related", "tests associated with changed files")
            policy = str(self.cfg.verify["full_suite"]).lower()
            small = len(self.manifest.test_files) <= int(self.cfg.verify["max_test_files_for_auto_full"])
            if policy == "always" or (policy == "auto" and (small or self.state.route == STRUCTURED) and fw.name not in ("django", "sympy", "none", "make")):
                add(build_command(fw, []), "full", "full test suite")
        # Keep independent regression checks ahead of the agent's optional commands. Otherwise
        # ten submitted commands can fill the cap and silently exclude related/full-suite tests.
        for c in args.get("test_commands") or []:
            if c.strip():
                add(c.strip(), "target", "declared by agent")
        ids = [t for t in (args.get("fail_to_pass") or []) if t.strip()]
        if ids and fw.base_cmd:
            add(build_command(fw, ids), "target", "fail_to_pass ids")
        # Evidence the agent gathered while working (can support, never contradict the issue claim).
        red = [o["command"] for o in self.state.observed_tests if o["failing"] or o["base_failing"]]
        for c in list(dict.fromkeys(reversed(red)))[:3]:
            add(c, "observed", "test command that failed during the work")
        scripts = [o["command"] for o in self.state.observed_scripts if o["exit"] not in (0, None)]
        for c in list(dict.fromkeys(reversed(scripts)))[:2]:
            add(c, "observed", "scratch script that failed during the work")
        return specs

    def _target_split(self) -> tuple[list[str], list[str]]:
        """Supplied test cases -> (shell commands, test ids). Only entries starting with a known runner are
        commands; `name (module.Class)` display forms are normalized to ids."""
        cmds = [t for t in self.target_tests if is_test_command(t)]
        ids = [normalize_test_id(t) for t in self.target_tests if not is_test_command(t)]
        return cmds, ids

    def _review_gate(self, args: dict) -> str | None:
        """Self-review before verification: once per patch version, show the exact diff and a checklist.
        Skipped when the model already inspected this exact patch or the budget is nearly spent."""
        if not self.cfg.verify.get("review_on_submit", True) or self.reviewed_tree == self.tree:
            return None
        if self.budget.remaining_steps() <= 3:
            return None
        self.reviewed_tree = self.tree
        self.pending_submit = dict(args)
        self.pending_tree = self.tree
        diff, _ = head_tail(self.ws.diff(None, self.tree), 8000, 3000)
        return (
            "SELF-REVIEW before verification. Your complete patch:\n" + diff +
            "\n\nCheck, fix anything that fails, then call submit again (your evidence arguments are remembered):\n"
            "1. Every hunk is needed for the issue - no debug output, commented-out code, stray files or reformatting.\n"
            "2. The general case from the issue is handled, not only its example; inputs that worked before still work.\n"
            "3. If code changed after you last ran your reproducer/tests, run them again now.\n"
            "4. No existing test was weakened; any change to an existing test file is required by the issue.\n"
            + self._requirements_review_line() +
            "6. submit names the evidence: reproducer and/or tests that fail before and pass after."
        )

    def _requirements_review_line(self) -> str:
        acc = self.state.plan.acceptance.strip()
        base = ("5. Your reproducer/tests check EVERY case the issue states (each example, and edge cases it names such as "
                "empty, whitespace or large values) - not only the first example.")
        if acc:
            return base + f" Requirements you recorded - confirm each is implemented AND checked:\n{acc[:1200]}\n"
        if self._needs_requirement_list():
            return base + " You have not listed the issue's required behaviours yet: list them now and check each.\n"
        return base + "\n"

    def _handle_submit(self, call: ToolCall) -> tuple[str, str | None]:
        st = self.state
        args = dict(call.args)
        err = validate_args(self.spec_by_name["submit"], args)
        if err:
            return f"ERROR INVALID_ARGS: {err}", None
        self.tree = self.ws.tree()
        if not self.ws.changed(None, self.tree):
            st.tool_errors += 1
            return ("REJECTED: there are no code changes to verify. Locate the fault and edit the code first "
                    f"(files in {SCRATCH_DIR}/ are not part of the patch)."), None
        review = self._review_gate(args)
        if review:
            return review, None
        # Evidence remembered by the self-review belongs to the reviewed patch (and quick follow-up fixes to it
        # in this attempt); it is consumed by this verification and never reused for a later patch.
        merged = dict(self.pending_submit) if self.pending_tree else {}
        merged.update({k: v for k, v in args.items() if v})
        args = merged
        self.pending_submit, self.pending_tree = {}, ""
        specs = self._test_plan(args)
        key = self.tree + "|" + "|".join(f"{s.kind}:{s.command}" for s in specs)
        if key == self.prev_report_key and st.last_report is not None:
            rep = st.last_report
            return rep.summary() + "\n[harness] Resubmitted without any change: keeping this verdict and ending the attempt.", "resubmitted_unchanged"
        st.set_phase(FINAL_VERIFY)
        st.verify_rounds += 1
        self.tel.event("submit", step=st.step, args={k: v for k, v in args.items() if k != "summary"}, summary=str(args.get("summary", ""))[:500])
        self.tel.say(f"verify: round {st.verify_rounds}, {len(specs)} test spec(s) ...")
        t0 = time.time()
        explicit = list(args.get("fail_to_pass") or []) + self._target_split()[1]
        rep = self.verifier.verify(specs, explicit)
        self.prev_report_key = key
        st.last_report = rep
        commit = self.ws.commit(rep.candidate_hash, f"cand{len(st.candidates) + 1}")
        st.candidates.append((rep.candidate_hash, commit, rep))
        self.memory.add_note("submit", args.get("summary", ""))
        self.tel.event("verification", round=st.verify_rounds, status=rep.status, seconds=round(time.time() - t0, 1), report=rep.to_dict(), summary=args.get("summary", "")[:2000])
        self.tel.say(f"verify: {rep.status} (fixed={len(rep.fixed_tests)}, regressions={len(rep.regressions)}) in {time.time() - t0:.0f}s")
        if rep.status == VERIFIED:
            st.set_phase("DONE")
            return rep.summary(), "verified"
        cls, guidance = self._classify(rep)
        sig = cls + ":" + short_hash(normalize_signature(" ".join(rep.regressions[:5]) + " ".join(str(v[1])[:300] for v in rep.claims.values())))
        count = st.failure_signatures.get(sig, 0) + 1
        st.failure_signatures[sig] = count
        st.set_phase(RECOVER)
        if st.verify_rounds >= 2:
            st.route = STRUCTURED
        ladder = ""
        if count == 2:
            ladder = ("\nThe same verification failure occurred twice: your hypothesis is probably wrong. Re-read the code path, "
                      "question your assumptions, and record a new hypothesis with update_plan before editing.")
        if count >= int(self.cfg.loop["same_failure_signature"]):
            return rep.summary() + "\n[harness] Same failure three times: abandoning this strategy.", f"repeated_failure:{cls}"
        if st.verify_rounds >= int(self.cfg.budget["max_verify_rounds"]):
            return rep.summary() + "\n[harness] Verification round limit reached for this attempt.", f"verify_rounds_exhausted:{cls}"
        if self.budget.work_exhausted():
            return rep.summary(), f"budget_after_verify:{cls}"
        return rep.summary() + "\n\n" + guidance + ladder, None

    def _classify(self, rep: VerificationReport) -> tuple[str, str]:
        c = rep.claims
        if c["structural_validity"][0] == "CONTRADICTED":
            return "syntax", f"Fix the syntax errors: {c['structural_validity'][1]}"
        if c["diff_scope"][0] == "CONTRADICTED":
            return "scope", (f"The diff audit blocked the patch: {c['diff_scope'][1]}. Restore weakened/deleted tests "
                             "(repo_changes action=revert) and fix the code instead.")
        if rep.regressions:
            ex = next((r["candidate"] for r in rep.results if r["transitions"].get("regressed")), None)
            logid = ex.get("log_id") if ex else ""
            return "regression", (f"Your change breaks tests that passed on the original code: {', '.join(rep.regressions[:8])}. "
                                  f"Inspect them (read_log {logid}) and adjust the fix to preserve existing behaviour; check other "
                                  "callers of the code you changed.")
        st, why = c["issue_behavior"]
        if st == "CONTRADICTED":
            return "target_test", (f"The issue is not fixed yet: {why[:1500]}\nInspect the failing assertion and the execution "
                                   "path; revise your hypothesis rather than patching symptoms.")
        if c["no_regressions"][0] == "CONTRADICTED":
            return "regression", f"Regression check failed: {c['no_regressions'][1]}"
        if st == UNKNOWN:
            if "same file as the requested" in why:
                return "sibling_failures", (f"{why}. Tests next to the requested test case usually describe the same change: "
                                            "make them pass too (without editing them), then submit again.")
            if "already pass" in why:
                return "weak_evidence", ("Your target test/reproducer already passes on the ORIGINAL code, so it does not capture "
                                         "the issue. Make it assert the exact behaviour the issue describes (it must fail on the "
                                         "original code), then submit again.")
            return "missing_evidence", (
                f"The fix is not demonstrated: {why}. Provide executable evidence: a reproducer script in {SCRATCH_DIR}/ that "
                "exits non-zero on the original code and 0 with your fix, and/or a focused regression test that fails before "
                "and passes after. Then submit again with `reproducer` and/or `test_commands`."
            )
        if c["no_regressions"][0] == UNKNOWN:
            return "missing_regression_evidence", (
                f"Regression safety is unmeasured: {c['no_regressions'][1]}. Include the existing test file(s) covering the "
                "code you changed in `test_commands` when you submit."
            )
        return "unknown", "Verification was inconclusive; see the notes above."

    # ---------------------------------------------------------------------------- attempts
    def _prepare_next_attempt(self, end_reason: str) -> None:
        st = self.state
        rep = st.last_report
        tree_now = self.ws.tree()
        actual = rep.summary() if rep is not None and rep.candidate_hash == tree_now else f"attempt ended: {end_reason}"
        diffstat = self.ws.diff(None, tree_now, stat=True).strip()[-600:] if self.ws.changed(None, tree_now) else "(no changes)"
        fr = FailureRecord(
            attempt=st.attempt, strategy=f"changes: {diffstat}", hypothesis=st.plan.hypothesis, predicted="verified fix",
            actual=actual, signature=end_reason.split(":")[0], failure_class=end_reason, patch_hash=tree_now,
            forbidden_repeat="the same patch/approach" if rep is not None and rep.status == FAILED else "",
        )
        st.failures.append(fr)
        self.memory.add_episode(st.attempt, fr.strategy, fr.hypothesis, fr.predicted, fr.actual, fr.signature, fr.failure_class, fr.patch_hash)
        if self.ws.changed(None, tree_now) and not any(c[0] == tree_now for c in st.candidates):
            # keep unverified work reachable for final selection
            st.candidates.append((tree_now, self.ws.commit(tree_now, f"cand{len(st.candidates) + 1}"), None))
        best = self._best_candidate()
        refuted = rep is not None and rep.candidate_hash == tree_now and rep.status == FAILED
        if end_reason.split(":")[0] in NON_EVIDENCE_ENDINGS and self.ws.changed(None, tree_now) and not refuted:
            # Ran out of context/steps, not out of ideas: keep the work and continue from it with a fresh context.
            self._start_note = ("The workspace still contains your changes from the previous attempt (it ended because the "
                                "context/step budget filled up, not because the approach failed). Continue from them: "
                                "re-read only what you need, finish the change, run the checks and submit.")
        elif best and best[2] is not None and best[2].claims.get("issue_behavior", (UNKNOWN,))[0] == SUPPORTED:
            self.ws.restore(best[0])
            self._start_note = ("The workspace now contains the best previous candidate, which fixes the target behaviour but was "
                                "not verified (see outcome above). Improve it rather than starting over.")
        else:
            self.ws.restore(self.ws.base_tree)
            self._start_note = "The workspace was reset to the original code. Take a materially different approach."
        self.tree = self.ws.tree()
        self.pending_submit, self.pending_tree, self.reviewed_tree = {}, "", ""
        st.attempt += 1
        st.set_phase(LOCATE)
        st.edits_since_check = 0
        self.tel.say(f"starting attempt {st.attempt} with a fresh context")

    _start_note = ""
    _codemap_text = ""
    _pending_signal = None

    def _best_candidate(self):
        scored = [c for c in self.state.candidates if c[2] is not None]
        if not scored:
            return self.state.candidates[-1] if self.state.candidates else None
        return max(scored, key=lambda c: c[2].score())

    # ---------------------------------------------------------------------------- finalize
    def _finalize(self, end_reason: str) -> RunResult:
        st = self.state
        final_report: VerificationReport | None = None
        try:
            tree_now = self.ws.tree()
            if self.ws.changed(None, tree_now) and not any(c[0] == tree_now and c[2] is not None for c in st.candidates):
                # Unverified current work: spend the reserve verifying it (never report unverified success).
                self.tel.say("final verification of the current (unsubmitted) changes ...")
                rep = self.verifier.verify(self._test_plan(self.pending_submit), self._target_split()[1])
                st.candidates.append((rep.candidate_hash, self.ws.commit(rep.candidate_hash, f"cand{len(st.candidates) + 1}"), rep))
            best = self._best_candidate()
            if best is not None:
                if self.ws.tree() != best[0]:
                    self.ws.restore(best[0])
                final_report = best[2]
            if final_report is not None and final_report.candidate_hash != self.ws.tree():
                final_report.status = INCONCLUSIVE
                final_report.blocking.append("final workspace does not match the verified candidate")
        except Exception as e:
            self.tel.event("finalize_error", error=repr(e), tb=traceback.format_exc()[-2000:])
        patch = ""
        try:
            patch = self.ws.diff() if self.ws.changed() else ""
        except Exception:
            pass
        status = final_report.status if final_report is not None else (FAILED if not patch else INCONCLUSIVE)
        if not patch:
            status = FAILED
        metrics = {
            **self.budget.to_dict(),
            "attempts": st.attempt,
            "verify_rounds_total": len([c for c in st.candidates if c[2] is not None]),
            "tool_errors": st.tool_errors,
            "stuck_events": st.stuck_events,
            "route": st.route,
            "end_reason": end_reason,
            "tool_mode": "native" if self.native else "text",
            "masking": self.masking,
            "token_breakdown_est": {  # where input tokens went (estimates; provider-reported totals above)
                "fixed_prompt_and_tools": self.tok_fixed,
                "task_and_history": self.tok_history,
                "codemap_in_first_message": estimate_tokens(self._codemap_text) if self._codemap_text else 0,
            },
        }
        (self.run_dir / "patch.diff").write_text(patch, encoding="utf-8")
        result = {
            "status": status,
            "task_id": self.task_id,
            "repo": str(self.root),
            "base_revision": self.ws.real_git_head or None,
            "candidate_hash": final_report.candidate_hash if final_report else None,
            "patch_file": str(self.run_dir / "patch.diff"),
            "changed_files": final_report.changed_files if final_report else [],
            "claims": {k: {"status": v[0], "detail": v[1]} for k, v in (final_report.claims.items() if final_report else [])},
            "fixed_tests": final_report.fixed_tests if final_report else [],
            "regressions": final_report.regressions if final_report else [],
            "notes": (final_report.notes + final_report.skipped + final_report.blocking) if final_report else [],
            "evidence": final_report.results if final_report else [],
            "metrics": metrics,
            "model": self.provider.describe(),
            "model_config": {k: self.cfg.model.get(k) for k in ("provider", "name", "temperature", "seed", "tool_mode", "effort")}
            | {"base_url_set": bool(self.cfg.model.get("base_url"))},
            "config_source": self.cfg.source,
            "plan": st.plan.render(),
        }
        (self.run_dir / "result.json").write_text(REDACT(json.dumps(result, indent=2, default=str)), encoding="utf-8")
        (self.run_dir / "report.md").write_text(REDACT(_report_md(result, final_report)), encoding="utf-8")
        self.tel.event("final", status=status, metrics=metrics)
        try:
            self.ws.finish(self.run_dir, keep_scratch=bool(self.cfg.run.get("keep_scratch")))
        except Exception:
            pass
        self.memory.close()
        summary = final_report.summary() if final_report else f"No verified candidate ({end_reason})."
        return RunResult(status, patch, final_report, self.run_dir, metrics, summary)


def _brief_args(args: dict, limit: int = 160) -> str:
    parts = []
    for k, v in args.items():
        if k == "edits":
            parts.append(f"edits={len(v)}")
        elif k == "content":
            parts.append(f"content=<{len(str(v))} chars>")
        else:
            s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else repr(v)
            parts.append(f"{k}={s[:80]}")
    s = " ".join(parts)
    return s if len(s) <= limit else s[:limit] + "..."


def _stub(call: ToolCall, out: str) -> str:
    first = out.strip().splitlines()[0][:160] if out.strip() else ""
    return f"[older {call.name} output elided ({len(out)} chars); first line: {first}]"


def _report_md(result: dict, rep: VerificationReport | None) -> str:
    lines = [f"# Harness result: {result['status']}", "", f"- repository: `{result['repo']}`", f"- model: {result['model']}",
             f"- candidate hash: `{result['candidate_hash']}`", ""]
    lines.append("## Claims")
    for k, v in result["claims"].items():
        lines.append(f"- **{k}**: {v['status']} - {v['detail'][:400]}")
    if result["fixed_tests"]:
        lines += ["", "## Fixed tests (fail on original -> pass on patch)"] + [f"- `{t}`" for t in result["fixed_tests"][:50]]
    if result["regressions"]:
        lines += ["", "## Regressions"] + [f"- `{t}`" for t in result["regressions"][:50]]
    if result["notes"]:
        lines += ["", "## Notes / evidence gaps"] + [f"- {n}" for n in result["notes"][:30]]
    lines += ["", "## Test evidence"]
    for r in result["evidence"]:
        cand = r["candidate"]
        base = r.get("base") or {}
        lines.append(f"- [{r['kind']}] `{r['command'][:160]}` base={base.get('counts')} candidate={cand.get('counts')} "
                     f"logs={base.get('log_id')},{cand.get('log_id')}")
    if result.get("plan"):
        lines += ["", "## Agent plan / hypothesis", "```", result["plan"][:3000], "```"]
    m = result["metrics"]
    lines += ["", "## Metrics", "```", json.dumps(m, indent=2), "```"]
    return "\n".join(lines) + "\n"
