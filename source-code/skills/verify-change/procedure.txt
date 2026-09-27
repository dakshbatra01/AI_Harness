---
name: verify-change
description: Check a change with the right tests before submitting - target tests, then related tests, comparing against the original code. Use after an edit and before submit.
---
# Verify a change

1. Cheapest first: the reproducer, then the specific test file for the changed module:
   `run_tests(tests=["path/to/test_module.py"])` (or node ids `file::Class::test`).
2. Read the comparison the harness prints: `fixed` (fail->pass) is your evidence; `regressed` (pass->fail) must be
   fixed; `still_failing` tests that also failed on the original code are pre-existing - ignore unless related.
3. Run tests for other modules that import what you changed if the change affects shared behaviour
   (`search_code(query="from <module> import")` to find them).
4. A test moving to SKIP, disappearing, or failing to collect is not a pass.
5. Submit with evidence: `submit(summary=..., test_commands=[...], fail_to_pass=[ids], reproducer=...)`.
   The harness re-runs everything on both original and patched code and decides.

Never edit tests to make them pass. If an existing test encodes the old (buggy) behaviour and the issue explicitly
requires the new behaviour, update only that assertion and explain why in the summary.
