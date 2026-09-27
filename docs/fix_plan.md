# Experiment backlog (one change per L0 iteration; keep only if it passes the §14.6 gate)

Baseline first: `make eval` on >= 20 real tasks with the prescribed model. Record resolve rate, false-VERIFIED rate,
calls, tokens, wall time. Read >= 10 transcripts before choosing the next item.

- [ ] Build a dev task set: 20-50 SWE-bench Verified/Pro instances (repo+commit+issue+hidden tests) in eval/tasks.json.
- [ ] Ablation: native tool calling vs text protocol (`AI_TOOL_MODE=text`) on the prescribed model.
- [ ] Ablation: masking on/off (`HARNESS__MODEL__MASK_OBSERVATIONS=on|off`) - tokens vs resolution.
- [ ] Ablation: max_attempts 1 vs 2 (L1 fresh-context restart value).
- [ ] Tune routing thresholds (controller._route) from traces: does LIGHT_PLAN/STRUCTURED help?
- [ ] Tune verification breadth: related_test_limit, full_suite auto threshold vs wall-time.
- [ ] Localization metric: file Recall@1/3/5 of the orientation candidate list vs gold patch files.
- [ ] Conditional: tree-sitter symbol index / repo map for large non-Python repos (only if localization misses).
- [ ] Conditional: best-of-N escalation when attempt 1 is not VERIFIED and budget remains.
- [ ] Conditional: SBFL ranking when a failing test exists.
