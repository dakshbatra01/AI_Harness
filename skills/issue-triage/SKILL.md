---
name: issue-triage
description: Turn an ambiguous issue into checkable acceptance criteria before searching. Use at the start when the issue is long, vague, a feature request, or mixes several symptoms.
---
# Issue triage

1. Separate what the issue *states* (observed vs expected behaviour, versions, inputs) from what you *infer*.
2. Write 1-4 acceptance criteria as concrete, checkable behaviours: "calling X with Y returns Z", "no exception when ...".
   Prefer the issue's own example inputs; add one generalization (the fix must not only handle the example).
3. Classify: bug (wrong output / crash), feature (new behaviour), or compatibility/regression. For features, expected
   behaviour must come from the issue text or existing conventions in the codebase - never invent an API shape.
4. List anchors to search: exact error text, function/class names, file paths, config keys, CLI flags.
5. Record the criteria with `update_plan(acceptance=..., hypothesis=...)`.

Pitfalls: solving an adjacent problem; treating a workaround mentioned in the issue as the requested fix; ignoring
"should also" clauses; over-scoping to refactors the issue did not ask for.
