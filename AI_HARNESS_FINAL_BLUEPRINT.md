# AI Coding Harness Hackathon — Final Unified Architecture, Research & Execution Blueprint

**Status:** Final merged planning/reference document; not implementation.  
**Primary users:** Human team + Claude Code.  
**Companion operating file:** `CLAUDE.md`.  
**Source basis:** Merged from `AI_Harness_1 (1).md` and `AI_Harness_2.md`, preserving the first document's research depth/breadth/extensibility and the second document's hackathon focus, interface precision, evidence design, anti-loop control, and implementation readiness.  
**Research snapshot represented by the source material:** 2026-09-26.

---

# 0. Executive Decision

We are not building “the most complicated coding agent.”

We are building the **smallest, strongest, evidence-gated, model-portable software-engineering harness** that can reliably turn the organizer-prescribed text model into an effective software engineer.

The governing optimization order is:

1. **Correctness / issue resolution**
2. **Evidence / verification**
3. **Reliability / recovery**
4. **Efficiency — model calls, tokens, time, compute**
5. **Reproducibility / environment independence**
6. **Interface clarity / implementation reliability**
7. **Maintainability / extensibility**
8. **Research sophistication only when it improves the above**

The selected architecture is:

> **Adaptive Evidence-Gated Controller = deterministic outer state machine + bounded evidence-driven loops + one adaptive coding agent + deterministic specialist services + external verification.**

The core lifecycle is:

```text
ORIENT
  -> LOCATE
  -> REPRODUCE
  -> EDIT
  -> CHECK
  -> FINAL_VERIFY
  -> DONE
```

Failure path:

```text
FAILURE
  -> CLASSIFY
  -> EXTRACT NEW EVIDENCE
  -> INVALIDATE WRONG ASSUMPTIONS
  -> CHANGE STRATEGY
  -> RETURN TO THE CORRECT PRIOR STATE
```

The LLM chooses useful actions **inside** a controlled phase. It does not own final truth, budgets, lifecycle legality, or the `VERIFIED` status.

---

# 1. Why This Merge Is Better

The two source documents had complementary strengths. The final design deliberately optimizes every review dimension rather than simply concatenating them.

| Review dimension | What the final blueprint keeps/adds |
|---|---|
| Research depth | Detailed papers, repositories, blogs, memory/RAG/tool references from AI_Harness_1 |
| Breadth of ideas | Skills, context, RAG, memory, research sidecar, ingestion, alternative architectures |
| Hackathon focus | Core/Conditional/Experiment tiers; official runtime contract; no runtime dependency bloat |
| Architectural coherence | One Adaptive Evidence-Gated Controller and one shared state/evidence model |
| Implementation readiness | Canonical interfaces, module contracts, hashes, statuses, acceptance gates, build order |
| Coding-agent friendliness | Exact lifecycle, tool schemas, retry rules, state fields, prompt compiler, decision rules |
| Interface clarity | `TaskState`, `ContextPack`, `Observation`, `VerificationReport`, `FailurePacket`, IDs/hashes |
| Verification design | Baselines, exact test identities, patch-hash-bound evidence ledger, VERIFIED/FAILED/INCONCLUSIVE |
| Avoids overengineering | Component-entry gate + paired ablations + explicit “do not build initially” list |
| Future extensibility | Provider adapters, retrieval adapters, skill format, executor interface, optional advanced modules |

**Design rule:** preserve the broad research library, but keep only the high-return ideas in the runtime core.

---

# 2. Project Objective and Competition Contract

## 2.1 Objective

Given:
- a repository,
- a software-engineering issue/test case,
- the prescribed text-only foundation model,
- the competition runtime/testing environment,

the harness should:

1. Understand the requested behavior.
2. Convert the issue into explicit acceptance claims.
3. Orient itself in an unfamiliar repository.
4. Find the relevant files/symbols/tests.
5. Form hypotheses and gather evidence.
6. Make the smallest justified change.
7. Execute tests/checks.
8. Recover intelligently from failures.
9. Produce a patch with an honest evidence report.
10. Stop only when external evidence supports the claimed result or when the budget is exhausted.

## 2.2 Non-negotiable competition requirements

From the supplied hackathon material:

- Root `Makefile` required.
- `make setup` installs/configures what is required.
- `make run` launches the harness.
- `make test` exposes the team test/evaluation procedure where applicable.
- `make clean` may remove generated artifacts.
- At minimum `make setup` and `make run` must work.
- Runtime credential is supplied as `AI_API_KEY`.
- Never hard-code/commit credentials.
- Evaluation is text-only.
- The organizers may prescribe the model/model family.
- Evaluators should not need to alter source/configuration.
- Dependencies must be declared and setup must be reproducible.
- Randomness/configuration affecting output should be controlled/documented.
- Evaluation may supply a repository/issue and test infrastructure after launch.

## 2.3 Critical architectural consequence

The submission must remain **model portable**.

Claude Code, Claude Skills, Claude Agent SDK, MCP, web research, external memory platforms, embeddings services, or any provider-specific API may be excellent development references, but they must not become required competition-runtime dependencies unless the official evaluator supports them.

---

# 3. Evidence Convention

Every architecture idea must be labeled mentally or in design notes as one of:

### CORE
Implement in the first serious harness because it directly supports correctness, verification, control, or reproducibility.

### CONDITIONAL
Implement only when repository/model/task/budget characteristics justify it **and** a paired evaluation shows benefit.

### EXPERIMENT
A local hypothesis. It must have a baseline, success metric, and rollback plan.

### RESEARCH REFERENCE
Useful idea or paper, not automatically a component.

Historical benchmark scores from different papers/models are **not directly comparable** and must not be treated as expected hackathon gains.

---

# 4. Decided Architecture — Adaptive Evidence-Gated Controller

```text
issue + repo + evaluator contract
          |
          v
+------------------------+
| BOOTSTRAP / ORIENT     |
| env, repo, test hints  |
+-----------+------------+
            |
            v
+------------------------+
| ROUTE STRATEGY         |
| DIRECT / LIGHT_PLAN /  |
| STRUCTURED             |
+-----------+------------+
            |
            v
+------------------------------------------------------+
| ADAPTIVE EVIDENCE-GATED CONTROLLER                  |
|                                                      |
| ORIENT -> LOCATE -> REPRODUCE -> EDIT -> CHECK      |
|    ^        ^           ^          |        |         |
|    |        |           |          v        v         |
|    +--------+-----------+------ RECOVER <- failure   |
|                                      |               |
|                                      v               |
|                                 FINAL_VERIFY         |
+--------------------------------------+---------------+
                                       |
                      +----------------+----------------+
                      |                                 |
                      v                                 v
                  VERIFIED                     FAILED/INCONCLUSIVE
```

The controller owns:
- phase,
- strategy,
- budgets,
- loop detection,
- attempt count,
- state transitions,
- completion legality.

The model owns:
- the next useful reasoning/action choice inside the current phase,
- hypotheses,
- local plans,
- edit intent.

Repository intelligence owns:
- orientation,
- localization,
- ranked source context.

Tool/runtime owns:
- validated execution,
- edits,
- snapshots,
- logs,
- workspace hashes.

Verifier owns:
- test/evidence policy,
- baseline comparisons,
- claim status,
- final completion recommendation.

---

# 5. Looping — Yes, but Only Evidence-Driven Bounded Looping

Looping is **good and necessary** for this hackathon because software engineering is iterative.

The correct loop is:

```text
ACT
 -> OBSERVE
 -> LEARN SOMETHING NEW
 -> UPDATE STATE/HYPOTHESIS
 -> ACT DIFFERENTLY OR WITH BETTER INFORMATION
```

The wrong loop is:

```text
TRY
 -> FAIL
 -> SAME TRY
 -> FAIL
 -> SAME TRY
 -> ...
```

## 5.1 Four useful loops

### A. Outer lifecycle loop
Controls movement between phases.

```text
LOCATE -> EDIT -> CHECK
  ^                 |
  |                 v
  +---- RECOVER <---+
```

### B. Inner action loop
Within one phase:

```text
observe -> choose next action -> execute -> update state
```

### C. Retrieval loop
Useful when newly discovered symbols/errors improve the next search.

```text
issue
 -> retrieve
 -> discover symbol/error
 -> improve query
 -> retrieve again
```

### D. Verification/recovery loop

```text
patch
 -> targeted check
 -> fail
 -> classify
 -> revise hypothesis or edit
 -> check again
```

## 5.2 Progress definition

A loop iteration counts as progress only if it creates at least one of:

- new verified fact,
- new relevant file/symbol,
- new test outcome,
- changed hypothesis,
- changed diff/workspace hash,
- eliminated hypothesis,
- new failure signature,
- new dependency/caller/test relationship,
- meaningful reduction of an unresolved uncertainty.

If none occurs, increment `no_progress_count`.

## 5.3 Action and observation fingerprints

Track:

```text
ActionFingerprint {
  action_type,
  target,
  normalized_arguments
}

ObservationFingerprint {
  exit_code,
  error_or_failure_signature,
  changed_paths,
  workspace_hash,
  diff_hash,
  test_transition_signature
}
```

Detect:
- identical action-result repetition,
- A/B/A/B alternating cycles,
- repeated edit/revert of the same hunk,
- repeated search with the same query/scope and no new results,
- repeated model call with unchanged state,
- multiple steps producing no new evidence.

## 5.4 Anti-loop policy

Suggested default semantics:

```text
first same failure:
    inspect evidence more deeply

second same failure signature:
    challenge the active hypothesis / broaden context

third same failure signature:
    abandon current strategy and return to an earlier phase

continued no-progress:
    stop INCONCLUSIVE if no distinct strategy remains
```

Exact counts are configuration, not universal truths. Tune them empirically.

## 5.5 Loop budgets

Controller tracks:

```text
BudgetState {
  max_wall_ms,
  max_model_calls,
  max_steps,
  max_tokens_or_cost_if_available,
  max_same_failure_retries,
  max_no_progress_steps,
  verification_reserve
}
```

Do not consume the entire budget exploring and leave nothing for final verification.

The exact exploration/verification split is an **Experiment**; begin with a configurable reserve rather than hard-coding 80/20.

---

# 6. Adaptive Strategy Routing

After cheap orientation, route the task based on observable complexity.

## DIRECT

Use when:
- localization confidence is high,
- likely one/few local edits,
- behavior/test is clear,
- no cross-component propagation.

Flow:

```text
inspect -> edit -> targeted check -> final verify
```

No separate planning call required.

## LIGHT_PLAN

Use when:
- several related changes,
- moderate uncertainty,
- multiple dependent files/tests.

Store a small 3–5 step plan with:
- target,
- dependency,
- required evidence.

## STRUCTURED

Use when:
- API/schema propagation,
- cross-component behavior,
- uncertain location,
- repeated failures,
- high regression risk,
- ambiguous specification.

Add:
- explicit acceptance claims,
- hypothesis ledger,
- checkpoints,
- wider context/test policy,
- optional fresh-context review.

### Important

Route **upward when evidence demands it**.

Do not allow an early complexity score to make an easy discovered fix artificially complicated.

---

# 7. Shared State and Evidence Contract

The best feature from the implementation-oriented source is one **canonical state/evidence contract** shared across all modules.

Use synchronous, in-process dataclass/JSON interfaces first. Avoid distributed orchestration in V1.

## 7.1 Common IDs

Every cross-module object should carry relevant IDs:

- `task_id`
- `workspace_id`
- `call_id`
- `candidate_id`
- `snapshot_id`
- `evidence_id`
- `log_id`
- `base_revision`
- `workspace_hash`

Paths are always repository-relative.

Source spans include:
- file hash,
- 1-based inclusive line range,
- optional symbol identity.

Large outputs are immutable artifacts referenced by ID; model-facing messages contain bounded excerpts.

## 7.2 TaskState

```text
TaskState {
  task_id,
  original_issue,
  objective,
  explicit_requirements[],
  inferred_requirements[],
  acceptance_claims[],
  constraints[],
  phase,
  strategy,

  plan_steps[],
  active_hypothesis,
  hypotheses[],
  confirmed_facts[],
  unknowns[],
  failed_hypotheses[],

  relevant_files[],
  changed_files[],
  base_revision,
  workspace_hash,

  action_history[],
  failure_history[],
  evidence_ids[],

  model_calls_used,
  tokens_used,
  elapsed_ms,
  verification_reserve,
  verification_status
}
```

Rules:
- `original_issue` is immutable.
- Inferences remain explicitly separate from requirements.
- Model interpretations remain hypotheses until supported by tools/tests/code observations.
- Editing a file invalidates stale source spans for that file.
- Editing after a green test makes patch-specific green evidence stale.

## 7.3 Fact

```text
Fact {
  statement,
  source_id,
  source_kind,
  file_hash?,
  confidence,
  created_at,
  last_verified_at?
}
```

Only observable evidence may create confirmed facts.

## 7.4 Hypothesis

```text
Hypothesis {
  claim,
  predicted_observation,
  evidence_for[],
  evidence_against[],
  status,
  attempts
}
```

A useful hypothesis predicts something checkable.

## 7.5 PlanStep

```text
PlanStep {
  goal,
  depends_on[],
  target_paths_or_symbols[],
  required_evidence[],
  status
}
```

---

# 8. Canonical Module Interfaces

These interfaces make the architecture coding-agent-friendly and replaceable.

```text
RepoContext.orient(
    issue,
    workspace_hash,
    token_budget
) -> ContextPack

RepoContext.retrieve(
    information_goal,
    evidence,
    token_budget
) -> ContextPack

RepoContext.invalidate(
    changed_paths,
    workspace_hash
) -> None
```

```text
ToolRuntime.call(
    tool_name,
    validated_args,
    workspace_id
) -> Observation

ToolRuntime.snapshot(
    reason
) -> SnapshotId

ToolRuntime.restore(
    snapshot_id,
    expected_workspace_hash
) -> Observation
```

```text
Verifier.capture_baseline(
    test_plan,
    base_revision,
    environment_hash
) -> Baseline

Verifier.verify(
    candidate_hash,
    acceptance_claims,
    policy,
    budget
) -> VerificationReport

Verifier.classify_failure(
    report
) -> FailurePacket
```

## ContextPack

```text
ContextPack {
  snippets[],
  repo_map_excerpt?,
  linked_tests[],
  unresolved_questions[],
  token_count,
  workspace_hash
}
```

Each snippet includes:
- path,
- line range,
- file hash,
- symbol,
- relevance reason,
- score explanation.

## Observation

```text
Observation {
  call_id,
  success,
  error_code?,
  exit_code?,
  timeout?,
  signal?,
  stdout_excerpt,
  stderr_excerpt,
  log_id?,
  duration_ms,
  changed_paths[],
  workspace_hash,
  truncated?,
  output_lost?
}
```

## VerificationReport

```text
VerificationReport {
  candidate_hash,
  environment_hash,
  test_manifest_hash,
  claim_statuses[],
  test_transitions[],
  mandatory_checks[],
  skipped_checks[],
  blocking_findings[],
  recommended_status
}
```

Allowed recommendation:
- `VERIFIED`
- `FAILED`
- `INCONCLUSIVE`

The controller must confirm that `candidate_hash` equals the patch being submitted.

---

# 9. Capability Map and Priority

| Capability | Tier | V1 decision |
|---|---|---|
| Competition/config adapter | CORE | Build immediately |
| Model adapter | CORE | Provider-neutral |
| Controller / bounded loops | CORE | Build immediately |
| Task contract/state | CORE | Build immediately |
| Sandbox/runtime | CORE | Build immediately |
| Lexical repo search | CORE | Build immediately |
| Bounded file reads | CORE | Build immediately |
| Transactional edits | CORE | Build immediately |
| Test execution | CORE | Build immediately |
| Evidence ledger | CORE | Build early |
| Verification gate | CORE | Build early |
| Loop detector | CORE | Build early |
| Repo map | CORE/Conditional by repo size | Add after baseline |
| Tree-sitter symbols | CONDITIONAL but high-value | Add for dominant languages |
| Skills | CORE after baseline stabilizes | Add modularly |
| Lightweight SQLite memory | CORE after state works | Keep simple |
| Dense code embeddings | CONDITIONAL | Only after retrieval eval |
| Neural reranking | CONDITIONAL | Only after retrieval eval |
| Generated reproducer | CONDITIONAL | Must validate oracle |
| Fresh-context reviewer | EXPERIMENT | After deterministic checks |
| Parallel read-only diagnosis | EXPERIMENT | Hard tasks only |
| MCP | CONDITIONAL | External integrations only |
| Mem0 / Graphiti | CONDITIONAL | Only if memory bottleneck measured |
| ToT / LATS | EXPERIMENT | Rare hard-task escalation |
| Runtime web research | CONDITIONAL | Only if explicitly allowed |
| Research/document sidecar | DEVELOPMENT | Useful for team, not required core |

---

# 10. Competition / Configuration Adapter

## Goal
Separate evaluator/environment details from the agent.

```text
RuntimeConfig {
  api_key_env = "AI_API_KEY",
  model,
  repo_root,
  task_source,
  max_wall_ms,
  max_agent_steps,
  max_tool_output,
  network_policy,
  random_seed?,
  executor_mode
}
```

Best practices:
- fail fast on missing API configuration,
- never print the key,
- keep model-provider behavior behind an adapter,
- pass the key only to the model client unless a repository task explicitly needs it,
- do not leak the model credential into arbitrary build/test subprocesses,
- clean-environment smoke test every release.

---

# 11. Repository Intelligence and Localization

This area combines the research depth of the first source with the focused retrieval rules of the second.

## 11.1 CORE — cheap orientation

Create a manifest of useful files and exclude:
- binary,
- large generated artifacts,
- vendor dependencies,
- caches,
- build output.

Detect:
- languages,
- build/test systems,
- likely source roots,
- test roots,
- package manifests.

Extract direct issue anchors:
- quoted strings,
- file paths,
- symbols,
- exceptions,
- stack frames,
- test names,
- API names.

## 11.2 CORE — lexical search

Use bounded:
- filename search,
- `rg`,
- FTS/BM25 where useful.

Rank strongly:
1. exact stack path,
2. issue-named file,
3. exact symbol,
4. failing test,
5. exact error string,
6. callers/references,
7. weaker semantic similarity.

Return previews/counts first; read exact spans only as needed.

## 11.3 CONDITIONAL — Tree-sitter / AST index

For dominant supported languages, cache:
- definitions,
- class/function bounds,
- imports,
- references where reliable.

Fail open to lexical search when grammar/parsing fails.

Persist by content hash so unchanged files are not re-indexed.

## 11.4 CORE/CONDITIONAL — compact repository map

Aider's RepoMap idea is useful:
- show top-level structure,
- key signatures,
- important symbols,
- stay within a token budget.

Do not treat repo-map ranking as ground truth.

## 11.5 Iterative localization

Use the RepoCoder-style principle:

```text
issue
 -> search
 -> read
 -> discover identifier/error
 -> improve search
 -> read
 -> ...
```

Every search must answer a named uncertainty.

Suppress duplicate query/scope attempts that produced no new evidence.

## 11.6 Relation expansion

Add only cheap, provenance-labeled one-hop relations initially:
- import,
- definition/reference,
- test/source association,
- caller/callee when reliable.

Do **not** build a perfect cross-language knowledge graph at startup.

## 11.7 Optional semantic retrieval

Add embeddings only when lexical+structural retrieval misses relevant code on held-out tasks.

Possible later:
- hybrid lexical+dense,
- cross-encoder reranker,
- ColBERT-like late interaction.

### Metrics
- file Recall@1/3/5,
- symbol Recall@k,
- MRR,
- tokens-to-first-relevant-region,
- relevant-symbols per 1k context tokens,
- end-to-end resolution delta.

---

# 12. Context Engineering

The goal is the **smallest high-signal context for the next action**, not the largest possible context.

## 12.1 Context frame

```text
PINNED
- original issue
- constraints
- acceptance claims

CURRENT STATE
- confirmed facts
- current hypothesis
- unknowns
- current plan
- budget

REPOSITORY STRUCTURE
- compact map / paths

ACTIVE SOURCE
- exact relevant spans only

RECENT EVIDENCE
- current failure/test/tool output

PROCEDURE
- relevant skill only

TOOLS
- schemas needed in current phase
```

## 12.2 Invalidation

When a file changes:
- invalidate snippets for old file hash,
- invalidate derived facts that depended on removed code where necessary,
- mark patch-specific prior test evidence stale.

When a candidate changes after tests:
- green test evidence for the old candidate cannot verify the new candidate.

## 12.3 Compaction

Before discarding old context:
1. extract durable fact/failure lesson,
2. attach source/log ID,
3. update TaskState/episode,
4. preserve large raw output outside active context,
5. then compact/remove.

## 12.4 Avoid “Lost in the Middle” behavior

Put the most action-critical source/evidence where the model can use it clearly.
Do not create giant mixed blocks of source, logs, docs, and instructions.

---

# 13. Skills / Procedural Memory

Use Agent-Skills-style progressive disclosure.

```text
skills/
  fault-localization/
    SKILL.md
    references/
    examples/
    scripts/
```

Load order:

```text
metadata
 -> activate relevant skill
 -> SKILL.md
 -> specific reference/example/script only when required
```

Recommended initial skills:

- `issue-triage`
- `repo-recon`
- `fault-localization`
- `reproduce-bug`
- `patch-code`
- `verify-change`
- `failure-recovery`
- `final-review`

Rules:
- one coherent procedure per skill,
- non-overlapping triggers,
- imperative steps,
- keep `SKILL.md` lean,
- move deep material to references,
- deterministic script for repetitive parsing/validation,
- skills contain procedures, not current-repo facts,
- portable skill loader if runtime is not Claude.

Metrics:
- trigger precision,
- trigger recall,
- downstream task-resolution delta,
- context tokens added,
- latency.

---

# 14. Model-Facing Tools and Runtime

Use a small **hybrid** interface.

A bash-only implementation remains a required baseline because mini-SWE-agent shows how effective a small shell surface can be, while SWE-agent motivates more tailored ACIs.

## 14.1 Canonical tool contracts

### `search_code`

```text
search_code(
  query,
  scope?,
  mode?,
  max_hits?
)
```

Returns:
- ranked bounded hits,
- path/line preview,
- total count,
- truncation flag,
- reason code.

### `read_file`

```text
read_file(
  path,
  start_line?,
  end_line?,
  expected_sha256?
)
```

Returns:
- numbered source,
- total lines,
- current hash,
- omitted-range flags.

Reject stale hashes and out-of-workspace paths.

### `edit_files`

Primary format:

```text
edit_files(
  edits = [
    {
      path,
      expected_file_hash,
      exact_old_text,
      new_text
    }
  ],
  creates?
)
```

Requirements:
- each old text matches exactly once,
- validate all edits before changing anything,
- apply transactionally,
- return changed hunks + new hashes.

### `run_command`

```text
run_command(
  command,
  cwd?,
  timeout?,
  output_budget?
)
```

Returns:
- real exit code,
- stdout/stderr separately,
- log ID,
- duration,
- changed-file manifest.

### `run_tests`

Runs verifier-registered or scoped test command in the same isolation.

Return structured test IDs/outcomes when parseable; raw bounded fallback otherwise.

### `repo_changes`

Returns:
- base revision,
- tracked/untracked status,
- diff stat,
- bounded diff,
- full final patch including new files.

### `read_log`

Allows bounded drill-down into immutable raw output.

## 14.2 Tool rules

- exact schema validation,
- stable error codes,
- no silent fuzzy edits,
- no path clamping,
- explicit cwd,
- timeout and process cleanup,
- model-facing output cap,
- raw logs stored outside agent-writable repo,
- compare workspace manifests around mutating commands.

---

# 15. Editing and Workspace Safety

## 15.1 Preferred edit path

```text
reread exact target
 -> formulate edit intent
 -> exact-match transaction or validated patch
 -> dry-run
 -> parse/syntax check
 -> apply
 -> diff inspection
```

Unified patch may be an alternate front-end if the prescribed model reliably generates it.

Whole-file rewrite is fallback for:
- small files,
- new files,
- cases where localized editing is more fragile.

## 15.2 Snapshots

Take private snapshots:
- before major repair attempts,
- before risky mutating commands,
- before broad dependency/config operations.

Restore only the snapshot's intended scope.

Avoid blindly using:
- `git reset --hard`
- `git clean`

in an unknown or initially dirty workspace.

Preserve the initial dirty state.

---

# 16. Reproduction and Testing

## 16.1 Capture baseline before editing

Record:
- base revision,
- environment fingerprint,
- test manifest,
- test commands,
- relevant test IDs/outcomes,
- known pre-existing failures.

When full-suite baseline is too expensive:
- baseline the most relevant comparison set,
- mark unobserved tests as unknown.

For feature work without a failing test:
- define explicit post-change acceptance examples instead of inventing a fake red baseline.

## 16.2 Progressive checks

After a material edit:
1. syntax/parse/build check if cheap,
2. issue-specific/target test,
3. nearby/affected tests,
4. broader static/build checks,
5. final risk-based regressions,
6. full suite if feasible or risk warrants.

## 16.3 Compare test identities, not counts

Good:

```text
test_x: FAIL -> PASS
test_y: PASS -> PASS
```

Bad evidence:

```text
47 failing -> 46 failing
```

unless the exact intended test changed and no passing test regressed.

Special cases:
- `FAIL -> SKIP` is not a fix.
- missing/unparseable test = unknown.
- parameterization changes require comparable IDs.
- flaky transitions require repeats.

---

# 17. Evidence Ledger and Verification

This is one of the most important merged improvements.

Use an append-only ledger:

```text
EvidenceItem {
  evidence_id,
  claim_id,
  status,              // SUPPORTED | CONTRADICTED | UNKNOWN | STALE
  candidate_hash,
  environment_hash,
  test_manifest_hash?,
  command?,
  test_ids[],
  baseline_outcomes?,
  candidate_outcomes?,
  source_ids[],
  timestamp,
  log_ids[]
}
```

Required claim categories:
- requested behavior,
- structural validity,
- observed regression preservation,
- mandatory build/static gates,
- diff scope/integrity,
- security/credential safety where applicable.

A reviewer/model opinion never becomes a confirmed ledger fact until validated.

## 17.1 Completion statuses

### VERIFIED

Requires:
- nonempty justified patch,
- current candidate hash matches evidence,
- all required claims supported,
- issue-specific behavior passes,
- red baseline fixed when comparable red baseline existed,
- required regression set shows no new failures,
- mandatory checks completed,
- diff contains no unexplained changes.

### FAILED

Observed counterevidence remains.

### INCONCLUSIVE

Required verification is unavailable because of:
- environment failure,
- timeout,
- untrustworthy oracle,
- exhausted budget,
- missing comparable test outcome,
- blocked dependency.

Return best patch + precise evidence gaps.

Never convert:
- `UNKNOWN`,
- skipped test,
- lower failure count,
- zero process exit alone,
- model confidence

into `VERIFIED`.

---

# 18. Failure Classification and Recovery

| Failure | First response | Escalation |
|---|---|---|
| Syntax/compile/type/lint in changed code | Fix exact diagnostic | Same diagnostic survives different edits |
| Target test fails | Inspect assertion/path | Same signature repeats / hypothesis falsified |
| New regression | Inspect diff/dependents | Shared API / several regressions |
| Wrong location | Search stack/callers/tests | Central premise contradicted |
| Patch apply error | Reread target/hash | Repeated editing mismatch |
| Timeout/tool/environment | Diagnose runner; bounded retry | Repeated infrastructure failure -> INCONCLUSIVE |
| No progress | Change code/information/strategy | No distinct strategy remains |

Failure memory stores one compact record per failed strategy:

```text
FailureRecord {
  hypothesis,
  predicted_observation,
  actual_result,
  relevant_logs_tests,
  forbidden_repeat,
  candidate_hash
}
```

Reflection after every step is unnecessary.

Generated reproducers are **Conditional**:
- useful only if the oracle is credible,
- failure on baseline alone does not prove the generated test captures the true issue.

Fresh-context reviewer is **Experiment**:
- invoke only after deterministic gates,
- ask for concrete counterexample/testable objection,
- validate every objection with code/tests.

---

# 19. Optimized Memory Architecture

Do not confuse memory with “saving the whole transcript.”

## 19.1 Working memory

Lifetime: current run.

Contains:
- phase,
- active hypothesis,
- plan,
- unresolved questions,
- evidence pointers,
- selected source,
- verification state,
- remaining budget.

## 19.2 Episodic memory

Attempts/sessions:

```text
Episode {
  task_id,
  attempt_id,
  hypothesis,
  actions,
  observations,
  failure_class,
  outcome,
  lesson?,
  evidence_ids,
  candidate_hash,
  repo_revision,
  timestamp
}
```

## 19.3 Semantic memory

Only verified repository facts:

```text
Fact {
  statement,
  provenance,
  repo_revision,
  file_hash?,
  confidence,
  created_at,
  last_verified_at,
  validity_status
}
```

Examples:
- verified test command,
- actual location of relevant parser,
- validated source/test association.

## 19.4 Procedural memory

Skills/workflows:
- stable,
- versioned,
- changed through eval-driven engineering,
- not automatically rewritten from a single failed task.

## 19.5 Long-term external knowledge

Research papers/docs/framework knowledge.

Keep separate from live repository state.

## 19.6 Memory write gate

```text
new information
 -> useful later?
     no -> discard
 -> source/tool backed?
     no -> hypothesis/episode only
 -> stable/reusable?
     no -> episode only
 -> dedupe + contradiction check
 -> write with provenance/version
```

## 19.7 Do not durably store

- secrets,
- raw hidden reasoning,
- unverified guesses as facts,
- huge raw logs,
- duplicate source text,
- entire files that are cheap to reread,
- line numbers without version/hash,
- stale transient failures after summarization.

## 19.8 V1 storage

SQLite + FTS5:

```text
task_state
episodes
facts
skills_index
sources
artifacts
```

Optional embeddings later.

Graphiti/Mem0 are **Conditional**, not V1 requirements.

---

# 20. Prompt / Instruction Architecture

Do not maintain one giant static prompt.

Compile each inference:

```text
SYSTEM INVARIANTS
+ TASK CONTRACT
+ CURRENT STATE
+ CURRENT PHASE
+ RELEVANT SKILL
+ ONLY REQUIRED TOOL SCHEMAS
+ SELECTED SOURCE CONTEXT
+ LATEST EVIDENCE
+ ACTION/OUTPUT CONTRACT
```

## Core system invariants

- Solve the supplied issue, not adjacent problems.
- Preserve original issue/constraints.
- Separate facts from hypotheses.
- Inspect before editing.
- Use tools as evidence.
- Prefer smallest justified change.
- Do not weaken tests to manufacture success.
- Re-plan when evidence contradicts the hypothesis.
- Repeated identical failure must change strategy.
- Never declare verified without current external evidence.
- Respect all budgets.
- Return INCONCLUSIVE rather than false success.

---

# 21. Research / Web Sidecar

This supports the **team's engineering work**, not necessarily competition runtime.

Workflow:

```text
research question
 -> freshness/scope
 -> independent facets if useful
 -> prefer primary sources
 -> capture source ledger
 -> extract supported claims
 -> identify contradictions/limitations
 -> follow second-order references only when material
 -> synthesize
 -> stop when marginal search yield is low
```

Parallelize independent research topics, not tightly coupled reasoning that requires one evolving shared state.

Source ledger:
- title,
- URL,
- author/org,
- publication date,
- retrieval date,
- source type,
- supported claim,
- limitation,
- direct/secondary.

---

# 22. Blog / Document Ingestion and Research RAG

## Web/blogs

Use Trafilatura-like extraction for:
- canonical URL,
- title/author/date,
- main body,
- headings,
- code blocks,
- links,
- retrieval time,
- content hash.

## Complex documents

Use Docling-style structured parsing for PDFs/Office/layout-heavy documents.

Apache Tika is a broad-format fallback.

## Research RAG

Separate this from source-code localization.

V1:

```text
BM25/FTS
+
dense embeddings
+
metadata filters
+
rank fusion
+
optional reranker
```

Advanced ColBERT/late-interaction only when corpus scale/quality measurements justify it.

Contextual Retrieval is valuable for prose/research documents but should not be blindly applied to every source-code chunk.

---

# 23. Integration Rules

## Strong integrations

- TaskState <-> controller
- controller <-> budgets/loop detector
- repo intelligence <-> localizer
- localizer <-> context manager
- context manager <-> skill router
- tools <-> telemetry
- edits <-> workspace hash
- verifier <-> evidence ledger
- verifier <-> recovery
- memory <-> provenance/version
- eval harness <-> telemetry

## Keep separate

### Code retrieval vs research RAG

Code:
- exact identifiers,
- AST,
- references,
- tests,
- execution evidence.

Research:
- prose semantics,
- metadata,
- citations,
- publication freshness.

### Skills vs semantic memory

Skills = procedures.  
Semantic memory = verified facts.

### Research web sidecar vs competition runtime

Internet availability cannot be assumed.

## Do not combine initially

- Mem0 + Graphiti + custom DB + vector DB
- ToT + LATS + multiple reviewers
- several full coding-agent frameworks
- huge MCP ecosystem
- many autonomous editing agents in one workspace
- vector-first source-code indexing
- permanent multi-agent team for ordinary fixes

---

# 24. Trade-Off Matrix

| Decision | Gain | Loss/risk | Cost/latency | Complexity | Use when | Avoid when |
|---|---|---|---|---|---|---|
| Deterministic state machine | Reliability/debuggability | Less free-form autonomy | Low | M | Always | — |
| Evidence-driven inner loop | Adaptive problem solving | Can wander without guardrails | M | M | Most tasks | Without loop detector |
| Tree-sitter | Structural precision | Language support | Low after index | M | Supported medium/large repo | Tiny/unsupported repo |
| Repo map | Whole-repo orientation | Extra context | Low | M | Large/broad task | Tiny repo |
| Dense code embeddings | Semantic recall | Index/staleness/noise | M | M | Lexical misses | Exact identifiers dominate |
| Reranker | Better top-k | Extra model/stage | M | M | Noisy candidates | Baseline already high |
| Skills | Modular procedures | Trigger/version maintenance | Low | M | Repeated workflows | Only trivial instruction set |
| Persistent memory | Reuse | Staleness/harm | L-M | M | Long/repeated tasks | Independent short tasks |
| Graph memory | Temporal relations | DB/ops | M | H | Evolving relationship queries | Simple repo facts |
| Multi-agent diagnosis | Search breadth | Coordination/tokens | High | H | High uncertainty | Ordinary bug |
| ToT/LATS | Deep search | Very high inference | Very high | H | Rare hard search | Default path |
| Architect/editor split | Editing reliability | Extra call | M-H | M | Editing syntax bottleneck | Direct edits work |
| Generated reproducer | New executable evidence | Wrong oracle risk | M | M | No existing test | Issue unclear/oracle weak |
| Fresh reviewer | Independent challenge | Correlated model error/cost | M-H | M | Risky green patch | Strong deterministic evidence |
| Full regression | Confidence | Runtime | High | L | Shared API/high risk | Huge suite/time limit |
| MCP | Standard interop | Protocol/deps | L-M | M | External tool ecosystem | Local 7-tool core |
| Docker | Isolation/reproducibility | Host compatibility | L-M | M | Supported evaluator | Nested-container restriction |
| Runtime web | External current info | Network/security/non-repro | High | M | Explicitly allowed and needed | Offline evaluation |

---

# 25. Alternative Architectures

## A. Recommended — Adaptive Evidence-Gated Harness

Best balance of:
- flexibility,
- implementation readiness,
- verification,
- cost,
- debugging,
- extensibility.

## B. Agentless deterministic pipeline

```text
localize -> repair candidates -> validate -> select
```

Use when:
- tasks are conventional,
- time/cost limits are strict,
- predictability matters more than exploratory debugging.

## C. Planner -> coder -> verifier

Use for:
- large cross-component changes,
- generous budget,
- complex dependency propagation.

Not the default.

## D. Parallel read-only hypothesis diagnosis

```text
planner
 -> diagnosis A
 -> diagnosis B
 -> diagnosis C
 -> evidence merge
 -> one editor
```

Use only:
- after baseline fails,
- when localization/hypothesis uncertainty is genuinely high.

Multiple workers must not concurrently edit the same workspace in V1.

---

# 26. Proposed Repository Structure

```text
project/
├── Makefile
├── README.md
├── CLAUDE.md
├── AI_HARNESS_FINAL_BLUEPRINT.md
├── pyproject.toml / requirements...
│
├── harness/
│   ├── config.py
│   ├── model/
│   │   ├── base.py
│   │   └── provider_adapter.py
│   ├── core/
│   │   ├── controller.py
│   │   ├── state.py
│   │   ├── budgets.py
│   │   ├── loops.py
│   │   └── completion.py
│   ├── repo/
│   │   ├── manifest.py
│   │   ├── search.py
│   │   ├── symbols.py
│   │   ├── repomap.py
│   │   └── context.py
│   ├── tools/
│   │   ├── schemas.py
│   │   ├── runtime.py
│   │   ├── editor.py
│   │   ├── command.py
│   │   └── logs.py
│   ├── verify/
│   │   ├── baseline.py
│   │   ├── testplan.py
│   │   ├── ledger.py
│   │   ├── verifier.py
│   │   └── failures.py
│   ├── memory/
│   │   ├── store.py
│   │   ├── episodes.py
│   │   └── facts.py
│   ├── skills/
│   │   └── loader.py
│   └── telemetry/
│       ├── events.py
│       └── metrics.py
│
├── skills/
│   ├── issue-triage/
│   ├── repo-recon/
│   ├── fault-localization/
│   ├── reproduce-bug/
│   ├── patch-code/
│   ├── verify-change/
│   ├── failure-recovery/
│   └── final-review/
│
├── evals/
│   ├── fixtures/
│   ├── tasks/
│   ├── graders/
│   ├── ablations/
│   └── reports/
│
└── research/
    ├── sources/
    ├── decisions/
    └── experiments/
```

The exact language/file layout is adjustable; interfaces matter more than names.

---

# 27. Build Sequence and Acceptance Gates

## Phase 0 — Submission skeleton

Deliver:
- root Makefile,
- provider-neutral model adapter,
- runtime config,
- API-key loading,
- evaluator I/O adapter,
- structured event log.

Acceptance:
- clean clone,
- `make setup`,
- `make test`,
- fixture-backed `make run`.

Do not add advanced retrieval or memory.

## Phase 1 — Minimal end-to-end baseline

Deliver:
- controller loop,
- bounded search/read,
- safe command execution,
- exact transactional edit,
- diff capture,
- one targeted test,
- final patch.

Acceptance:
- solve at least one known small fixture end-to-end,
- full trajectory recorded.

Benchmark immediately.

## Phase 2 — Evidence core

Deliver:
- base revision/environment hash,
- test manifest,
- evidence ledger,
- parsed test transitions,
- candidate hash binding,
- diff audit,
- timeouts,
- snapshots,
- VERIFIED/FAILED/INCONCLUSIVE.

Acceptance tests:
- stale evidence after edit,
- `FAIL -> SKIP`,
- new regression,
- runner timeout,
- dirty initial workspace,
- changed candidate after green test.

## Phase 3 — Adaptive controller and anti-looping

Deliver:
- DIRECT/LIGHT_PLAN/STRUCTURED routing,
- action/observation fingerprints,
- no-progress detector,
- bounded retry policy,
- verification reserve.

Acceptance:
- repeated identical action is blocked/escalated,
- alternating cycle detected,
- budget exhaustion returns INCONCLUSIVE,
- strategy changes after repeated failure.

## Phase 4 — Repository intelligence

Deliver:
- manifest,
- exact anchors,
- lexical ranking,
- iterative retrieval,
- source hash provenance.

Then add:
- symbols/Tree-sitter with fallback,
- compact repo map.

Acceptance:
- file/symbol localization benchmark,
- fixed token budget comparison.

## Phase 5 — Context and TaskState

Deliver:
- explicit facts/hypotheses/unknowns,
- source-hash invalidation,
- bounded ContextPack,
- compaction/artifact pointers.

Acceptance:
- edited source invalidates old context,
- old log compacts without losing necessary evidence.

## Phase 6 — Skills

Convert repeated proven procedures into skills.

Acceptance:
- trigger precision/recall measured,
- no always-loaded skill bloat,
- compare with/without skill on same tasks.

## Phase 7 — Typed recovery

Deliver:
- failure taxonomy,
- failure packets,
- bounded recovery routing,
- failed-hypothesis memory.

Acceptance:
- recovery yield measured,
- no repeated same-strategy loop.

## Phase 8 — Lightweight memory

Deliver SQLite/FTS:
- episodes,
- facts,
- sources,
- skills index.

Acceptance:
- stale facts invalidate,
- unverified guesses cannot become confirmed,
- memory retrieval helps or does not degrade held-out tasks.

## Phase 9 — Optional retrieval upgrades

Only after paired pilots:
- BM25 fields,
- embeddings,
- graph expansion,
- reranker,
- alternate edit grammar.

Keep only measured wins.

## Phase 10 — Optional higher test-time compute

Evaluate:
- fresh reviewer,
- generated reproducer,
- 2-candidate branching,
- parallel read-only diagnosis.

Do not default-enable.

## Phase 11 — Research/document sidecar

Build only if useful for continued team learning/skill design.

## Phase 12 — Full ablation

Remove anything that fails to earn its complexity.

## Phase 13 — Submission hardening

- clean clone tests,
- no hidden local dependency,
- no credentials,
- exact Makefile contract,
- deterministic config,
- network-disabled trial if plausible,
- final diff/package inspection.

---

# 28. Evaluation Framework

## 28.1 Top-level metrics

Prioritize:

1. hidden/held-out task resolution
2. false-`VERIFIED` rate
3. regression rate
4. setup/runtime failure rate

Then:

- model calls,
- input/output tokens,
- wall time,
- test runtime,
- tool errors,
- patch-apply failures,
- files/lines read,
- no-progress loops,
- recovery yield,
- cost/task,
- cost/resolved-task.

## 28.2 Capability metrics

| Capability | Metrics |
|---|---|
| Task interpretation | acceptance-claim precision/recall |
| Localization | Recall@1/3/5, MRR |
| Repo map | relevant symbols per 1k tokens |
| Retrieval | Recall@k, nDCG, MRR |
| Context | resolution per context token |
| Skill routing | precision/recall/F1 |
| Tools | valid-call %, unnecessary-call rate |
| Editing | patch-apply %, parse-valid %, unrelated diff |
| Reproducer | valid reproduction rate |
| Verification | false-positive completion rate |
| Recovery | rescue rate |
| Memory | useful recall, stale/harmful memory |
| Loop controller | repeated-action rate, no-progress steps, strategy-change effectiveness |

## 28.3 Experimental protocol

For baseline B vs candidate C:
- same prescribed model,
- same settings,
- same issue set,
- same commits,
- same runtime image,
- same wall/model budgets,
- same grader.

Run paired comparisons.

For stochastic settings, run multiple trials.

Report:
- per-task wins/losses,
- task categories,
- aggregate metrics,
- cost/latency delta.

A small pilot can expose large defects; it cannot prove small improvements.

## 28.4 High-value ablations

Suggested order:
1. bash-only vs hybrid tools
2. direct vs adaptive route planning
3. lexical vs lexical+symbols
4. one-shot vs iterative evidence-gap retrieval
5. raw/capped logs vs parsed observations
6. generic retry vs typed recovery
7. skills vs no skills
8. repo map vs no map
9. memory vs stateless
10. optional reviewer vs none
11. embeddings/reranking vs lexical+structural baseline

Verification remains required; measure its cost rather than removing the safety/evidence gate in production.

---

# 29. Architecture-Change Gate

Before adding a subsystem, Claude Code/team must answer:

1. Which measured failure does this solve?
2. Which tasks reproduce that failure?
3. What is the simplest alternative?
4. Which primary source supports the mechanism?
5. Is that source truly comparable to our task/model?
6. What dependencies are added?
7. What tokens/context are added?
8. What latency is added?
9. What new failure modes appear?
10. What is the paired eval?
11. What is the ablation?
12. What is the rollback plan?
13. Does it preserve prescribed-model portability?

A component enters the core only if it:
- improves held-out resolution without unacceptable cost/latency, or
- reduces cost/latency/variance without materially reducing resolution, or
- is required for security/reproducibility/competition compliance.

---

# 30. Open Decisions Requiring Rules or Local Measurement

Do not fabricate these:

- exact `make run` input/output protocol,
- exact prescribed model/model API/tool-calling features,
- time/token/model-call ceilings,
- languages in evaluation repos,
- Docker availability,
- network availability,
- ability to install system packages,
- whether tests may be added,
- whether full-suite execution fits,
- whether multiple contexts/reviewer calls share one budget,
- required final patch/result format,
- whether external services are allowed.

Routing thresholds, retry counts, output caps, token allocations, verification reserve, and snapshot policy should remain configurable until pilots.

---

# 31. Research Library — Architecture Decisions Supported

Use primary sources wherever possible.

## 31.1 Core coding-agent architecture

### mini-SWE-agent
https://github.com/swe-agent/mini-swe-agent

Use for:
- minimal baseline,
- shell-centric interface,
- proof that strong models may need less scaffold than expected.

Architectural influence:
- keep controller thin,
- measure every extra layer.

### SWE-agent — Agent-Computer Interfaces Enable Automated Software Engineering
https://arxiv.org/abs/2405.15793

Use for:
- ACI/tool interface design,
- bounded software-engineering actions.

Architectural influence:
- tool ergonomics are part of agent performance.

### Agentless
https://arxiv.org/abs/2407.01489  
https://github.com/OpenAutoCoder/Agentless

Use for:
- hierarchical localization,
- repair,
- validation,
- staged deterministic flow.

Architectural influence:
- deterministic lifecycle around the adaptive model.

### AutoCodeRover
https://arxiv.org/abs/2404.05427

Use for:
- AST/program structure,
- iterative search,
- executable-test/fault evidence.

### RepairAgent
https://arxiv.org/abs/2403.17134

Use for:
- finite-state repair-control ideas.

### CodePlan
https://www.microsoft.com/en-us/research/publication/codeplan-repository-level-coding-using-llms-and-planning/

Use for:
- planning interdependent repository changes.

Do not use as justification for planning every trivial patch.

---

## 31.2 Repository localization/context

### Aider RepoMap
https://aider.chat/docs/repomap.html  
https://github.com/Aider-AI/aider/blob/main/aider/repomap.py

Use for:
- token-budget repository representation,
- symbol-level global orientation.

### RepoCoder
https://aclanthology.org/2023.emnlp-main.151/

Use for:
- iterative retrieval driven by newly discovered context.

### Repoformer
https://arxiv.org/abs/2403.10059

Use for:
- selective repository retrieval ideas.

### LocAgent
https://aclanthology.org/2025.acl-long.426/

Use for:
- localization research reference.

### CodeRAG / CodeRAG-Bench
https://aclanthology.org/2025.emnlp-main.1187/  
https://aclanthology.org/2025.findings-naacl.176/

Use for:
- advanced retrieval/reranking experiments.

---

## 31.3 Planning / looping / recovery

### ReAct
https://arxiv.org/abs/2210.03629

Supports:
- observation-driven action loops.

### Reflexion
https://arxiv.org/abs/2303.11366

Supports:
- compact outcome-linked episodic feedback.

Do not infer that reflection after every action is useful.

### Tree of Thoughts
https://arxiv.org/abs/2305.10601

Use only as:
- deliberate branching/search reference.

### LATS
https://arxiv.org/abs/2310.04406

Use only as:
- expensive test-time search reference.

Not default.

---

## 31.4 Claude / Agent Skills

### Anthropic skill-creator
https://github.com/anthropics/skills/blob/main/skills/skill-creator/SKILL.md

Use for:
- skill creation/evaluation,
- progressive disclosure,
- scripts/references.

### Claude plugin skill-development guide
https://github.com/anthropics/claude-plugins-official/blob/main/plugins/plugin-dev/skills/skill-development/SKILL.md

Use for:
- skill triggers,
- lean `SKILL.md`,
- references/examples/scripts.

### Claude Code documentation
https://code.claude.com/docs/

Use for:
- `CLAUDE.md`,
- project instruction organization,
- development workflow.

Remember: native Claude features are development aids unless competition runtime guarantees Claude.

---

## 31.5 Context engineering / long-running systems

### Anthropic Effective Context Engineering
https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

Use for:
- high-signal context,
- just-in-time retrieval.

### Effective Harnesses for Long-Running Agents
https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

Use for:
- explicit progress/state artifacts.

### Harness Design for Long-Running Application Development
https://www.anthropic.com/engineering/harness-design-long-running-apps

Use for:
- planner/generator/evaluator ideas,
- most importantly: ablation/simplification as model capability increases.

### Lost in the Middle
https://aclanthology.org/2024.tacl-1.9/

Use for:
- caution around long contexts and evidence placement.

---

## 31.6 Tools/editing/runtime

### Anthropic — Writing Effective Tools for Agents
https://www.anthropic.com/engineering/writing-tools-for-agents

Use for:
- clear small tool sets,
- bounded high-signal responses.

### MCP
https://modelcontextprotocol.io/

Use for:
- optional external interoperability.

Not required for local core calls.

### Aider edit formats
https://aider.chat/docs/more/edit-formats.html

Use for:
- model editing-format trade-offs.

### Codex / apply-patch reference
https://developers.openai.com/api/docs/guides/tools-apply-patch

Use for:
- patch-tool design reference.

### Git
https://git-scm.com/docs/git

Use for:
- workspace/diff semantics.

### Docker limits
https://docs.docker.com/engine/containers/run/

Use for:
- runtime isolation/resource bounds when supported.

---

## 31.7 Verification / tests

### SWE-bench
https://arxiv.org/abs/2310.06770  
https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py

Supports:
- environment outcome over narrative confidence,
- exact test-transition evaluation.

### SWT-Bench
https://arxiv.org/abs/2406.12952

Research reference for:
- test-generation/evaluation.

### Ekstazi
https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf

Reference for:
- regression test selection.

### PatchDiff
https://arxiv.org/abs/2503.15223

Reference for:
- patch/test evaluation limitations and analysis.

---

## 31.8 Memory

### MemGPT
https://arxiv.org/abs/2310.08560

Use for:
- active context vs external memory tiers.

### LongMemEval
https://arxiv.org/abs/2410.10813

Use for:
- temporal reasoning,
- knowledge updates,
- abstention/freshness evaluation.

### Mem0
https://arxiv.org/abs/2504.19413

Use as:
- future memory implementation reference.

Do not assume reported benchmark gains transfer.

### Graphiti
https://github.com/getzep/graphiti

Use when:
- temporal relationship queries become a measured need.

---

## 31.9 Research RAG / ingestion

### Anthropic Contextual Retrieval
https://www.anthropic.com/engineering/contextual-retrieval

Use for:
- contextual chunks,
- lexical+dense retrieval,
- reranking.

### ColBERT
https://github.com/stanford-futuredata/ColBERT

Use for:
- late-interaction retrieval if simple hybrid retrieval is insufficient.

### Trafilatura
https://trafilatura.readthedocs.io/en/latest/

Use for:
- web article/blog extraction.

### Docling
https://github.com/docling-project/docling

Use for:
- structured document parsing.

### Apache Tika
https://tika.apache.org/

Use for:
- broad format fallback.

---

# 32. Prompt for Architecture Work

```text
You are changing the AI Coding Harness architecture.

Before coding:
1. Read CLAUDE.md.
2. Read the relevant section of AI_HARNESS_FINAL_BLUEPRINT.md.
3. Inspect current implementation and eval results.
4. State the measured failure or objective.
5. Compare the simplest viable alternatives.
6. Identify the source/research basis.
7. Identify added dependencies, token/context cost, latency, and failure modes.
8. Define acceptance criteria.
9. Define a paired eval and ablation.
10. Do not implement until the design is falsifiable.

When implementing:
- preserve model portability;
- keep interfaces modular;
- avoid unrelated refactors;
- add structured telemetry;
- add capability-level tests;
- run the relevant benchmark;
- compare against baseline;
- keep the feature only if evidence supports it.

Never claim an optimization is better without measured evidence.
```

---

# 33. Prompt for Solving a Coding Task

```text
Goal: solve the supplied software-engineering issue in the current repository.

Rules:
- Preserve original issue as immutable task input.
- Separate facts from hypotheses.
- Every search should answer a named uncertainty.
- Inspect before editing.
- Localize broad-to-narrow.
- Prefer smallest relevant context and justified patch.
- Reproduce the failure first when practical.
- Treat tools/tests as evidence.
- Do not weaken tests to manufacture success.
- Bind verification evidence to the exact candidate hash.
- If evidence contradicts the hypothesis, replan explicitly.
- If the same failure repeats, change strategy.
- If a loop produces no new evidence, break/escalate.
- Do not declare success without external verification.
- Respect wall/step/token/model-call budgets.
- Preserve enough budget for final verification.
- Return INCONCLUSIVE with exact evidence gaps rather than false success.
```

---

# 34. Claude Code Decision Checklist

For every task:

```text
[ ] What phase are we in?
[ ] What is the exact information gap?
[ ] What evidence would resolve it?
[ ] What is the cheapest tool/action that can provide that evidence?
[ ] Has this action/query already been tried?
[ ] Will this create new information?
[ ] Is current source/evidence fresh for this workspace hash?
[ ] Are we about to edit based on a hypothesis rather than evidence?
[ ] After edit, which evidence became stale?
[ ] What is the cheapest relevant check now?
[ ] Are we preserving final verification budget?
[ ] Does final evidence match the exact candidate hash?
```

---

# 35. Final Optimization Pass

## Build now

- model-portable adapter
- deterministic controller
- bounded evidence-driven loops
- loop/no-progress detector
- TaskState + evidence contract
- lexical search
- bounded file reads
- transactional edit engine
- command/test runner
- exact diff/workspace hash
- baseline + evidence ledger
- verifier
- typed recovery
- structured telemetry
- clean Makefile runtime

## Build after baseline

- symbol/AST index
- repo map
- modular skills
- context compaction
- SQLite episodic/semantic memory

## Build only after evidence

- dense code embeddings
- reranker
- generated reproducer
- reviewer model/context
- architect/editor split
- parallel diagnosis
- MCP
- Mem0
- Graphiti
- ColBERT
- runtime web search
- ToT/LATS

## Explicitly reject as default

- giant monolithic prompt,
- dozens of always-loaded skills,
- unlimited retry loop,
- many editing agents sharing one workspace,
- verification by self-confidence,
- `47 failures -> 46 failures` as proof,
- stale green tests after another edit,
- vector database before lexical/structural baseline,
- framework stacking for its own sake,
- broad destructive git commands in unknown workspaces.

---

# 36. Definition of a Winning Engineering Process

The target is not:

> “We integrated the maximum number of papers.”

The target is:

> **We built a compact harness, measured each capability, kept the mechanisms that improved held-out software-engineering outcomes, removed redundant scaffolding, and bound every success claim to current executable evidence.**

The practical optimization objective is:

```text
                     correctly resolved tasks
maximize   ------------------------------------------------
           time × tokens × model calls × failure risk
```

subject to:
- verification,
- reproducibility,
- security,
- competition compliance.

**Every block must earn its place.**
