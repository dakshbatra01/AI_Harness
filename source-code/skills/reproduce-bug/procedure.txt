---
name: reproduce-bug
description: Write and run a minimal reproducer that fails on the original code for the issue's reason. Use before editing for any behavioural bug, and whenever verification reports missing or weak evidence.
---
# Reproduce the bug

1. Create `.harness_scratch/repro.py` (or `.sh`/`.js` matching the project). It is never part of the patch; the repo
   root is on PYTHONPATH.
2. Use the issue's exact example first, then one variation. Assert the EXPECTED behaviour:
   `assert result == expected, f"got {result!r}"`. Exit non-zero on failure, print "ok" on success.
3. Run it: `run_command(command="python .harness_scratch/repro.py")`. It must fail NOW, and fail for the issue's
   reason (the assertion or the reported exception) - not an ImportError or a typo in the script.
4. If it passes on the original code, it does not capture the bug: re-read the issue, adjust inputs/config.
   After 3 failed tries, continue without it and rely on tests; say so in your notes.
5. After the fix, run it again, then submit with `reproducer="python .harness_scratch/repro.py"`.

For crashes, assert no exception is raised. For wrong output, compare exact values. For warnings, use
`warnings.catch_warnings(record=True)`. Avoid network, timing and randomness in reproducers.
