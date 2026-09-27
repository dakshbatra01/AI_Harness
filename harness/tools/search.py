"""Bounded lexical search: ripgrep when installed, else `git grep` over the shadow index,
else a pure-Python scan. Results are grouped per file, ranked by a localization prior,
and capped with an explicit "narrow your query" note."""
from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from harness.util import clip_line, scrubbed_env

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".tox", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".harness_scratch", "dist", "build", ".eggs"}

SYMBOL_TEMPLATES = [
    r"^\s*(async\s+)?def\s+{n}\b",
    r"^\s*class\s+{n}\b",
    r"^\s*(export\s+)?(default\s+)?(async\s+)?function\*?\s+{n}\b",
    r"^\s*(export\s+)?(const|let|var)\s+{n}\s*=",
    r"^\s*func\s+(\([^)]*\)\s*)?{n}\b",
    r"^\s*(pub(\([a-z]+\))?\s+)?(async\s+)?(fn|struct|enum|trait|type|mod)\s+{n}\b",
    r"^\s*((public|private|protected|internal|static|final|abstract|sealed|open|override|synchronized)\s+)*(class|interface|enum|record|object)\s+{n}\b",
    r"^\s*((public|private|protected|internal|static|final|abstract|override|synchronized)\s+)+[\w<>\[\],.?]+\s+{n}\s*\(",
    r"^\s*{n}\s*[:=]\s*",
    r"^\s*(def|class|module)\s+(self\.)?{n}\b",
]


class Searcher:
    def __init__(self, root: Path, workspace=None) -> None:
        self.root = Path(root).resolve()
        self.ws = workspace
        self.rg = shutil.which("rg")
        self.prior: dict[str, float] = {}

    def set_prior(self, prior: dict[str, float]) -> None:
        self.prior = dict(prior)

    # ---- public ------------------------------------------------------------------------
    def search(self, query: str, path: str = ".", mode: str = "regex", max_hits: int = 50, glob: str | None = None) -> str:
        if not query:
            return "ERROR INVALID_ARGS: query is empty"
        mode = (mode or "regex").lower()
        if mode == "file":
            return self._file_search(query, max_hits)
        if mode == "symbol":
            # Accept what models naturally type: "def loads", "json.loads", "Foo.bar()", "class Foo".
            q = re.sub(r"^\s*(async\s+def|def|class|function|func|fn|struct|interface|type)\s+", "", query.strip())
            q = q.split("(")[0].strip().split(".")[-1].split("::")[-1]
            name = re.escape(q or query.strip())
            pattern = "|".join("(" + t.replace("{n}", name) + ")" for t in SYMBOL_TEMPLATES)
            hits, err = self._grep(pattern, path, regex=True, glob=glob, ignore_case=False)
            label = f"definitions of '{query}'"
            parts = re.split(r"\.|::", query.strip().split("(")[0])
            if len(parts) >= 2 and re.fullmatch(r"[A-Za-z_]\w*", parts[-2] or "") and not err:
                owner = re.escape(parts[-2])
                owners = self.count_files(rf"^\s*(class|struct|interface|trait|impl|type)\s+{owner}\b|^\s*(export\s+)?class\s+{owner}\b", regex=True)
                if owners:
                    hits = [h for h in hits if h[0] in owners] + [h for h in hits if h[0] not in owners]
                    return self._format(hits, label, max_hits, boost=set(owners))
        else:
            regex = mode == "regex"
            if regex:
                try:
                    re.compile(query)
                except re.error as e:
                    return f"ERROR INVALID_REGEX: {e}. Retry with mode=literal or escape special characters."
            hits, err = self._grep(query, path, regex=regex, glob=glob, ignore_case=False)
            label = f"'{query}'"
        if err:
            return f"ERROR SEARCH_FAILED: {err}"
        return self._format(hits, label, max_hits)

    def count_files(self, query: str, regex: bool = False) -> dict[str, int]:
        hits, _ = self._grep(query, ".", regex=regex, glob=None, ignore_case=False, timeout=20)
        counts: dict[str, int] = {}
        for f, _, _ in hits:
            counts[f] = counts.get(f, 0) + 1
        return counts

    def list_files(self) -> list[str]:
        if self.ws is not None:
            try:
                self.ws.git("add", "-A", ".")
                out = self.ws.git("ls-files", "-z").stdout.decode("utf-8", "surrogateescape")
                return [p for p in out.split("\0") if p]
            except Exception:
                pass
        files = []
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in fns:
                files.append(Path(dp, fn).relative_to(self.root).as_posix())
        return files

    # ---- internals ---------------------------------------------------------------------
    def _file_search(self, query: str, max_hits: int) -> str:
        files = self.list_files()
        q = query.strip()
        if any(ch in q for ch in "*?["):
            matches = [f for f in files if fnmatch.fnmatch(f, q) or fnmatch.fnmatch(os.path.basename(f), q)]
        else:
            ql = q.lower()
            matches = [f for f in files if ql in f.lower()]
        matches.sort(key=lambda f: (-self.prior.get(f, 0), len(f), f))
        if not matches:
            return f"No files match '{query}'."
        shown = matches[:max_hits]
        more = f"\n... {len(matches) - len(shown)} more; narrow the pattern" if len(matches) > len(shown) else ""
        return f"{len(matches)} file(s) match '{query}':\n" + "\n".join(shown) + more

    def _grep(self, pattern: str, path: str, regex: bool, glob: str | None, ignore_case: bool, timeout: int = 60):
        target = (self.root / (path or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            return [], "path must be inside the repository"
        if not target.exists():
            return [], f"path '{path}' does not exist"
        rel_target = "." if target == self.root else target.relative_to(self.root).as_posix()
        if self.rg:
            cmd = [self.rg, "-n", "--no-heading", "--color", "never", "-M", "400", "--max-count", "200", "-g", "!.harness_scratch"]
            if not regex:
                cmd.append("-F")
            if ignore_case:
                cmd.append("-i")
            if glob:
                cmd += ["-g", glob]
            cmd += ["-e", pattern, "--", rel_target]
            env = scrubbed_env()
        elif self.ws is not None:
            self.ws.git("add", "-A", ".")
            cmd = ["git", "-c", "core.quotepath=off", "grep", "-n", "-I", "--no-color", "-P" if regex else "-F"]
            if ignore_case:
                cmd.append("-i")
            if glob:
                spec = f":(glob){rel_target}/**/{glob}" if rel_target != "." else f":(glob)**/{glob}"
            else:
                spec = rel_target
            cmd += ["-e", pattern, "--", spec]
            env = self.ws._env()
            env.pop("GIT_LITERAL_PATHSPECS", None)
        else:
            return self._py_grep(pattern, target, regex, ignore_case), None
        try:
            proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return [], "search timed out; narrow the path or pattern"
        if proc.returncode not in (0, 1):
            err = proc.stderr.decode("utf-8", "replace")[:400]
            if not self.rg and "-P" in cmd:  # git without PCRE: fall back to ERE
                cmd[cmd.index("-P")] = "-E"
                proc = subprocess.run(cmd, cwd=self.root, env=env, capture_output=True, timeout=timeout)
                if proc.returncode not in (0, 1):
                    return self._py_grep(pattern, target, regex, ignore_case), None
            else:
                return [], err
        hits = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            m = re.match(r"^(.*?):(\d+):(.*)$", line)
            if m:
                f = m.group(1)
                if f.startswith("./"):
                    f = f[2:]
                hits.append((f, int(m.group(2)), m.group(3)))
        return hits, None

    def _py_grep(self, pattern: str, target: Path, regex: bool, ignore_case: bool):
        flags = re.I if ignore_case else 0
        rx = re.compile(pattern if regex else re.escape(pattern), flags)
        hits = []
        paths = [target] if target.is_file() else []
        if target.is_dir():
            for dp, dns, fns in os.walk(target):
                dns[:] = [d for d in dns if d not in SKIP_DIRS]
                paths.extend(Path(dp, fn) for fn in fns)
        for p in paths:
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096] or len(data) > 2_000_000:
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    hits.append((p.relative_to(self.root).as_posix(), i, line))
        return hits

    def _format(self, hits, label: str, max_hits: int, boost: set | None = None) -> str:
        if not hits:
            return f"No matches for {label}."
        by_file: dict[str, list] = {}
        for f, ln, text in hits:
            by_file.setdefault(f, []).append((ln, text))
        boost = boost or set()
        files = sorted(by_file, key=lambda f: (f not in boost, -self.prior.get(f, 0), _is_test(f), f))
        total = len(hits)
        out = [f"{total} match(es) for {label} in {len(files)} file(s):"]
        shown = 0
        hidden_files = []
        for f in files:
            if shown >= max_hits:
                hidden_files.append(f"{f} ({len(by_file[f])})")
                continue
            out.append(f"{f}:")
            file_hits = by_file[f]
            cap = min(12, max_hits - shown)
            for ln, text in file_hits[:cap]:
                out.append(f"  {ln}: {clip_line(text.strip(), 220)}")
                shown += 1
            if len(file_hits) > cap:
                out.append(f"  ... {len(file_hits) - cap} more matches in this file")
        if hidden_files:
            out.append(f"... {total - shown} more matches in {len(hidden_files)} more file(s) (narrow the query or path): " + ", ".join(hidden_files[:25]))
        return "\n".join(out)


def _is_test(path: str) -> bool:
    p = path.lower()
    return any(s in p for s in ("test", "spec", "fixture"))
