---
name: failure-recovery
description: Recover when checks keep failing, verification rejects the patch, or the harness reports a loop. Use after a failed verification or a stuck warning.
---
# Failure recovery

1. Classify the failure from the evidence, not from memory:
   syntax/import error | target still failing | new regression | wrong location | misunderstood requirement |
   environment/tool problem | no progress.
2. Extract new information before acting: read the failing assertion and traceback (`read_log` with `grep`), and
   compare expected vs actual values. Write down what this falsifies.
3. Change at least one of: information (read the real code path, callers, tests), hypothesis, code, or strategy.
   Retrying the same edit or the same command is not recovery.
4. Ladder: 1st failure -> inspect evidence; 2nd identical failure -> your hypothesis is wrong, re-localize;
   3rd -> abandon the approach (`repo_changes(action="revert", paths=[...])`) and try a different root cause.
5. Regressions: find which behaviour the broken test expects and make the fix preserve it (often the fix belongs
   deeper or needs a narrower condition).
6. Record the failed hypothesis and the new one with `update_plan` so it is not repeated.
