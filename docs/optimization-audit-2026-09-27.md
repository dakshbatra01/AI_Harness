# Harness optimization audit — 2026-09-27

## Contract and evidence

The submission PDF requires a root Makefile, clean `make setup`/`make run`, the evaluator's prescribed text-only model, and an `AI_API_KEY` supplied at runtime. The engineering brief leaves architecture choices to the team. The exact evaluator model and endpoint have not been confirmed; local testing is planned on Groq GPT-OSS 120B. The current provider adapter supports both that route and DeepSeek Flash with an explicit provider/model setting.

The baseline was 105 passing unit tests plus a `VERIFIED` smoke run. The offline honesty benchmark judged 24 of 26 scripted patch variants correctly; two narrow reproducers caused false `VERIFIED` results on E3 and M2. These variants pass one example while failing hidden cases. This is a real unresolved accuracy limit, not a score we should hide.

## Changes made

| Problem | Smallest option chosen | Acceptance evidence | Cost and rollback |
|---|---|---|---|
| A target command could have one fail→pass test and another still failing, yet support `issue_behavior`. | Report `UNKNOWN` until the remaining target failures are isolated or fixed. | Focused regression test; full unit suite and smoke pass. | May delay verification when a broad target command includes unrelated baseline failures. If real task resolution falls on paired eval, revert this branch and require issue-specific target IDs instead. |
| A process could exit nonzero while the parser saw only passing tests; an unavailable planned check could disappear from the evidence set. | Mark the corresponding issue or regression claim `UNKNOWN`. | Focused incomplete/nonzero test; full suite passes. | Some otherwise correct patches may need a working narrower command. Revert only after a paired eval shows a material resolution loss and an equally honest alternative. |
| An agent could submit enough optional commands to fill the ten-spec cap and displace independent related/full checks. | Schedule supplied targets and reproducer, then independent regression checks, then optional agent commands and prior observations. | Focused scheduling test; scripted fixture still resolves with seven model calls. | Optional commands near the cap may be deferred. Revert if paired real-model tasks lose issue evidence or resolution. |
| `read_log` could send hundreds of long lines back to the model. | Clip displayed lines and apply the existing head/tail budget; raw logs remain available by ID. | Focused 100-line/5,000-character test; unit suite passes. | A single call may show less log detail; the model can request another range or grep. Revert if necessary evidence becomes inaccessible. |
| Provider selection for the planned test/evaluator routes was undocumented. | Document explicit `AI_PROVIDER`/`AI_MODEL` examples and test endpoint construction without credentials. | Provider route test passes; `make setup` self-check passes. | Exact organizer setting still needs confirmation. |
| Running a second URL task hard-reset and cleaned the first task's clone, discarding its patch and untracked files. | Create a fresh clone per URL task. | Local-repository test confirms the second baseline is clean and the first patch survives. | Repeated URL tasks cost extra clone time and disk; use a proven safe cache only if this cost matters in real evals. |
| Generated benchmark traces occupied more than 100 MB in the working directory. | Ignore generated `eval/runs` and `eval/benchmark/results` in future repository submissions. | `.gitignore` covers both directories; existing local evidence remains available. | Archive or omit these generated directories if submitting a ZIP instead of Git. |

After these changes, 110 unit tests and the smoke run pass. The same 26 offline variants still score 24/26 with two false `VERIFIED` results. The scheduler and log budget have focused tests; the one-task scripted `make eval` remains resolved, `VERIFIED`, zero false `VERIFIED`, seven model calls, 9,126 reported input tokens. These are narrow checks, not evidence of an overall held-out gain. No live Groq or DeepSeek run was possible because no API key was available in this workspace.

## Research basis and decisions

- [SWE-bench's grader](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py) compares test identities and regressions. That supports the harness's base-versus-candidate evidence model and the target completeness fix.
- [Agentless](https://arxiv.org/abs/2407.01489) and [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent) are useful evidence that simple, tightly controlled agent workflows deserve a baseline before adding agent roles or retrieval layers. Their reported benchmark results are not predictions for this evaluator.
- [Groq's model list](https://console.groq.com/docs/models) lists `openai/gpt-oss-120b`, and [Groq's OpenAI compatibility guide](https://console.groq.com/docs/openai) gives `https://api.groq.com/openai/v1`. [DeepSeek's first-call guide](https://api-docs.deepseek.com/guides/harness) gives `deepseek-flash`. The harness uses these only when configured to use the respective provider.
- The [2026 empirical test-writing study](https://arxiv.org/abs/2602.07900) did not show a general outcome gain from simply varying the volume of agent-written tests in its setting. Do not add an unconditional test-generation loop without an ablation here.

## Next paired experiments

1. Confirm the organizer's exact model ID, endpoint, time limit, and task input protocol. Run `make setup` and a no-secret endpoint smoke test on its environment.
2. With a Groq key, run the same held-out tasks at fixed model settings and budgets, at least three seeds/runs per candidate: current controller versus a conditional requirement-coverage prompt or a second independent probe. Track resolution, false `VERIFIED`, tokens, latency, and model calls. Keep the new step only if it reduces overfit without material resolution loss.
3. Add new unseen tasks for multi-case input handling, stale evidence, and broad target suites. The E3/M2 examples used during development are no longer a clean held-out set.
4. Profile very large command/test output. `CommandRunner` and test parsing still load whole raw logs into memory. A streaming parser should be considered only after measuring peak memory and preserving per-test identity recall.

Do not advertise the current two overfit cases as solved. The verifier proves only the behaviors exercised by its tests; issue-wide generalization needs broader independent evidence.
