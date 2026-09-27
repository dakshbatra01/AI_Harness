---
name: patch-code
description: Make the smallest correct, general code change once the root cause is confirmed. Use when you are about to edit source files.
---
# Patch code

1. Re-read the exact span you will edit (fresh `read_file`), then `edit_file` with old_text copied verbatim
   (without line-number prefixes), including enough surrounding lines to be unique.
2. Fix the root cause generally: handle the class of inputs the issue describes, not only the example. Never
   special-case test values.
3. Minimal diff: no renames, reformatting, reordering imports, or unrelated cleanups. Keep public signatures
   backward compatible (add optional parameters with defaults rather than changing existing ones).
4. Mirror local conventions: exception types, messages, helper utilities that already exist in the module.
5. Consider all call sites and sibling implementations (subclasses, duplicate code paths, sync/async variants) that
   need the same fix.
6. If an edit is rejected (NO_MATCH / AMBIGUOUS / SYNTAX_GATE), read the reported region again - do not guess.
7. After the edit, immediately run the reproducer or the nearest test.

Adding a focused regression test in the existing test module is good practice; do not modify existing assertions.
