"""Transactional, exact-match editor with a syntax gate.

Rules:
  * every old_text must match EXACTLY ONCE in the (progressively edited) file - no fuzzy matching;
  * all edits in one call are validated in memory first, then written atomically;
  * if an edit breaks the syntax of a file that parsed before, nothing is written (auto-revert);
  * the result shows the changed hunk so the model can inspect what happened.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from harness.tools.diagnostics import introduced_problems
from harness.tools.files import ToolError, is_binary, rel, resolve_path


def syntax_error(path: Path, text: str) -> str | None:
    """Return an error message if `text` is syntactically invalid for its file type, else None."""
    suf = path.suffix.lower()
    try:
        if suf in (".py", ".pyi"):
            ast.parse(text, filename=str(path))
        elif suf == ".json":
            json.loads(text)
        elif suf == ".toml":
            try:
                import tomllib  # type: ignore
            except ModuleNotFoundError:
                return None
            tomllib.loads(text)
        elif suf in (".js", ".mjs", ".cjs") and shutil.which("node"):
            return _external_check(["node", "--check"], text, suf)
        elif suf == ".go" and shutil.which("gofmt"):
            return _external_check(["gofmt", "-e", "-l"], text, suf)
        elif suf in (".sh", ".bash") and shutil.which("bash"):
            return _external_check(["bash", "-n"], text, suf)
        elif suf == ".rb" and shutil.which("ruby"):
            return _external_check(["ruby", "-c"], text, suf)
        elif suf == ".php" and shutil.which("php"):
            return _external_check(["php", "-l"], text, suf)
    except SyntaxError as e:
        return f"SyntaxError: {e.msg} (line {e.lineno}, col {e.offset})"
    except ValueError as e:  # json / toml
        return f"{type(e).__name__}: {e}"
    return None


def _external_check(cmd: list[str], text: str, suffix: str) -> str | None:
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        proc = subprocess.run(cmd + [tmp], capture_output=True, timeout=30)
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout).decode("utf-8", "replace").replace(tmp, "<file>")
            return msg.strip()[:800] or "syntax check failed"
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return None


def _match_positions(content: str, needle: str) -> list[int]:
    pos, out = 0, []
    while True:
        i = content.find(needle, pos)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def _line_of(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _closest_hint(content: str, needle: str) -> str:
    lines = content.splitlines()
    nlines = needle.strip("\n").splitlines() or [needle]
    first = nlines[0].strip()
    best, best_i = 0.0, -1
    for i, line in enumerate(lines):
        r = difflib.SequenceMatcher(None, line.strip(), first).ratio()
        if r > best:
            best, best_i = r, i
    if best_i < 0 or best < 0.5:
        return "No similar text found; re-read the file to get the exact current content."
    s = best_i
    e = min(len(lines), best_i + len(nlines))
    raw = "\n".join(lines[k] for k in range(s, e))
    ws_note = ""
    if first and lines[best_i].strip() == first and lines[best_i] != nlines[0]:
        ws_note = " (the text matches except for leading whitespace/indentation)"
    # Raw text (no line-number prefixes) so it can be copied into old_text exactly.
    return (f"Closest region: lines {s + 1}-{e} (similarity {best:.2f}){ws_note}. Its exact current text, between the "
            f"markers:\n<<<\n{raw}\n>>>")


def _atomic_write(full: Path, text: str) -> None:
    full.parent.mkdir(parents=True, exist_ok=True)
    mode = full.stat().st_mode if full.exists() else None
    fd, tmp = tempfile.mkstemp(dir=str(full.parent), prefix=".harness_tmp_")
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8", "surrogateescape"))
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, full)


class Editor:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.authored: set[str] = set()  # paths written through the editor

    def edit(self, path: str, edits: list[dict]) -> tuple[str, list[str]]:
        """Apply one or more exact replacements to a single file, transactionally."""
        if not edits:
            raise ToolError("INVALID_ARGS", "no edits given (need old_text and new_text)")
        full = resolve_path(self.root, path)
        if full.is_dir():
            raise ToolError("IS_DIRECTORY", f"{path} is a directory")
        raw = full.read_bytes()
        if is_binary(raw):
            raise ToolError("BINARY_FILE", f"{path} is binary")
        original = raw.decode("utf-8", "surrogateescape")
        crlf = "\r\n" in original
        content = original
        for k, e in enumerate(edits, 1):
            old = e.get("old_text")
            new = e.get("new_text")
            if old is None or new is None:
                raise ToolError("INVALID_ARGS", f"edit #{k}: both old_text and new_text are required")
            if old == "":
                raise ToolError("INVALID_ARGS", f"edit #{k}: old_text is empty; use write_file to create files")
            if crlf:
                old = old.replace("\r\n", "\n").replace("\n", "\r\n")
                new = new.replace("\r\n", "\n").replace("\n", "\r\n")
            if old == new:
                raise ToolError("NO_OP_EDIT", f"edit #{k}: old_text and new_text are identical")
            pos = _match_positions(content, old)
            if not pos:
                raise ToolError(
                    "NO_MATCH",
                    f"edit #{k}: old_text not found in {rel(self.root, full)} (exact match required, including "
                    f"whitespace; nothing was changed).\n{_closest_hint(content, old)}",
                )
            if len(pos) > 1:
                lines = ", ".join(str(_line_of(content, p)) for p in pos[:10])
                raise ToolError(
                    "AMBIGUOUS_MATCH",
                    f"edit #{k}: old_text occurs {len(pos)} times (lines {lines}) in {rel(self.root, full)}; include more "
                    "surrounding lines so it is unique. Nothing was changed.",
                )
            content = content[: pos[0]] + new + content[pos[0] + len(old) :]
        err_after = syntax_error(full, content)
        if err_after and not syntax_error(full, original):
            raise ToolError(
                "SYNTAX_GATE",
                f"the edit would break {full.suffix} syntax of {rel(self.root, full)}: {err_after}. "
                "The file was NOT modified; fix indentation/brackets in new_text and retry.",
            )
        _atomic_write(full, content)
        r = rel(self.root, full)
        self.authored.add(r)
        return self._hunk(r, original, content) + introduced_problems(full, original, content), [r]

    def write(self, path: str, content: str, overwrite: bool = False) -> tuple[str, list[str]]:
        full = resolve_path(self.root, path, must_exist=False)
        r = rel(self.root, full)
        existed = full.exists()
        if existed and full.is_dir():
            raise ToolError("IS_DIRECTORY", f"{r} is a directory")
        if existed and not overwrite:
            raise ToolError("EXISTS", f"{r} already exists; use edit_file for changes, or set overwrite=true to replace it entirely")
        original = full.read_text(encoding="utf-8", errors="surrogateescape") if existed else ""
        err_after = syntax_error(full, content)
        if err_after and (not existed or not syntax_error(full, original)):
            raise ToolError("SYNTAX_GATE", f"content has invalid {full.suffix} syntax: {err_after}. File NOT written.")
        _atomic_write(full, content)
        self.authored.add(r)
        warn = introduced_problems(full, original, content)
        if existed:
            return self._hunk(r, original, content) + warn, [r]
        n = content.count("\n") + (0 if content.endswith("\n") else 1)
        return f"Created {r} ({n} lines).{warn}", [r]

    @staticmethod
    def _hunk(r: str, before: str, after: str, limit: int = 60) -> str:
        diff = list(
            difflib.unified_diff(before.splitlines(), after.splitlines(), f"a/{r}", f"b/{r}", n=2, lineterm="")
        )
        body = diff[:limit]
        more = f"\n... ({len(diff) - limit} more diff lines)" if len(diff) > limit else ""
        return f"Edited {r}. Resulting change:\n" + "\n".join(body) + more
