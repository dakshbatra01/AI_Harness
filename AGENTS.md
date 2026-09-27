# AGENTS.md — AI Coding Harness Hackathon Operating Contract

> Always-loaded instructions for Codex.  
> Detailed architecture, research, trade-offs, interfaces, build phases, and references are in `AI_HARNESS_FINAL_BLUEPRINT.md`. Read that file before architecture-changing work.

## Mission

Build the **smallest, strongest, model-portable software-engineering harness** that reliably solves repository issues under the hackathon evaluator.

Priority:

1. correctness
2. evidence
3. reliability/recovery
4. efficiency
5. reproducibility/security
6. maintainability/extensibility

Do not optimize for number of agents, tools, frameworks, papers, or memory layers.

## Competition Rules to Preserve

- Root `Makefile`.
- `make setup` and `make run` must work from a clean environment.
- `make test` where applicable.
- Runtime credential comes from `AI_API_KEY`.
- Never hard-code/commit/log secrets.
- Evaluation is text-only.
- Use the organizer-prescribed model/model family.
- Evaluator should not need to patch source/config.
- Keep runtime model portable.
- Do not assume internet, Docker, MCP, external databases, or provider-specific features unless official rules confirm them.

## Decided Architecture

Use an **Adaptive Evidence-Gated Controller**:

**deterministic outer state machine + bounded evidence-driven loops + one adaptive coding agent + deterministic services + external verification**

Lifecycle:

```text
ORIENT -> LOCATE -> REPRODUCE -> EDIT -> CHECK -> FINAL_VERIFY -> DONE
             ^                           |
             |                           v
             +--------- RECOVER <--------+
```

The model chooses actions inside phases.

The controller owns:
- legal transitions,
- strategy route,
- budgets,
- attempts,
- loop detection,
- completion legality.

Only the verifier may produce a `VERIFIED` recommendation.

## Looping Policy

Looping is required, but it must be **evidence-driven and bounded**.

Good:

```text
act -> observe -> learn something new -> update state -> act differently
```

Bad:

```text
same attempt -> same failure -> same attempt -> same failure
```

An iteration counts as progress only if it produces at least one:
- new verified fact,
- new relevant file/symbol,
- new test result,
- changed hypothesis,
- changed diff/workspace hash,
- eliminated hypothesis,
- new failure signature,
- new useful dependency/reference,
- resolved uncertainty.

Track action/observation fingerprints and detect:
- identical repetition,
- alternating cycles,
- edit/revert cycles,
- repeated zero-yield searches,
- repeated model calls with unchanged state.

Default recovery semantics:
- first repeated failure: inspect evidence,
- second same signature: challenge hypothesis / broaden context,
- third same signature: abandon strategy / move back a phase,
- no distinct strategy or budget: `INCONCLUSIVE`.

Exact thresholds are configurable and must be tuned by evals.

Always preserve a verification reserve.

## Strategy Routing

After cheap orientation:

### DIRECT
Clear local behavior/change/test. No extra planning call.

### LIGHT_PLAN
Several related edits or moderate uncertainty. Store a small 3–5 step plan.

### STRUCTURED
Cross-component/API propagation, uncertain localization, repeated failure, ambiguity, or high regression risk. Use explicit acceptance claims, hypothesis ledger, checkpoints, broader checks.

Escalate when evidence requires it. Never let complexity labeling make an obvious fix artificially complex.

## Shared State Rules

Maintain an immutable `original_issue` plus:
- objective,
- explicit/inferred requirements,
- acceptance claims,
- confirmed facts,
- hypotheses,
- unknowns,
- plan,
- phase/strategy,
- relevant/changed files,
- base revision/workspace hash,
- failure/action histories,
- evidence IDs,
- budgets,
- verification status.

Only tool/code/test observations can create confirmed facts. Model interpretations remain hypotheses until checked.

Every edited file invalidates source context from the previous file hash. Any edit after a green test makes patch-specific evidence stale.

## Repository Retrieval

Default order:

1. file tree / paths
2. exact issue anchors / stack frames / symbols
3. bounded lexical search (`rg`/FTS/BM25)
4. symbol/AST search when available
5. one-hop import/reference/test relations
6. compact repo map
7. semantic retrieval only if evals prove lexical/structural recall is insufficient
8. reranker only if evals justify it

Every search must answer a named uncertainty.

Do not run the same query/scope repeatedly without new evidence.

## Context

Always pin:
- original issue,
- constraints,
- acceptance claims,
- current verified state.

Load just-in-time:
- exact relevant code spans,
- latest failure/test evidence,
- current hypothesis/plan,
- relevant skill only,
- current-phase tools only.

Store large raw logs as artifacts and give the model bounded excerpts + IDs.

Before compacting old context:
1. extract useful verified information,
2. attach provenance,
3. update state/episode,
4. then discard raw content.

Optimize useful information per token.

## Skills

Use progressive disclosure:

`metadata -> SKILL.md -> references/examples/scripts only when needed`

Initial procedures:
- issue-triage
- repo-recon
- fault-localization
- reproduce-bug
- patch-code
- verify-change
- failure-recovery
- final-review

Skills are reusable procedures, not repository facts.

Keep descriptions/triggers distinct and measurable.

## Tool Surface

Prefer a small hybrid interface:

- `search_code`
- `read_file`
- `edit_files`
- `run_command`
- `run_tests`
- `repo_changes`
- `read_log`
- optional `lookup_skill`

Tool requirements:
- strict schema validation,
- explicit cwd,
- time/output caps,
- stable error codes,
- real exit codes,
- separate stdout/stderr,
- immutable raw log IDs,
- path/workspace safety,
- hash-aware reads/edits,
- transactionality for multi-edit operations.

Do not silently fuzzy-match an ambiguous edit.

## Editing

Preferred:

```text
reread target
 -> exact-hash localized edit
 -> validate/dry-run
 -> syntax/parse check
 -> apply transactionally
 -> inspect diff
```

Whole-file rewriting is fallback.

Take snapshots before risky attempts.

Do not use broad destructive `git reset --hard` / `git clean` on an unknown or initially dirty workspace.

## Verification

Never treat model confidence as evidence.

Capture a baseline before editing where feasible:
- base revision,
- environment hash,
- test manifest,
- relevant test IDs/outcomes,
- known existing failures.

Progressive checks:
1. cheapest structural check,
2. issue/target test,
3. related tests,
4. build/type/lint,
5. risk-based regressions,
6. full suite when feasible/necessary,
7. final diff audit.

Compare **test identities**, not aggregate failure counts.

`FAIL -> SKIP` is not a fix.

Any test evidence applies only to the exact candidate hash it evaluated.

## Evidence Ledger

Maintain append-only evidence with:
- claim,
- SUPPORTED / CONTRADICTED / UNKNOWN / STALE,
- candidate hash,
- environment hash,
- test manifest hash,
- command/test IDs,
- baseline/candidate outcomes,
- logs/timestamp.

`VERIFIED` requires current support for all mandatory claims.

Use:
- `VERIFIED`
- `FAILED`
- `INCONCLUSIVE`

Do not convert missing checks, unknown outcomes, lower failure counts, or process exit zero alone into success.

## Recovery

Classify before retrying:
- setup,
- localization,
- requirement misunderstanding,
- patch application,
- syntax,
- build,
- target test,
- regression,
- timeout,
- tool/environment,
- repeated action/no progress,
- unknown.

Recovery must change at least one of:
- information,
- hypothesis,
- code,
- environment,
- strategy.

Reflection with no new evidence is not progress.

## Memory

V1:
- Working = current run state.
- Episodic = attempts/failures/outcomes.
- Semantic = verified repo facts scoped to revision/hash.
- Procedural = skills.
- External knowledge = separate research KB.

Start with SQLite + FTS.

Never durably store:
- secrets,
- unverified guesses as facts,
- raw hidden reasoning,
- huge duplicate logs,
- entire cheaply reread files,
- line-number facts without hash/version.

Facts require provenance, confidence, version, and stale/superseded semantics.

Mem0/Graphiti/embeddings are conditional, not default.

## Prompt Assembly

Do not maintain one giant prompt.

Compile:

```text
system invariants
+ task contract
+ current state/phase
+ relevant skill
+ only needed tool schemas
+ selected code
+ latest evidence
+ action/output contract
```

Distinguish:
- fact,
- hypothesis,
- observation,
- plan,
- result.

## Build Order

0. evaluator contract + model adapter + Makefile
1. minimal end-to-end loop
2. evidence/verification core
3. anti-loop controller and adaptive routes
4. repository intelligence/localization
5. TaskState + context invalidation/compaction
6. skills
7. typed recovery
8. lightweight SQLite memory
9. optional semantic retrieval/reranking
10. optional reviewer/generated reproducer/parallel diagnosis
11. research/document sidecar
12. full ablations
13. submission hardening

Do not jump directly to multi-agent, GraphRAG, complex memory, or vector-first retrieval.

## Architecture Change Gate

Before adding a component:
1. state measured failure,
2. define metric/baseline,
3. compare simplest alternatives,
4. identify source/research basis,
5. identify dependencies/latency/context/failure risk,
6. implement smallest testable version,
7. run same held-out tasks under same model/settings/budget,
8. ablate it,
9. keep only if it earns complexity.

A component belongs in core only if it:
- improves held-out resolution without unacceptable cost/latency,
- reduces cost/latency/variance without material correctness loss, or
- is required for security/reproducibility/compliance.

## Metrics

Top-level:
- held-out resolution,
- false-VERIFIED rate,
- regression rate,
- setup/runtime failure rate,
- runtime,
- model/tool calls,
- tokens,
- test runtime,
- tool errors,
- patch failures,
- files/lines read,
- no-progress loops,
- recovery yield,
- cost where measurable.

Localization: Recall@1/3/5, MRR.  
Retrieval: Recall@k, nDCG, MRR.  
Skills: trigger precision/recall/F1.  
Editing: patch-apply / parse-valid.  
Memory: useful recall / stale-harmful rate.  
Loop controller: repetition rate / no-progress steps / strategy-change success.

## Adopt / Conditional / Avoid

**CORE:** model-portable loop, deterministic controller, evidence-driven bounded looping, exact state/evidence contracts, lexical search, bounded reads, transactional edits, tests, verifier, evidence ledger, typed recovery, telemetry, clean Makefile.

**ADD AFTER BASELINE:** Tree-sitter/symbols, repo map, skills, context compaction, SQLite memory.

**CONDITIONAL:** embeddings, reranking, reviewer, generated reproducer, architect/editor split, parallel diagnosis, MCP, Mem0, Graphiti, ColBERT, runtime web.

**AVOID AS DEFAULT:** giant prompts, many always-loaded skills, unlimited reflection, many editing agents in one workspace, stale evidence, success by confidence, framework stacking, broad destructive git operations, vector DB before lexical/structural baseline, ToT/LATS on ordinary tasks.

## Research Integrity

Prefer original papers, official repositories/docs, and author engineering write-ups.

Historical benchmark results are mechanisms/evidence from their setting, not promised gains here.

Never invent:
- hackathon rules,
- APIs,
- model capabilities,
- benchmark results,
- repository features,
- paper claims.

## Before Architecture Work

Read:
1. `AGENTS.md`
2. relevant section of `AI_HARNESS_FINAL_BLUEPRINT.md`
3. current implementation
4. current eval report

Then state:
- problem,
- evidence,
- options,
- trade-offs,
- acceptance criteria,
- paired eval,
- rollback.

Never call an optimization “better” without measured evidence.

## Implementation map (current code)

- Entry: `harness/cli.py` (`make run`), config `config/harness.toml`, env overrides `AI_MODEL`, `HARNESS__SECTION__KEY`.
- Controller/state machine/budgets/recovery/L1: `harness/controller/controller.py`, `state.py`, `loopguard.py`.
- Tool schemas + text protocol: `harness/controller/protocol.py`; prompts: `harness/controller/prompts.py`.
- Verifier + ledger + test parsing: `harness/verify/`. Workspace (shadow git, base swap): `harness/workspace.py`.
- Code map (issue-ranked outline + usage graph for test selection): `harness/repo/codemap.py`; post-edit diagnostics: `harness/tools/diagnostics.py`; repo guidance: `harness/repo/manifest.py::read_guidance`.
- Task input (interactive session / headless / piped JSON / test cases): `harness/cli.py`.
- Always run `make test` before and after a change; evaluate with `make eval` (see README "Improving the harness").
