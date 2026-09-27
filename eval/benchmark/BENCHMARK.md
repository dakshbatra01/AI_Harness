# Harness Benchmark: Problem Setting and Test Cases

A small, self-contained benchmark for measuring an AI coding harness the way the AI Harness Hackathon evaluates one: a repository, an issue (or a failing test case), a prescribed text-only model, and hidden tests that decide whether the change is correct.

Every project in this benchmark was **written from scratch for the benchmark**. No third-party repository was modified. Each task is either a **feature request** or a **wrong-behaviour report** written the way a user would file it on GitHub.

## 1. What is measured

The hackathon ranks harnesses on correctness first, evidence second and efficiency third. This benchmark measures all three per task:

| Criterion | Metric | How it is measured |
|---|---|---|
| **Correctness** | Resolved | Hidden tests (never shown to the harness) pass **and** the project's full visible test suite still passes (no regressions) |
| **Evidence** | Verdict honesty | The harness's own verdict (`VERIFIED` / `FAILED` / `INCONCLUSIVE`) is compared with the hidden result. **False VERIFIED** (the harness claims success but the hidden tests fail) is the worst error. |
| **Efficiency** | Cost | Model calls, input/output tokens, steps, wall-clock time |
| **Robustness** | Behaviour | Recovery from mistakes, loop control, tool-call errors, end reason |

The input modes match the hackathon's "GitHub issue / test case" wording. Seven tasks give issue text. One task (E4) gives **only a failing test ID**, with no issue text.

## 2. Setup

- **Model:** `openai/gpt-oss-120b` served by Groq (free tier). This is the closest available match to the DeepSeek-Flash class of model expected at the hackathon. The Groq key used offers no DeepSeek model.
- **Provider limits (free tier):**
  - 8,000 tokens per minute, and each request counts toward it;
  - 1,000 requests per day;
  - a daily token cap.

  This makes the run an extreme test of token efficiency. The harness runs with the `config/profiles/tight-8k.toml` profile: small context budget, aggressive masking of old outputs, and pacing that follows the provider's rate-limit headers.
- **Harness entry point:** the same `Controller` that `make run` uses. Every task starts from a fresh, git-initialised copy of the project.
- **Budget per task:** at most 60 steps, 70 model calls, 40 minutes, and 2 fresh-context attempts.

## 3. Projects

| Project | Size | What it is |
|---|---|---|
| `invoicekit` | 3 modules | Invoice arithmetic with `Decimal` money, discount and tax |
| `textstats` | 4 modules | Word statistics library plus a CLI |
| `slugkit` | 2 modules | URL slug generation |
| `inventory` | 7 modules | Stock tracking: items, receipts, shipments, CSV import, report |
| `taskflow` | 24 modules in 6 packages | Task-pipeline runner: config schema and loader, task model, dependency graph, action registry, executor, results, text and JSON reporters, CLI |

## 4. Test cases

| ID | Level | Kind | Project | What the harness must do |
|---|---|---|---|---|
| E1 | Easy | Wrong behaviour | invoicekit | Discount must be taken off **before** tax (total 110.00 → 108.00) |
| E2 | Easy | Feature | textstats | Add a `--json` flag to the CLI with a fixed key set; text output unchanged |
| E3 | Easy | Wrong behaviour (crash) | textstats | `ZeroDivisionError` on empty, whitespace-only or punctuation-only input → report 0 words, 0.0 average |
| E4 | Easy | **Test case only** | slugkit | Given only `tests/test_slug.py::test_collapses_separators`: collapse separator runs, strip edge separators; the documented `max_length` rule must also hold |
| M1 | Medium | Feature, multi-file | inventory | Stock reservations: `reserve` / `release`, `Item.reserved` / `available`, shipping only from available stock, extended report total line |
| M2 | Medium | Wrong behaviour, silent | inventory | CSV quantities with thousands separators (`"1,200"`) import as 1; no error raised; must be found from the symptom |
| H1 | Hard | Wrong behaviour, cross-module | taskflow | Dependencies referenced by **alias** are ignored, so tasks run out of order and failures do not propagate. The root cause sits in the dependency graph, away from where the symptom appears. |
| H2 | Hard | Feature, cross-cutting | taskflow | `retries` policy: schema validation (reject negative, non-integer and boolean values), task model, executor retry loop, `attempts` in results, text report and JSON output |

### What the hidden tests check (summary)

- **E1:** discount-before-tax totals, with and without tax, with rounding; the no-discount path is unchanged.
- **E2:** the `--json` report keys and values, that `--top` is honoured, and that text output is still the default.
- **E3:** average length for empty, whitespace-only and punctuation-only text; `top_words("")`; the CLI on an empty file.
- **E4:** symbol runs become one separator; no leading or trailing separators; truncation never ends with a separator; custom separator; `unique_slug`.
- **M1:** defaults, `reserve` / `release` rules and errors, shipping limited to available stock, the report total line.
- **M2:** `"1,200"` and `"12,345,678"`; plain numbers still parse; CSV import with a quoted value, end to end, including report totals.
- **H1:** alias ordering with no warning, case-insensitive aliases, failures propagating through an alias, target selection through an alias; unknown dependencies still only warn.
- **H2:** retry until success, give up after N retries, a default of one attempt, invalid values rejected (`-1`, `"2"`, `True`, `1.5`), the report and JSON showing attempts.

### Issue texts given to the harness

These are shown verbatim, exactly as the harness receives them.

**E1:** *Invoice total is wrong when an invoice has both a discount and tax.* One item at 100.00, a discount of 10.00 and a 20% tax rate gives a total of 110.00. The accountant says it should be 108.00: the discount comes off before tax, as the module docstring also says. A code snippet reproduces it.

**E2:** *Feature request: JSON output for the CLI.* Add a `--json` flag. It prints one JSON object with `words`, `unique`, `avg_length` and `top` (a list of `[word, count]` pairs, honouring `--top`). The human-readable output stays the default and unchanged.

**E3:** *textstats crashes on an empty file*, with the full `ZeroDivisionError` traceback. The same happens for whitespace-only or punctuation-only files. Expected: 0 words and a 0.0 average.

**E4:** no issue text. The harness receives only the failing test ID `tests/test_slug.py::test_collapses_separators`.

**M1:** *Feature: reserve stock for pending orders*, with the five bullet requirements summarised in the table above.

**M2:** *Imported stock quantities are wrong for large numbers.* The ERP exports `"1,200"`; after `load_csv` the item shows 1 unit. Small numbers are fine and no error is raised.

**H1:** *Tasks run before their dependencies when the dependency is referenced by an alias*, with a pipeline JSON example and the observed warning. Referencing `build` directly works. If `build` fails, `deploy` should be skipped but runs anyway.

**H2:** *Feature: retry flaky tasks*, with the retry semantics, validation rules, `attempts` field and report and JSON formats.

## 5. Validity of the benchmark

`eval/benchmark/validate.py` proves every task is well-formed before any harness run:

- the hidden tests **fail** on the starting code;
- a reference solution (kept in `eval/benchmark/reference/`, never shown to the harness) passes the hidden tests **and** the full visible suite.

```
E1  easy   hidden fails on start=True  reference passes hidden=True  reference passes suite=True  -> OK
E2  easy   ...                                                                                    -> OK
E3  easy   ...                                                                                    -> OK
E4  easy   ...                                                                                    -> OK
M1  medium ...                                                                                    -> OK
M2  medium ...                                                                                    -> OK
H1  hard   ...                                                                                    -> OK
H2  hard   ...                                                                                    -> OK
```

## 6. How to run it

```bash
python3 -m venv .venv && .venv/bin/pip install pytest          # the projects' tests use pytest
PATH=$PWD/.venv/bin:$PATH python eval/benchmark/validate.py      # sanity-check the benchmark itself
export AI_API_KEY=...                                            # e.g. a Groq key (gsk_...)
PATH=$PWD/.venv/bin:$PATH HARNESS_CONFIG=config/profiles/tight-8k.toml \
    python3 eval/benchmark/run.py --run-id my-run                # optionally: --only E1,H2
```

Results land in `eval/benchmark/results/<run-id>/`: `results.json`, plus the full trace, report and patch per task.

## 7. Results (2026-09-27)

### 7.1 Real-model runs (Groq free tier)

Each Groq model has a free allowance of 200,000 tokens per day. `gpt-oss-120b` used its allowance on E1–E3, so E4 and M1 were run on `qwen/qwen3.8-27b`, which has its own allowance. M2, H1 and H2 are pending until the allowance resets.

| Task | Level | Model | Harness verdict | Hidden tests + suite | Calls | Input / output tokens | Time |
|---|---|---|---|---|---|---|---|
| E1 | easy | gpt-oss-120b | VERIFIED | ✅ resolved (4/4 hidden) | 16 | 47.6k / 2.1k | 5.7 min |
| E2 | easy | gpt-oss-120b | VERIFIED | ✅ resolved (3/3) | 27 | 104.4k / 4.2k | 12.9 min |
| E3 | easy | gpt-oss-120b | VERIFIED | ✅ resolved (3/3) | 11 | 35.8k / 1.7k | 4.4 min |
| E4 | easy (test case only) | qwen3.8-27b | VERIFIED | ✅ resolved (4/4) | 8 | 21.7k / 0.4k | 3.4 min |
| M1 | medium (multi-file feature) | qwen3.8-27b | VERIFIED | ✅ resolved (6/6) | 18 | 86.1k / 3.6k | 13.4 min |
| M2, H1, H2 | medium / hard | — | pending (daily token limit) | — | — | — | — |

**So far:** 5 of 5 tasks were resolved and there were **0 false VERIFIED**. Wall time is dominated by the free tier's 8k tokens-per-minute pacing; model latency itself is under 2 s per call.

**Recovery observed:**
- **E3 and M1:** the provider's daily limit cut the model off mid-task. In both cases the harness verified the changes already made without calling the model, reported VERIFIED, and the hidden tests confirmed the fixes.
- **M1:** the model hit the loop detector after three identical failing edits, recovered, and added regression tests of its own.

### 7.2 Verifier honesty test (no model; `honesty.py`)

A scripted agent submits 26 patches of known quality across all 8 tasks. The harness verdict is compared with the hidden tests.

| Patch kind | Runs | Expected | Result |
|---|---|---|---|
| correct fix | 8 | VERIFIED | 8/8 VERIFIED |
| wrong fix | 8 | not VERIFIED | 8/8 FAILED |
| partial fix, careful reproducer | 3 | not VERIFIED | 2/2 FAILED, 1/1 INCONCLUSIVE (E4) |
| fix that breaks existing tests | 2 | not VERIFIED | 2/2 FAILED (regression detected) |
| cheat: weaken or delete tests | 2 | not VERIFIED | 2/2 FAILED (diff audit) |
| correct fix, no evidence | 1 | INCONCLUSIVE | INCONCLUSIVE |
| overfit, narrow reproducer | 3 | not VERIFIED | 1/3 caught (E1 hard-coded example); **2 false VERIFIED** (E3, M2) |

**Overall:** 24 of 26 verdicts were honest; it was 22 of 26 before the fixes below. The 2 remaining false VERIFIED are partial fixes checked only against the issue's first example. No verifier can catch cases that nobody tested; the self-review checklist now requires testing every case the issue states.

### 7.3 Harness bugs found by this benchmark and fixed

1. The gateway rejected Python's default user-agent (403/1010).
2. Tool names with a namespace prefix (`repo_browser.run_tests`) were rejected server-side. The call is now salvaged.
3. Any 400 error mentioning "tool" wrongly disabled native tool calling.
4. Bare script paths (`.harness_scratch/repro.py`) failed with exit 126. The interpreter is now added automatically.
5. There was no tokens-per-minute pacing. The harness now paces against a budget that follows the rate-limit headers.
6. A daily-quota error made the harness wait. It now stops fast and verifies the pending work.
7. Rate-limit errors were not retried because the message links to a `/billing` page.
8. The per-minute output-token limit rejected requests. `max_tokens` now shrinks automatically.
9. Tool calls written in the `<function=…><parameter=…>` dialect were lost. That dialect is now parsed.
10. A final answer in prose instead of a `submit` call cost about 12 extra calls on E2. It is now treated as a submit (self-review, then verifier).
11. An overfit patch that special-cases the issue's example values is now blocked by the diff audit.
12. VERIFIED was reported while tests in the same file as the requested test case still failed.
13. List arguments sent as JSON strings made every edit fail on qwen.
14. Context overflow discarded the attempt's work and restarted from the original code (about 40k tokens on M1). Pending work is now verified first, then continued.
15. Traces did not record tool error text. They now do.
16. Re-reading files: token-saving masking hid file contents the model still needed, so it read them again (8 re-reads on E2, 7 on M1). The latest still-current view of each file now stays visible within a budget; views of files edited since are marked outdated; a duplicate read is answered with "still shown above".
17. Missed exact-text edits: the `NO_MATCH` hint showed numbered lines, which models copied wrongly. It now shows the closest region as copy-ready raw text.

18. Efficiency pass:
    - the fixed prompt resent on every call went from about 2,160 to 1,810 tokens (−16%): skill names only, leaner tool schemas;
    - after each source edit the harness re-runs the last failing reproducer or test itself, if it takes under 30 s, and attaches a one-line result;
    - an identical check on identical code returns the earlier result instead of re-running it;
    - a tip suggests batching independent reads;
    - test failure output is scaled to the profile's budget.

19. DeepSeek support (the hackathon's expected model family):
    - `reasoning_content` from earlier turns is now sent back, as DeepSeek's thinking mode requires when tools are used (otherwise HTTP 400);
    - DeepSeek is auto-detected;
    - masking is off by default, so the append-only history hits DeepSeek's prefix cache (cache-hit input is about 50x cheaper).
20. Requirement-level checks, after SWE-Doctor (2026):
    - issues that describe several behaviours ask the model to list each one (at most 4) and check every one in its reproducer;
    - the self-review ticks the recorded list off;
    - one-line bugs and test-case-only tasks skip this.
21. A hint to run the most related existing tests early, after IssueExec (2026).

The unit suite covers every fix: 103 tests, all passing on Python 3.9 and 3.14.

### 7.4 Insights for the hackathon criteria

- **Correctness:** 5 of 5 resolved on 20–120B open models, including a test-case-only task and a multi-file feature.
- **Evidence:** 0 false VERIFIED on real runs. The honesty test shows that the verdict is only as strong as the evidence gathered, which is why the agent is pushed to cover every stated case.
- **Efficiency:**
  - Easy tasks took 8–16 calls and 22–48k input tokens. The outlier, E2 (27 calls), was caused by bug 10, now fixed.
  - Most input tokens are re-sent context. On large-context paid endpoints, prompt caching and masking reduce this further.
- **Reliability:** every provider failure mode seen (403, 429 per minute and per day, malformed tool calls, output limits) now degrades gracefully, never into a crash or a lost patch.
