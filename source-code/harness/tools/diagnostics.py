"""Post-edit diagnostics: report problems an edit INTRODUCED, never pre-existing ones.

The syntax gate (editor.py) already blocks unparsable edits. This layer catches the next most common
agent mistake - using a name that is not defined anywhere (missing import, typo, renamed variable) -
which parses fine but fails at runtime, usually only when the right test runs.

The checker is deliberately conservative (scope-insensitive: a name counts as defined if it is bound
anywhere in the module), so it reports few false positives and runs in milliseconds with no
third-party tools. Findings are warnings: the edit is kept, the model decides."""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

_IMPLICIT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__", "__builtins__", "__path__",
    "__annotations__", "__dict__", "__module__", "__qualname__", "__class__", "__all__", "__debug__",
    "__version__", "reveal_type", "__cached__",
}
_KNOWN = set(dir(builtins)) | _IMPLICIT


def undefined_names(source: str) -> dict[str, int] | None:
    """Names loaded but never bound anywhere in the module -> first line. None if not analysable
    (syntax error or star import / dynamic namespace tricks that make the answer unknowable)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    bound: set[str] = set()
    used: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)
            else:
                bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return None
                bound.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("exec", "globals", "locals", "vars"):
            return None  # namespace is manipulated dynamically; stay silent rather than guess
        else:
            for attr in ("name", "rest"):  # match-case captures, PEP 695 type params
                v = getattr(node, attr, None)
                if isinstance(v, str) and type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping", "TypeVar", "ParamSpec", "TypeVarTuple"):
                    bound.add(v)
    return {n: ln for n, ln in used.items() if n not in bound and n not in _KNOWN}


def introduced_problems(path: Path, before: str, after: str) -> str:
    """Human/model-readable warning about problems present after the edit but not before ('' if none)."""
    if path.suffix not in (".py", ".pyi"):
        return ""
    new = undefined_names(after)
    if not new:
        return ""
    old = undefined_names(before) or {}
    fresh = {n: ln for n, ln in new.items() if n not in old}
    if not fresh:
        return ""
    items = ", ".join(f"`{n}` (line {ln})" for n, ln in sorted(fresh.items(), key=lambda x: x[1])[:8])
    return (f"\n[diagnostics] this edit uses name(s) that are not defined anywhere in the file: {items}. "
            "Missing import, typo, or renamed variable? (warning only - the edit was applied)")
