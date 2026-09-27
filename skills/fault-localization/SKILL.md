---
name: fault-localization
description: Find the exact code responsible for the reported behaviour, broad to narrow. Use when the fix location is not yet confirmed or a previous hypothesis about the location was falsified.
---
# Fault localization

Work broad -> narrow; every search must answer a named question ("where is this error raised?").

1. Strongest anchors first: stack-trace frames (innermost repo frame), exact error message text
   (`search_code(mode=literal)`), names from the issue (`search_code(mode=symbol)`).
2. Skeleton before full read: `read_file(mode=skeleton)` to pick the function, then read only its line range.
3. Follow the data: from the entry point the issue uses, trace calls to where the wrong value is produced.
   Check callers (`search_code` for `name(`) and overrides/subclasses when behaviour differs by type.
4. Distinguish where the symptom *appears* from where the defect *originates*; fix the origin unless that is
   unsafe for other callers.
5. Confirm with evidence: a reproducer or a quick `run_command` probe that exercises the suspected line.
6. Record the location and ruled-out places in `update_plan(notes=...)`.

Stop when you have: a plausible execution path, the exact span to change, a way to check it, and no unexplored
dependency that could reverse the conclusion. Reopen localization when a check contradicts it.
