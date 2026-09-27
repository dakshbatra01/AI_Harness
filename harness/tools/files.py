"""Path safety, bounded numbered file views, and signature-only skeleton views."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from harness.util import clip_line, sha256_bytes


class ToolError(Exception):
    """Actionable, model-facing error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def resolve_path(root: Path, path: str, must_exist: bool = True) -> Path:
    if not path or not str(path).strip():
        raise ToolError("INVALID_PATH", "path is empty")
    p = str(path).strip()
    cand = Path(p)
    if cand.is_absolute():
        full = cand.resolve()
    else:
        full = (root / p).resolve()
    if full != root and root not in full.parents:
        raise ToolError("OUTSIDE_WORKSPACE", f"'{path}' resolves outside the repository; use repo-relative paths")
    rel = full.relative_to(root).as_posix() if full != root else "."
    if rel == ".git" or rel.startswith(".git/"):
        raise ToolError("FORBIDDEN_PATH", "the .git directory is off limits")
    if must_exist and not full.exists():
        raise ToolError("NOT_FOUND", f"'{rel}' does not exist. Use search_code(mode=file) to find the right path")
    return full


def rel(root: Path, full: Path) -> str:
    return full.relative_to(root).as_posix()


def file_hash(full: Path) -> str:
    return sha256_bytes(full.read_bytes())[:16]


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def read_text(full: Path) -> str:
    data = full.read_bytes()
    if is_binary(data):
        raise ToolError("BINARY_FILE", f"{full.name} is binary")
    return data.decode("utf-8", "replace")


def view_lines(root: Path, path: str, start: int | None, end: int | None, window: int) -> tuple[str, dict]:
    full = resolve_path(root, path)
    if full.is_dir():
        return list_dir(root, full), {"path": rel(root, full), "dir": True}
    text = read_text(full)
    lines = text.splitlines()
    n = len(lines)
    s = max(1, int(start or 1))
    e = int(end) if end else s + window - 1
    e = min(max(e, s), n, s + max(window * 3, 400) - 1)
    h = file_hash(full)
    r = rel(root, full)
    if n == 0:
        return f"[{r}] (empty file) sha={h}", {"path": r, "sha": h, "range": (0, 0)}
    if s > n:
        raise ToolError("OUT_OF_RANGE", f"{r} has only {n} lines")
    body = "\n".join(f"{i:>6}| {clip_line(lines[i - 1], 500)}" for i in range(s, e + 1))
    header = f"[{r}] lines {s}-{e} of {n} | sha={h}"
    footer = []
    if s > 1:
        footer.append(f"lines 1-{s - 1} above not shown")
    if e < n:
        footer.append(f"lines {e + 1}-{n} below not shown (read_file start={e + 1})")
    out = header + "\n" + body + ("\n(" + "; ".join(footer) + ")" if footer else "")
    return out, {"path": r, "sha": h, "range": (s, e)}


def list_dir(root: Path, full: Path, limit: int = 200) -> str:
    items = []
    for child in sorted(full.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
        if child.name in (".git",) or child.name == "__pycache__":
            continue
        items.append(child.name + ("/" if child.is_dir() else ""))
    r = rel(root, full) if full != root else "."
    more = f"\n... {len(items) - limit} more" if len(items) > limit else ""
    return f"[{r}/] directory listing:\n" + "\n".join(items[:limit]) + more


# ---- skeleton ----------------------------------------------------------------------------
_GENERIC_DEF = re.compile(
    r"^\s*(export\s+)?(default\s+)?(public|private|protected|static|async|abstract|final|pub(\(crate\))?|\s)*"
    r"(def|class|function|func|fn|interface|struct|enum|trait|impl|type|module|object|record)\b[^\n]{0,160}"
)
_ARROW_DEF = re.compile(r"^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(async\s+)?(\([^)]*\)|\w+)\s*=>")


def skeleton(root: Path, path: str, max_lines: int = 250) -> str:
    full = resolve_path(root, path)
    text = read_text(full)
    r = rel(root, full)
    out: list[str] = []
    if full.suffix == ".py":
        try:
            tree = ast.parse(text)
            _py_skeleton(tree, text.splitlines(), out, 0)
        except SyntaxError as e:
            out.append(f"(python parse failed: {e}; showing regex outline)")
            out.extend(_regex_outline(text))
    else:
        out.extend(_regex_outline(text))
    if not out:
        out.append("(no definitions found)")
    more = f"\n... {len(out) - max_lines} more entries" if len(out) > max_lines else ""
    return f"[{r}] skeleton ({len(text.splitlines())} lines, sha={file_hash(full)}):\n" + "\n".join(out[:max_lines]) + more


def _py_skeleton(node, lines: list[str], out: list[str], depth: int) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ln = child.lineno
            end = getattr(child, "end_lineno", ln)
            sig = lines[ln - 1].strip() if ln - 1 < len(lines) else child.name
            # include decorators and multi-line signatures compactly
            j = ln
            while not sig.rstrip().endswith(":") and j < min(len(lines), ln + 6):
                sig += " " + lines[j].strip()
                j += 1
            doc = ast.get_docstring(child)
            doc_s = f'  # "{doc.strip().splitlines()[0][:90]}"' if doc else ""
            out.append(f"{'    ' * depth}{ln:>5}-{end:<5} {clip_line(sig, 200)}{doc_s}")
            if isinstance(child, ast.ClassDef) or depth == 0:
                _py_skeleton(child, lines, out, depth + 1)
        elif depth == 0 and isinstance(child, (ast.Assign, ast.AnnAssign)) and child.lineno - 1 < len(lines):
            line = lines[child.lineno - 1].strip()
            if re.match(r"^[A-Z_][A-Z0-9_]*\s*[:=]", line):
                out.append(f"{child.lineno:>5}       {clip_line(line, 120)}")


def _regex_outline(text: str) -> list[str]:
    res = []
    for i, line in enumerate(text.splitlines(), 1):
        if _GENERIC_DEF.match(line) or _ARROW_DEF.match(line):
            res.append(f"{i:>5}  {clip_line(line.rstrip(), 180)}")
    return res
