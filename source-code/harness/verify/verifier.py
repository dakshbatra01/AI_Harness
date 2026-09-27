"""External verifier: the ONLY component that may recommend VERIFIED.

For each test command it runs the candidate and the base code (base = original sources plus
the candidate's versions of test files, i.e. how hidden FAIL_TO_PASS tests are graded), then
compares per-test identities. Evidence is bound to the candidate tree hash."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from harness.repo.manifest import is_test_path
from harness.tools.editor import syntax_error
from harness.tools.runtime import CommandRunner
from harness.util import REDACT, new_id, short_hash
from harness.verify.ledger import CONTRADICTED, SUPPORTED, UNKNOWN, EvidenceItem, Ledger
from harness.verify.testrun import (
    FAILLIKE,
    PASS,
    SKIP,
    TestFramework,
    TestRun,
    classify_transitions,
    env_fingerprint,
    failure_excerpt,
    normalize_command,
    canonical_test_id,
    parse_output,
    test_id_matches,
)

VERIFIED, FAILED, INCONCLUSIVE = "VERIFIED", "FAILED", "INCONCLUSIVE"
CLAIMS = ("issue_behavior", "no_regressions", "structural_validity", "diff_scope")
_ASSERT_RE = re.compile(r"\b(assert\w*|expect\(|should\.|self\.fail|pytest\.raises|t\.(Error|Fatal)|assert!)")


@dataclass
class TestSpec:
    command: str
    kind: str  # target | repro | related | full
    reason: str = ""


@dataclass
class SpecResult:
    spec: TestSpec
    base: TestRun | None
    cand: TestRun
    transitions: dict
    flaky: list = field(default_factory=list)


@dataclass
class VerificationReport:
    candidate_hash: str
    environment_hash: str
    test_manifest_hash: str
    status: str
    claims: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    blocking: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    changed_files: list = field(default_factory=list)
    fixed_tests: list = field(default_factory=list)
    regressions: list = field(default_factory=list)

    def score(self) -> tuple:
        rank = {VERIFIED: 3, INCONCLUSIVE: 2, FAILED: 1}.get(self.status, 0)
        return (rank, len(self.fixed_tests) - 3 * len(self.regressions), -len(self.changed_files))

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def summary(self, max_items: int = 12) -> str:
        lines = [f"VERIFICATION {self.status} (candidate {self.candidate_hash[:12]})"]
        for c in CLAIMS:
            st, why = self.claims.get(c, (UNKNOWN, ""))
            lines.append(f"  - {c}: {st}" + (f" - {why}" if why else ""))
        if self.fixed_tests:
            lines.append(f"  fixed (fail->pass): {', '.join(self.fixed_tests[:max_items])}")
        if self.regressions:
            lines.append(f"  REGRESSIONS (pass->fail): {', '.join(self.regressions[:max_items])}")
        for b in self.blocking[:6]:
            lines.append(f"  BLOCKING: {b}")
        for n in self.notes[:6]:
            lines.append(f"  note: {n}")
        for s in self.skipped[:4]:
            lines.append(f"  not checked: {s}")
        return "\n".join(lines)


_INTERPRETER = {".py": "python", ".sh": "bash", ".js": "node", ".mjs": "node", ".rb": "ruby"}


def with_interpreter(command: str) -> str:
    """`.harness_scratch/repro.py` -> `python .harness_scratch/repro.py`: scratch files are not executable."""
    parts = command.strip().split(None, 1)
    if parts and "/" in parts[0] or (parts and parts[0].endswith(tuple(_INTERPRETER))):
        ext = "." + parts[0].rsplit(".", 1)[-1] if "." in parts[0].rsplit("/", 1)[-1] else ""
        if ext in _INTERPRETER:
            return f"{_INTERPRETER[ext]} {command.strip()}"
    return command


_NUM_LIT = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
_STR_LIT = re.compile(r"""(?<![\w])(["'])([^"'\n]{2,60})\1""")
_COND_START = ("if ", "elif ", "else if", "} else if", "case ", "when ", "while ")


def issue_literals(issue: str) -> set:
    """Concrete values from the issue (numbers with 2+ digits, quoted strings) - the example inputs/outputs."""
    lits = set()
    for m in _NUM_LIT.finditer(issue):
        num = m.group(0).lstrip("-").rstrip(",")
        if len(num.replace(",", "").replace(".", "")) >= 2:
            lits.add(num)
    for m in _STR_LIT.finditer(issue):
        text = m.group(2)
        # Identifier-like strings ("deps", "name") and paths are vocabulary, not example values.
        if not re.fullmatch(r"[A-Za-z_][\w.-]*", text) and not text.endswith((".py", ".js", ".ts", ".go", ".rs")):
            lits.add(text)
    return lits


def special_cased_literals(line: str, literals: set) -> list:
    """Issue literals used inside a condition on this added line: a sign of special-casing the example."""
    code = line.strip()
    if not (code.startswith(_COND_START) or " == " in code or "===" in code):
        return []
    found = []
    for lit in literals:
        if lit[:1].isdigit():
            if re.search(r"(?<![\w.])" + re.escape(lit) + r"(?![\w])", code):
                found.append(lit)
        elif f"'{lit}'" in code or f'"{lit}"' in code:
            found.append(lit)
    return sorted(found)


class TestExecutor:
    """Runs test commands on the candidate and (cached) on the base code."""

    def __init__(self, workspace, runner: CommandRunner, framework: TestFramework, timeout: int, tel=None) -> None:
        self.ws = workspace
        self.runner = runner
        self.fw = framework
        self.timeout = timeout
        self.tel = tel
        self._base_cache: dict[str, TestRun] = {}

    def run(self, command: str, timeout: int | None = None) -> TestRun:
        cmd = normalize_command(self.fw, with_interpreter(command))
        r = self.runner.run(cmd, timeout=timeout or self.timeout, allow_denied=False)
        if r.denied or r.launch_error:
            return TestRun(command=cmd, exit_code=None, parser="none", output_tail=r.denied or r.launch_error or "", coarse=True)
        raw = ""
        try:
            raw = self.runner.logs.read(r.log_id)
        except (OSError, ValueError):
            raw = r.stdout + "\n" + r.stderr
        so, _, se = raw.partition("--- stderr ---")
        outcomes, parser, coll = parse_output(cmd, so, se, r.exit_code)
        tr = TestRun(
            command=cmd,
            exit_code=r.exit_code,
            outcomes=outcomes,
            parser=parser,
            timed_out=r.timed_out,
            duration_s=r.duration_s,
            log_id=r.log_id,
            output_tail=(r.stdout + "\n" + r.stderr)[-1500:],
            failure_excerpt=failure_excerpt(so, se),
            collection_error=coll,
        )
        if not outcomes and r.exit_code in (126, 127):
            tr.launch_failed = True  # command not found / not executable: environment problem, not a test result
        if not outcomes and not r.timed_out:
            tr.coarse = True
            tr.outcomes = {f"[cmd] {cmd[:120]}": PASS if r.exit_code == 0 else "FAIL"}
        return tr

    def run_base(self, command: str, keep_paths: list[str], timeout: int | None = None) -> TestRun:
        key = command + "|" + "|".join(sorted(keep_paths)) + "|" + self._keep_hash(keep_paths)
        if key in self._base_cache:
            return self._base_cache[key]
        with self.ws.base_swap(keep_paths=keep_paths):
            tr = self.run(command, timeout)
        self._base_cache[key] = tr
        return tr

    def _keep_hash(self, keep_paths: list[str]) -> str:
        """Base results depend on the kept test files AND on scratch files (reproducers are not in the
        tree hash), so both are part of the cache key."""
        blob = []
        for p in sorted(keep_paths):
            fp = self.ws.root / p
            blob.append(p + ":" + (short_hash(fp.read_text(encoding="utf-8", errors="replace")) if fp.is_file() else "-"))
        scratch = self.ws.scratch
        if scratch.is_dir():
            for fp in sorted(x for x in scratch.rglob("*") if x.is_file()):
                try:
                    blob.append(f"{fp.relative_to(scratch)}:{fp.stat().st_mode}:" + short_hash(fp.read_bytes().decode("utf-8", "replace")))
                except OSError:
                    blob.append(f"{fp}:?")
        return short_hash("\n".join(blob))


class Verifier:
    def __init__(self, workspace, executor: TestExecutor, ledger: Ledger, cfg: dict, tel=None, authored: set | None = None) -> None:
        self.ws = workspace
        self.ex = executor
        self.ledger = ledger
        self.cfg = cfg
        self.tel = tel
        self.authored = authored if authored is not None else set()
        self.env_hash = env_fingerprint()
        self.issue_literals: set = set()  # set by the controller from the issue text

    def verify(self, specs: list[TestSpec], explicit_tests: list[str] | None = None) -> VerificationReport:
        cand_hash = self.ws.tree()
        manifest_hash = short_hash("\n".join(f"{s.kind}:{s.command}" for s in specs))
        rep = VerificationReport(cand_hash, self.env_hash, manifest_hash, INCONCLUSIVE)
        changed = self.ws.changed(None, cand_hash)
        rep.changed_files = [f"{s} {p}" for s, p in changed]
        if not changed:
            rep.status = FAILED
            rep.blocking.append("empty patch: no changes relative to the base snapshot")
            rep.claims = {c: (UNKNOWN, "") for c in CLAIMS}
            rep.claims["issue_behavior"] = (CONTRADICTED, "no code change")
            self._record(rep, specs)
            return rep

        # 1) structural validity of every changed/added file
        struct_errors = []
        for st, path in changed:
            if st.startswith("D"):
                continue
            fp = self.ws.root / path
            try:
                text = fp.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError:
                continue
            err = syntax_error(fp, text)
            if err:
                base = self.ws.base_content(path)
                if base is None or not syntax_error(fp, base.decode("utf-8", "surrogateescape")):
                    struct_errors.append(f"{path}: {err[:200]}")
        rep.claims["structural_validity"] = (CONTRADICTED, "; ".join(struct_errors)) if struct_errors else (SUPPORTED, "all changed files parse")

        # 2) diff audit (scope, test weakening, secrets, artifacts)
        diff_text = self.ws.diff(None, cand_hash)
        scope_block = self._audit(diff_text, changed, rep)
        rep.claims["diff_scope"] = (CONTRADICTED, "; ".join(scope_block)) if scope_block else (SUPPORTED, "diff audit clean")

        # A contradicted structural/scope claim already makes VERIFIED impossible. Preserve the
        # blocking evidence and defer expensive candidate/base tests until the patch is repaired.
        if struct_errors or scope_block:
            rep.claims["issue_behavior"] = (UNKNOWN, "not checked while the patch has blocking structural or scope errors")
            rep.claims["no_regressions"] = (UNKNOWN, "not checked while the patch has blocking structural or scope errors")
            rep.skipped.append("test runs deferred until structural and diff audit blockers are fixed")
            rep.status = FAILED
            self._record(rep, specs)
            return rep

        # 3) tests: candidate vs base (base keeps the candidate's test files)
        keep = [p for st, p in changed if not st.startswith("D") and is_test_path(p)]
        results: list[SpecResult] = []
        for spec in specs:
            cand = self.ex.run(spec.command)
            if self.ws.tree() != cand_hash:  # tests must not change sources; restore exact candidate
                rep.notes.append(f"`{spec.command[:60]}` modified tracked files; candidate restored")
                self.ws.restore(cand_hash)
            if cand.launch_failed:
                rep.skipped.append(f"{spec.kind} `{spec.command[:80]}` could not be launched (exit {cand.exit_code}): {cand.output_tail.strip()[-160:]}")
                results.append(SpecResult(spec, None, cand, classify_transitions({}, {})))
                continue
            if cand.timed_out:
                rep.skipped.append(f"{spec.kind} `{spec.command[:80]}` timed out on candidate")
                results.append(SpecResult(spec, None, cand, classify_transitions({}, {})))
                continue
            base = self.ex.run_base(spec.command, keep)
            if base.launch_failed:
                rep.skipped.append(f"{spec.kind} `{spec.command[:80]}` could not be launched on base (exit {base.exit_code})")
                results.append(SpecResult(spec, base, cand, classify_transitions({}, {})))
                continue
            if base.timed_out:
                rep.skipped.append(f"{spec.kind} `{spec.command[:80]}` timed out on base")
                results.append(SpecResult(spec, base, cand, classify_transitions({}, {})))
                continue
            tr = classify_transitions(base.outcomes, cand.outcomes)
            flaky = []
            if tr["regressed"] and int(self.cfg.get("flaky_reruns", 1)) > 0:
                again = self.ex.run(spec.command)
                for tid in list(tr["regressed"]):
                    if again.outcomes.get(tid) == PASS:
                        tr["regressed"].remove(tid)
                        flaky.append(tid)
                if flaky:
                    rep.notes.append(f"flaky (inconsistent across reruns, excluded): {', '.join(flaky[:5])}")
            results.append(SpecResult(spec, base, cand, tr, flaky))
            if self.tel:
                self.tel.event(
                    "verify_spec", kind=spec.kind, command=spec.command, base_counts=base.counts(), cand_counts=cand.counts(),
                    fixed=len(tr["fixed"]), regressed=len(tr["regressed"]), log_ids=[base.log_id, cand.log_id],
                )
        rep.results = [
            {
                "kind": r.spec.kind,
                "command": r.spec.command,
                "reason": r.spec.reason,
                "base": r.base.to_dict() if r.base else None,
                "candidate": r.cand.to_dict(),
                "transitions": {k: v[:50] for k, v in r.transitions.items() if v and k != "still_passing"},
                "still_passing_count": len(r.transitions.get("still_passing", [])),
                "flaky": r.flaky,
            }
            for r in results
        ]
        rep.fixed_tests = sorted({t for r in results for t in r.transitions.get("fixed", [])})
        # Exploratory scratch scripts (coarse, auto-collected) cannot establish regressions.
        rep.regressions = sorted({
            t for r in results if not (r.spec.kind == "observed" and r.cand.coarse) for t in r.transitions.get("regressed", [])
        })
        for r in results:
            if r.spec.kind == "observed" and r.cand.coarse and r.transitions.get("regressed"):
                rep.notes.append(f"scratch script `{r.spec.command[:60]}` passed on base but fails on candidate (ignored)")

        rep.claims["issue_behavior"] = self._issue_claim(results, explicit_tests or [], rep)
        rep.claims["no_regressions"] = self._regression_claim(results, rep)

        statuses = [rep.claims[c][0] for c in CLAIMS]
        if CONTRADICTED in statuses:
            rep.status = FAILED
        elif all(s == SUPPORTED for s in statuses):
            rep.status = VERIFIED
        else:
            rep.status = INCONCLUSIVE
        # Evidence must describe the exact candidate we are about to report.
        if self.ws.tree() != cand_hash:
            rep.status = INCONCLUSIVE
            rep.blocking.append("workspace changed during verification; evidence is stale")
        self._record(rep, specs)
        return rep

    # ---- claims ------------------------------------------------------------------------------
    def _issue_claim(self, results: list[SpecResult], explicit: list[str], rep: VerificationReport):
        issue_runs = [r for r in results if r.spec.kind in ("target", "repro")]
        targets = [r for r in results if r.spec.kind in ("target", "repro") and r.base is not None]
        cand_outcomes = [(tid, status) for r in results if r.spec.kind in ("target", "repro")
                         for tid, status in r.cand.outcomes.items()]
        failing_explicit = [t for t in explicit for k, v in cand_outcomes
                            if test_id_matches(t, k) and v in FAILLIKE]
        if failing_explicit:
            return (CONTRADICTED, f"declared target tests still fail: {', '.join(sorted(set(failing_explicit))[:5])}")
        missing_explicit = [t for t in explicit if not any(test_id_matches(t, k) for k, _ in cand_outcomes)]
        if missing_explicit:
            return (UNKNOWN, f"declared tests not found in candidate output: {', '.join(missing_explicit[:5])}")
        skipped_explicit = [t for t in explicit for k, v in cand_outcomes
                            if test_id_matches(t, k) and v == SKIP]
        if skipped_explicit:
            return (UNKNOWN, f"declared tests were skipped: {', '.join(sorted(set(skipped_explicit))[:5])}")
        incomplete = [r.spec.command for r in issue_runs if self._incomplete(r)]
        if incomplete:
            return (UNKNOWN, "issue-specific check did not complete on both versions: " + ", ".join(c[:80] for c in incomplete[:3]))
        unparsed_failure = [r.spec.command for r in issue_runs if self._unparsed_failure(r)]
        if unparsed_failure:
            return (UNKNOWN, "issue-specific check exited nonzero with no reported failing test: "
                    + ", ".join(c[:80] for c in unparsed_failure[:3]))
        if not targets:
            # Evidence gathered during the work (tests the agent ran, related/new tests) may SUPPORT the
            # claim through identity-level fail->pass transitions, but can never contradict it.
            aux = [r for r in results if r.spec.kind in ("observed", "related", "full") and r.base is not None]
            fixed = sorted({t for r in aux for t in r.transitions["fixed"]})
            if fixed:
                weak = all(r.cand.coarse for r in aux if r.transitions["fixed"])
                return (SUPPORTED, f"{len(fixed)} test(s) fail on base and pass on candidate: {', '.join(fixed[:5])}"
                        + (" (coarse: exit-status only)" if weak else ""))
            rep.skipped.append("no issue-specific test or reproducer demonstrated the fix")
            return (UNKNOWN, "no test or reproducer fails on the original code and passes on the candidate")
        fixed = [t for r in targets for t in r.transitions["fixed"]]
        target_fail = [t for r in targets for t in (r.transitions["still_failing"] + r.transitions["new_fail"])]
        target_regr = [t for r in targets for t in r.transitions["regressed"]]
        siblings = self._failing_siblings(results, explicit)
        if fixed and not target_regr and siblings:
            return (UNKNOWN, "tests in the same file as the requested test(s) still fail: " + ", ".join(siblings[:6])
                    + " - they most likely belong to the same change")
        if fixed and not target_regr and target_fail:
            # A target command is a claim about the issue, not a vote among tests. A single
            # fail->pass transition cannot establish that claim while the same target run
            # still has unresolved failures. They may be pre-existing, so ask for narrower
            # evidence instead of calling the patch wrong.
            return (UNKNOWN, "target run still has failing tests: " + ", ".join(sorted(set(target_fail))[:5])
                    + "; isolate the issue-specific targets or resolve the remaining failures")
        if fixed and not target_regr:
            note = f"{len(fixed)} target test(s) fail on base and pass on candidate"
            if all(r.cand.coarse for r in targets if r.transitions["fixed"]):
                note += " (coarse: exit-status only)"
            return (SUPPORTED, note)
        if target_fail or target_regr:
            ex = next((r.cand.failure_excerpt for r in targets if r.cand.failing()), "")
            return (CONTRADICTED, f"target tests fail on candidate: {', '.join((target_fail + target_regr)[:5])}" + (f"\n{ex[:600]}" if ex else ""))
        if any(r.transitions["new_pass"] for r in targets):
            return (UNKNOWN, "target tests pass on candidate but did not exist/run on base, so the fix is not demonstrated")
        passing_on_base = [t for r in targets for t in r.transitions["still_passing"]]
        if passing_on_base:
            return (UNKNOWN, "target tests already pass on the base code: they do not reproduce the issue")
        return (UNKNOWN, "target tests produced no comparable outcomes")

    @staticmethod
    def _failing_siblings(results: list[SpecResult], explicit: list[str]) -> list:
        """Tests that live next to an explicitly requested test (same file/class) and fail before AND after."""
        def group(tid: str) -> str:
            tid = canonical_test_id(tid)
            return re.sub(r"\[.*\]$", "", tid).rsplit(".", 1)[0]

        wanted = {group(t) for t in explicit}
        if not wanted:
            return []
        out = set()
        for r in results:
            if r.base is None:
                continue
            for tid in r.transitions.get("still_failing", []):
                if group(tid) in wanted and not any(test_id_matches(t, tid) for t in explicit):
                    out.add(tid)
        return sorted(out)

    def _regression_claim(self, results: list[SpecResult], rep: VerificationReport):
        compared = [r for r in results if r.base is not None and not r.cand.timed_out]
        if rep.regressions:
            return (CONTRADICTED, f"{len(rep.regressions)} test(s) passed on base and fail on candidate")
        new_coll = [r.spec.command for r in compared if r.cand.collection_error and not r.base.collection_error]
        if new_coll:
            return (CONTRADICTED, f"new test collection/import errors in: {', '.join(c[:60] for c in new_coll[:3])}")
        broad_incomplete = [r.spec.command for r in results if r.spec.kind in ("related", "full") and self._incomplete(r)]
        if broad_incomplete:
            return (UNKNOWN, "planned regression check did not complete on both versions: "
                    + ", ".join(c[:80] for c in broad_incomplete[:3]))
        broad_unparsed = [r.spec.command for r in results if r.spec.kind in ("related", "full") and self._unparsed_failure(r)]
        if broad_unparsed:
            return (UNKNOWN, "regression check exited nonzero with no reported failing test: "
                    + ", ".join(c[:80] for c in broad_unparsed[:3]))
        disappeared = [t for r in compared for t in r.transitions["disappeared"] if r.base.outcomes.get(t) == PASS]
        if disappeared:
            rep.notes.append(f"tests that passed on base are missing on candidate: {', '.join(disappeared[:5])}")
        to_skip = [t for r in compared for t in r.transitions["to_skip"]]
        if to_skip:
            rep.notes.append(f"FAIL/PASS -> SKIP is not a fix: {', '.join(to_skip[:5])}")
        broad = [
            r for r in compared
            if r.spec.kind in ("related", "full") and not r.cand.coarse and not r.base.coarse and not r.base.timed_out
            and not r.base.collection_error and r.transitions["still_passing"]
        ]
        if not compared:
            rep.skipped.append("no test run completed on both base and candidate")
            return (UNKNOWN, "no comparable test runs")
        if disappeared:
            return (UNKNOWN, "some previously passing tests did not run on the candidate")
        passing = sum(len(r.transitions["still_passing"]) for r in compared if not r.base.timed_out)
        if passing == 0:
            return (UNKNOWN, "no previously-passing tests were exercised; regression risk unmeasured")
        if not broad:
            rep.notes.append("regression check limited to target specs (no related test files were identified)")
        return (SUPPORTED, f"{passing} previously passing test(s) still pass; 0 regressions")

    @staticmethod
    def _incomplete(result: SpecResult) -> bool:
        return (result.base is None or result.base.timed_out or result.base.launch_failed
                or result.cand.timed_out or result.cand.launch_failed)

    @staticmethod
    def _unparsed_failure(result: SpecResult) -> bool:
        return (result.cand.exit_code not in (None, 0) and bool(result.cand.outcomes)
                and not result.cand.failing())

    # ---- audit -------------------------------------------------------------------------------
    def _audit(self, diff_text: str, changed: list[tuple[str, str]], rep: VerificationReport) -> list[str]:
        block = []
        if REDACT.contains_secret(diff_text):
            block.append("the diff contains a credential value")
        per_file: dict[str, dict] = {}
        cur = None
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
                cur = m.group(2) if m else None
                per_file[cur] = {"add": 0, "del": 0, "assert_add": 0, "assert_del": 0, "binary": False}
                continue
            if cur is None:
                continue
            st = per_file[cur]
            if line.startswith("GIT binary patch") or line.startswith("Binary files"):
                st["binary"] = True
            elif line.startswith("+") and not line.startswith("+++"):
                st["add"] += 1
                st["assert_add"] += bool(_ASSERT_RE.search(line))
                if not is_test_path(cur) and self.issue_literals:
                    hits = special_cased_literals(line[1:], self.issue_literals)
                    if len(hits) >= 2:
                        block.append(f"{cur}: special-cases values copied from the issue example ({', '.join(hits[:4])}) "
                                     "instead of implementing the general behaviour")
            elif line.startswith("-") and not line.startswith("---"):
                st["del"] += 1
                st["assert_del"] += bool(_ASSERT_RE.search(line))
        status = {p: s for s, p in changed}
        for path, st in per_file.items():
            if path is None:
                continue
            if is_test_path(path) and status.get(path, "").startswith("M") and st["assert_del"] > st["assert_add"]:
                block.append(f"{path}: removes more assertions than it adds (possible test weakening)")
            if is_test_path(path) and status.get(path, "").startswith("D"):
                block.append(f"{path}: deletes a test file")
            if st["binary"]:
                rep.notes.append(f"{path}: binary change")
        modified_tests = [p for s, p in changed if s.startswith("M") and is_test_path(p)]
        if modified_tests:
            rep.notes.append(f"existing test files modified: {', '.join(modified_tests[:5])}")
        not_authored = [p for s, p in changed if s.startswith("A") and p not in self.authored]
        if not_authored:
            rep.notes.append(f"files created by commands rather than the editor: {', '.join(not_authored[:6])}")
        total = sum(st["add"] + st["del"] for st in per_file.values() if st)
        if total > 1500:
            rep.notes.append(f"large diff ({total} changed lines); check for unrelated changes")
        return block

    def _record(self, rep: VerificationReport, specs: list[TestSpec]) -> None:
        for claim, (st, why) in rep.claims.items():
            self.ledger.add(
                EvidenceItem(
                    evidence_id=new_id("ev"),
                    claim_id=claim,
                    status=st,
                    candidate_hash=rep.candidate_hash,
                    environment_hash=rep.environment_hash,
                    test_manifest_hash=rep.test_manifest_hash,
                    commands=[s.command for s in specs],
                    test_ids=(rep.fixed_tests + rep.regressions)[:50],
                    detail=why[:1000],
                    log_ids=[x for r in rep.results for x in ((r.get("base") or {}).get("log_id"), r["candidate"].get("log_id")) if x],
                )
            )
