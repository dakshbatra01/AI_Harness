# Evidence-Gated Coding Harness

A harness that turns the prescribed text-only model into a careful software engineer. It understands an issue, finds the relevant code, fixes it, and **proves** the fix by running tests on the original code and on the patched code. Only that evidence can mark a result `VERIFIED`.

> Correctness first. Evidence over claims. Efficiency matters.

Optimisation priorities, in order: **1. the right answer**, **2. as many passing tests as possible**, **3. the fewest tokens**. When two priorities conflict, the higher one wins.

## Quick start (the official evaluator flow)

```bash
git clone <this repo> && cd <this repo>
export AI_API_KEY=...          # read from the environment only; never stored or logged
make setup                     # checks the toolchain; standard library only, nothing to download
make run                       # opens the terminal dashboard when run interactively
```

`make run` opens the full-screen dashboard in an interactive terminal. Press **W** for a workspace path or git URL, **I** for an issue URL, file path, or pasted text (finish with a line containing only `.`), **T** for optional failing test IDs or commands, then **R** to run. The dashboard shows progress and the evidence verdict. Press **D** to inspect the patch and **Q** to quit when idle.

The line-oriented session remains available with `make run ARGS=--plain`. It asks for three things:

1. **The issue.** A GitHub issue URL, a path to an issue file, or the pasted issue text. Finish the text with a line containing only `EOF`.
2. **The repository.** A local path or a git URL. A GitHub issue URL clones its repository automatically.
3. **The test case (optional).** Failing test IDs or commands that must pass.

It then solves the task, prints the verified result and the patch, and offers the next task. `quit`, or Ctrl-D on an empty prompt, ends the session.

Piped input and explicit issue flags still run headlessly through `make run`. `make tui` is an optional direct shortcut for the dashboard.

**Headless alternatives:**
- `make run REPO=path ISSUE_FILE=issue.md TESTS='id1,id2'`
- `ISSUE='text'`
- `ISSUE_URL=https://github.com/o/r/issues/N`
- text piped into `make run`
- JSON piped into `make run`: `{"repo", "issue" | "issue_url", "tests", "commit"}`

**Output:** the patch is left applied in the target repository. `runs/<id>/` contains:

| File | Contents |
|---|---|
| `patch.diff` | The final patch |
| `result.json` | Status, claims, fixed tests, regressions, metrics, token breakdown, model config |
| `report.md` | A human-readable evidence report |
| `ledger.jsonl` | Evidence entries tied to the exact patch version (its content hash) |
| `trace.jsonl` | The full trace of the run |
| `logs/` | Raw output of every command |

```bash
make test    # unit/capability tests plus an offline end-to-end smoke run
make eval    # run tasks with hidden-test grading: resolve rate, false-VERIFIED rate, tokens, time
```

Only the Python standard library (3.9+) and `git` are needed, so `make setup` cannot fail on a download. `ripgrep` is used when present.

## Model configuration

The model is defined in the **MODEL CONFIGURATION** block of `config/harness.toml`: `[model].name`, or `AI_MODEL` to override it. Some providers have a default; OpenRouter and Qwen require an explicit model ID. Set the exact evaluator model ID. The effective model is printed at startup and recorded in every `result.json`.

For local Groq GPT-OSS 120B testing, export your Groq key as `AI_API_KEY` and run:

```bash
AI_PROVIDER=groq AI_MODEL=openai/gpt-oss-120b make run
```

With an OpenRouter key, use OpenRouter's model ID for DeepSeek:

```bash
AI_PROVIDER=openrouter AI_MODEL=deepseek/deepseek-v4.1-flash make run REPO=/path/to/repo ISSUE_FILE=/path/to/issue.md ARGS=--headless
```

With a direct DeepSeek key, use `AI_PROVIDER=deepseek AI_MODEL=deepseek-flash`. OpenRouter's `sk-or-v1-` key is detected automatically, but a direct DeepSeek key needs the provider or model configured. A list of credentials is supported only when every endpoint serves the **same** selected model ID; it does not switch models between calls.

For an Alibaba Model Studio Qwen key, provide the model ID and the OpenAI-compatible base URL for the key's region (example for Singapore):

```bash
AI_PROVIDER=qwen AI_MODEL=qwen-plus AI_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1 make run REPO=/path/to/repo ISSUE_FILE=/path/to/issue.md ARGS=--headless
```

Qwen keys have no reliable provider or region prefix, so a key alone cannot select its endpoint. `AI_MODEL` and `AI_BASE_URL` keep the harness usable with whichever Qwen model and region the evaluator supplies. Keep the credential only in `AI_API_KEY`.

Providers (all over raw HTTP):
- Anthropic Messages
- OpenAI Chat Completions
- Gemini
- DeepSeek, OpenRouter, and Qwen Model Studio
- any OpenAI-compatible endpoint

The harness uses native tool calling and falls back automatically to a plain-text tool protocol that works with any model. Decoding is deterministic where the model allows it (`temperature=0`, fixed `seed`). Parameters a model rejects are dropped automatically.

## How it works

```
make run ─► task (issue / test case)
           │
ORIENT (no model calls): file manifest · issue anchors · candidate files · issue-ranked code map ·
           │             repository guidance · test-framework detection · route
           ▼
LOCATE ─► REPRODUCE ─► EDIT ─► CHECK ─► submit ─► SELF-REVIEW ─► VERIFY (no model calls) ─► DONE
   ▲                                                                         │
   └────────────── RECOVER (typed failure feedback, fresh-context retry) ◄───┘
```

### 1. Accuracy: the right answer

- **The verifier decides, not the model.**
  - Every test command runs on the original code and on the patch. The original-code run keeps the patch's test files, which is how hidden tests are graded.
  - Outcomes are compared **per test**, not by counts.
  - A test moving to SKIP, disappearing or timing out is never a pass.
  - Evidence is bound to the exact patch hash; any later edit makes it stale.
- **Test cases are first-class.** Tests supplied with the task are mandatory targets: they must go FAIL→PASS.
- **Self-review before verification.** The first submit of a new patch shows the exact diff plus a five-point checklist:
  1. scope;
  2. generality;
  3. re-run checks after late edits;
  4. no weakened tests;
  5. evidence named.

  It is skipped if the agent already reviewed that exact patch.
- **Guarded editing.**
  - Replacements must match exactly once.
  - Several edits to a file apply all-or-nothing.
  - A syntax gate rejects edits that break parsing.
  - Post-edit diagnostics report names *the edit introduced* that are defined nowhere (missing import, typo). The checker is our own, runs in milliseconds, and needs no third-party tools.
- **Reproduce first.** Reproducer scripts live in `.harness_scratch/`, which never enters the patch. A reproducer only counts if it fails on the original code.
- **Recovery.**
  - Failures are classified (syntax, target still failing, regression, missing evidence, weak evidence), and each class gets specific guidance.
  - The same failure three times abandons the strategy.
  - A stuck detector watches for repeated actions, repeated errors, empty replies, ping-pong and edit/revert oscillation.
  - A fresh-context second attempt carries only the recorded lessons. The **best** candidate is kept, never simply the latest.

### 2. Tests: as many passing as possible

The regression set combines:
- tests associated by name with the changed files;
- **tests that actually use the changed code**, found from the code map's usage graph;
- the full suite when it is affordable.

Flaky tests are re-run and excluded. A new collection error counts as a regression. The agent is also shown the repository's own documented test commands (from `AGENTS.md`, `CONTRIBUTING.md` and similar files), clipped and labelled as data.

### 3. Efficiency: fewer tokens

| Mechanism | What it saves | Measured |
|---|---|---|
| **Issue-ranked code map.** The harness indexes definitions and imports, builds a "which file uses which" graph, and runs a random walk that restarts at the issue's evidence, with widely used utility files damped. It renders signatures into a fixed token budget. | Exploration turns, each of which re-sends the whole conversation | Whole CPython stdlib (1,867 files) indexed in about 6 s. For an `email.message` issue it ranks `message`, `_policybase`, `utils`, `generator`, `charset` and `_encoded_words` in about 1,080 tokens. Skipped on repos under 15 source files; build time capped. |
| **Lean fixed prompt.** Terse tool descriptions and system prompt; a one-line skill catalog. | Tokens re-sent on every call | Per-call fixed prompt 2,900 → 2,060 tokens (**−29%**), all rules kept |
| **Bounded observations.** Head/tail truncation with a log ID for the rest; test output reduced to the failure sections; duplicate reads suppressed while still visible. | Large outputs | — |
| **Cache-friendly history.** A stable prefix and append-only history; old observations masked in chunks where the provider allows editing history. | Re-sent context | — |
| **Budget with a verification reserve.** 20% of every budget is reserved for verification. | Wasted turns | — |
| **Token accounting.** `result.json` splits estimated input tokens into fixed prompt versus task/history, and records the code-map cost. | — | Every run |

**Accuracy guardrails on token savings:**
- The verifier always reads the full raw logs.
- Masked outputs keep a pointer so they can be re-read.
- The code map is only a hint and can be switched off (`[context].codemap = "off"`).
- Running out of budget ends as `INCONCLUSIVE` with the best candidate, never as a false success.

### Evaluation-flow compliance

| Requirement | Implementation |
|---|---|
| Makefile with `setup`, `run`, `test`, `clean` | All present; `make setup` installs nothing, so it cannot fail on a download |
| `AI_API_KEY` from the environment | Removed from every child process and redacted from all logs (tested) |
| Text-only model | No other modality is used anywhere |
| Model clearly configured | The MODEL CONFIGURATION block, printed at startup and saved in the results |
| `make run` launches the harness in evaluation mode | The interactive session, or headless when input is piped |
| Interactive terminal flows | `make run` launches the dashboard in a terminal and accepts headless issue input |
| Reproducibility | `temperature=0` and a fixed `seed`; every setting lives in one TOML file with documented env overrides |

## Configuration switches worth knowing

| Setting | Default | Purpose |
|---|---|---|
| `[budget] max_steps / max_wall_s / max_attempts` | 100 / 3000 / 2 | Match to the evaluator's limits |
| `[verify] review_on_submit` | `true` | Self-review before verification |
| `[verify] full_suite` | `auto` | Full suite when it is small or the change is broad |
| `[context] codemap / codemap_tokens` | `auto` / 1200 | Issue-ranked code map |
| `[tools] profile` | `full` | `bash` = shell + submit only, for comparing interface designs under the same verifier |

## Repository layout

```
Makefile  config/harness.toml  .env.example  pyproject.toml  README.md  CLAUDE.md
harness/
  cli.py          task input: interactive session, headless flags/env, piped text or JSON
  controller/     controller.py (phases, budgets, recovery, self-review) · protocol.py (tools) ·
                  prompts.py · loopguard.py · state.py
  repo/           manifest.py (files, guidance) · anchors.py · localize.py · codemap.py
  tools/          runtime.py (sandboxed commands) · search.py · files.py · editor.py · diagnostics.py
  verify/         testrun.py (framework detection, parsers) · verifier.py · ledger.py
  provider/       anthropic.py · openai_compat.py · http.py · scripted.py
  memory/ skills.py telemetry.py workspace.py (private snapshot repo, base swaps) eval.py
skills/           8 procedure files loaded on demand
tests/            test_core.py · test_features.py · test_providers_http.py · test_review_regressions.py
eval/             tasks.json + runner output
docs/fix_plan.md  experiment backlog
```

## Improving it

1. Run `make eval` on a task set with hidden tests.
2. Read the transcripts in `runs/*/trace.jsonl`.
3. Change one thing, run `make test`, then re-evaluate on the same tasks.
4. Keep the change only if resolution rises at acceptable cost, or cost falls at equal resolution, and the false-VERIFIED rate stays at or below 10%.

## Prior art

This harness is our own design and implementation. We studied public work on software-engineering agents (SWE-bench grading methodology, agent-computer interface research, repository maps and open-source coding agents) and the research papers listed in `RESOURCES.md`, and credit them for the ideas they explore.
