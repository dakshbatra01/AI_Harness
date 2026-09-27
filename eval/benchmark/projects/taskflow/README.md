# taskflow

A small task-pipeline runner. Tasks name an action and their dependencies; the engine runs them in
dependency order and reports results.

```json
{"tasks": [
  {"name": "build", "action": "echo", "args": {"message": "built"}, "aliases": ["compile"]},
  {"name": "deploy", "action": "echo", "args": {"message": "shipped"}, "deps": ["build"]}
]}
```

    python -m taskflow run pipeline.json [target ...]

A dependency may refer to a task by its name or by any of its aliases (names are case-insensitive).
Unknown dependencies are reported as warnings and ignored.
