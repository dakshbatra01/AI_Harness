# Declared target coverage: 2026-09-27

## Problem and evidence

The verifier could recommend `VERIFIED` after one declared test changed from fail to pass even when a second declared test ID never appeared in candidate output. A focused regression test reproduced that false verdict before the change. A declared test that was skipped had the same gap.

The existing offline verdict benchmark contained 26 scripted patch variants: 24 honest verdicts and two false `VERIFIED` results caused by narrow reproducers for E3 and M2. This change does not address those two variants.

## Method and trade-offs

Require every declared target ID to match a candidate test identity with a passing outcome. A failure contradicts the issue claim; missing or skipped outcomes leave it unknown. File and class targets match descendants on component boundaries, so a file target can cover its tests without matching a similarly named file. This runs over results already collected by the verifier and adds no model, tool, or test invocation.

When syntax or diff audit already contradicts a mandatory claim, stop before running candidate/base tests. The controller's recovery path already handles those blockers first. The report leaves unchecked claims `UNKNOWN`, so it cannot silently treat skipped tests as passing.

The [SWE-bench grader](https://github.com/SWE-bench/SWE-bench/blob/main/swebench/harness/grading.py) checks resolution and maintenance by test identity. Its [skipped-test regression tests](https://github.com/SWE-bench/SWE-bench/blob/main/tests/test_grading_skipped.py) show why a skipped resolution test cannot count as a fix. [Agentless](https://arxiv.org/abs/2407.01489) supports a simple localization, repair, validation loop. A [2026 study of agent-written tests](https://arxiv.org/abs/2602.07900) found that changing test-writing volume did not significantly change outcomes in its experiments; that is a reason to measure a test-generation layer before adding one here, not proof it would never help.

Alternative: generate more reproducer tests or add a reviewer model. Those approaches may expose narrow overfits, but add model calls, latency, and potentially weak test or review evidence. The deterministic target coverage check addresses the measured false verdict directly.

## Acceptance and paired checks

- The new focused case changed from false `VERIFIED` to `INCONCLUSIVE`; a skipped declared target is also `INCONCLUSIVE`.
- `make setup` passed. Final `make test`: 105 unit tests and offline smoke passed.
- Offline verdict benchmark: 24/26 honest verdicts, two false `VERIFIED`, unchanged; all three E4 variants retained their expected verdicts.
- On three audit-blocked benchmark patches (E1 overfit, E1 cheat, H1 cheat), verification ran 4 specs per patch before and 0 after. Their combined wall time, including hidden grading, was 6.4 s before and 2.1 s after. All 26 patch verdicts stayed the same; total benchmark wall time was 54.6 s before and 50.3 s after. These are single scripted runs, so wall-time differences are indicative, not a stable speedup estimate.
- Scripted `make eval` after both changes: fixture resolved, `VERIFIED`, zero false `VERIFIED`, seven model calls. The earlier saved fixture also resolved in seven calls. Token and wall-time totals from these two runs are not a controlled efficiency comparison.

The target-coverage check consumes no extra model calls or test runs. The audit early stop avoids tests only for patches that cannot be verified in their current form. Missing parser identities can now produce a conservative `INCONCLUSIVE`; the focused file-target matching test protects the common grouped-target case. Audit-blocked reports give less test feedback until the blocking patch issue is repaired.

Next paired evaluation: run the same held-out tasks with the prescribed model, settings, commits, and budget before and after the change. Compare resolved tasks, false `VERIFIED`, model calls, tokens, and wall time. If legitimate targets become unmatchable and resolution falls, revert the target-matching and issue-claim changes together.
