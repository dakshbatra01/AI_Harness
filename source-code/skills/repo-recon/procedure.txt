---
name: repo-recon
description: Orient in an unfamiliar repository - layout, entry points, conventions, and how to run its tests. Use when the candidate list is weak or the codebase structure is unclear.
---
# Repository reconnaissance

1. Read the orientation block first (layout, detected test framework, candidate files). Do not re-list what it shows.
2. Find the package root and entry points: `read_file` the top-level package directory; `read_file(mode=skeleton)` on
   the 1-3 most relevant modules instead of reading them fully.
3. Find how tests are organized: locate the tests directory mirroring the module you will touch
   (`search_code(mode=file, query="test_<module>")`).
4. Confirm one test command works early: `run_tests(tests=[<one small test file>])`. If it fails to collect/import,
   read the error - the environment may need a specific invocation (e.g. Django's `tests/runtests.py`).
5. Note conventions to mirror: error types raised, logging style, typing, docstring format, compatibility shims.

Budget: recon should take a few steps, not a tour of the codebase. Stop when you know where to look.
