---
name: final-review
description: Review the complete diff for correctness, scope and leftovers right before submitting. Use when you believe the fix is done.
---
# Final review

1. `repo_changes()` and read the whole diff.
2. Every hunk must be explained by the issue. Remove debug prints, commented-out code, stray files, unrelated
   formatting changes.
3. Re-check the acceptance criteria against the code: each one handled, including the generalization beyond the
   issue's example.
4. Edge cases: None/empty inputs, types the function already accepted, error messages, backwards compatibility.
5. Make sure the evidence exists: a reproducer and/or test that fails on the original code and passes now, and the
   related test file still passes.
6. Submit with a precise summary (root cause -> change) and the evidence commands.
