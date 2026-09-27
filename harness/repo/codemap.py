"""Ranked code map: a token-budgeted outline of the parts of the repository most related to the issue.

Why: the dominant token cost of an agent run is exploration - every search/read turn re-sends the whole
conversation. Starting the model with a compact, issue-ranked outline (file -> key signatures) replaces
several of those turns.

How:
  1. Index every source file once: definitions (Python via `ast`, other languages via declaration
     patterns) and the identifiers each file uses.
  2. Build a usage graph: file A -> file B when A uses a symbol that B defines. Symbols defined in many
     places are ambiguous and get little weight; generic names are ignored.
  3. Rank files with a random walk that restarts at the issue's evidence (files named in the issue, in
     stack traces, or defining identifiers it mentions). Files the evidence *depends on* rank highly.
  4. Render signatures of the top files until the token budget is spent.

The same graph answers "which tests exercise this file?" for regression selection.
Bounded by a file cap and a wall-clock budget; a partial index is still usable."""
from __future__ import annotations

import ast
import math
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from harness.repo.manifest import LANG_BY_EXT, is_test_path
from harness.util import clip_line, estimate_tokens

_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_DECL = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([a-z]+\))?\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|static\s+|"
    r"final\s+|abstract\s+|async\s+|override\s+|open\s+|sealed\s+|inline\s+|unsafe\s+)*"
    r"(?:def|class|function\*?|func|fn|interface|struct|enum|trait|type|module|object|record|protocol)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)"
)
_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_]\w*)\s*=>")
_GENERIC = frozenset(
    "self cls None True False the and for not get set add run main test init data name value type len str int dict "
    "list tuple object args kwargs result return import from class def function const let var this new delete update "
    "create items keys values path file open close read write start stop call apply setup teardown config options "
    "params index key val obj err error exception message format string number bool float default other copy clear "
    "size count parse load save next prev node item info log debug warn text line lines check validate handle process "
    "execute build make render reset length push pop append extend insert remove equals hash toString main".split()
)
SKIP_DIRS = {".git", "node_modules", "vendor", "third_party", "dist", "build", ".tox", ".venv", "venv", "__pycache__",
             "site-packages", ".mypy_cache", ".pytest_cache", "target", ".harness_scratch", ".eggs"}


@dataclass
class Definition:
    name: str
    line: int
    signature: str
    depth: int = 0
    parent: int = -1  # index of the enclosing class definition in FileIndex.defs, -1 at module level


@dataclass
class FileIndex:
    path: str
    defs: list[Definition] = field(default_factory=list)
    uses: Counter = field(default_factory=Counter)
    imports: list = field(default_factory=list)  # module names / relative specifiers this file imports
    is_test: bool = False


class CodeMap:
    def __init__(self, root: Path, files: list[str], time_budget_s: float = 8.0, max_files: int = 6000,
                 max_bytes: int = 400_000) -> None:
        self.root = Path(root)
        self.files: dict[str, FileIndex] = {}
        self.definers: dict[str, set[str]] = defaultdict(set)
        self.edges: dict[str, dict[str, float]] = defaultdict(dict)
        self.complete = True
        self.build_seconds = 0.0
        self.use_files: Counter = Counter()  # symbol -> number of files using it (importance proxy)
        self._build(files, time_budget_s, max_files, max_bytes)

    # ---- indexing -------------------------------------------------------------------------------
    def _build(self, files: list[str], budget: float, max_files: int, max_bytes: int) -> None:
        t0 = time.time()
        code = [f for f in files if os.path.splitext(f)[1].lower() in LANG_BY_EXT
                and not (set(f.split("/")[:-1]) & SKIP_DIRS)]
        code.sort(key=lambda f: (is_test_path(f), f.count("/"), f))  # sources first if we run out of time
        for n, rel in enumerate(code):
            if n >= max_files or time.time() - t0 > budget:
                self.complete = False
                break
            p = self.root / rel
            try:
                if p.stat().st_size > max_bytes:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fi = FileIndex(rel, is_test=is_test_path(rel))
            if rel.endswith((".py", ".pyi")):
                fi.defs, fi.imports = _python_index(text, rel)
            else:
                fi.defs, fi.imports = _pattern_defs(text), _JS_IMPORT.findall(text) if rel.endswith(_JS_EXT) else []
            fi.uses = Counter(t for t in _IDENT.findall(text) if t not in _GENERIC)
            self.files[rel] = fi
            for d in fi.defs:
                # Only module-level names identify a file; method names (split, items, get...) collide with
                # calls on unrelated objects and would link everything to everything.
                if d.depth == 0 and d.name not in _GENERIC and not d.name.startswith("__"):
                    self.definers[d.name].add(rel)
        for fi in self.files.values():
            self.use_files.update(fi.uses.keys())
        modules = _module_table(self.files)
        for rel, fi in self.files.items():
            own = {d.name for d in fi.defs}
            out = self.edges[rel]
            for name, cnt in fi.uses.items():
                where = self.definers.get(name)
                if not where or name in own or len(where) > 6 or not _informative(name):
                    continue
                w = math.sqrt(cnt) / len(where)
                for target in where:
                    if target != rel:
                        out[target] = out.get(target, 0.0) + w
            # An import is explicit evidence of dependency: weigh it above name co-occurrence.
            for spec in fi.imports:
                target = _resolve_import(spec, rel, modules, self.files)
                if target and target != rel:
                    out[target] = out.get(target, 0.0) + 3.0
        self.build_seconds = time.time() - t0

    # ---- ranking --------------------------------------------------------------------------------
    def rank(self, seeds: dict[str, float], iterations: int = 30, damping: float = 0.5) -> dict[str, float]:
        """Random walk with restart at the seed distribution (uniform when there is no evidence).
        A low damping keeps the mass near the evidence: we want the issue's neighbourhood, not global hubs."""
        nodes = list(self.files)
        if not nodes:
            return {}
        seed = {f: w for f, w in seeds.items() if f in self.files and w > 0}
        total = sum(seed.values())
        restart = {f: w / total for f, w in seed.items()} if total else {f: 1 / len(nodes) for f in nodes}
        score = dict(restart)
        out_sum = {f: sum(e.values()) for f, e in self.edges.items()}
        for _ in range(iterations):
            nxt: dict[str, float] = defaultdict(float)
            dangling = 0.0
            for f, s in score.items():
                tot = out_sum.get(f, 0.0)
                if tot <= 0:
                    dangling += s
                    continue
                for g, w in self.edges[f].items():
                    nxt[g] += damping * s * w / tot
            for f, r in restart.items():
                nxt[f] += (1 - damping) * r + damping * dangling * r
            score = nxt
        return dict(score)

    def tests_using(self, sources: list[str], limit: int = 4) -> list[str]:
        """Test files that use symbols defined in `sources`, strongest first."""
        wanted = set(sources)
        scored = []
        for rel, fi in self.files.items():
            base = rel.rsplit("/", 1)[-1]
            if not fi.is_test or rel in wanted or base in ("__init__.py", "conftest.py") or "test" not in base.lower():
                continue
            w = sum(self.edges[rel].get(s, 0.0) for s in wanted)
            if w > 0:
                scored.append((w, rel))
        return [r for _, r in sorted(scored, reverse=True)[:limit]]

    # ---- rendering ------------------------------------------------------------------------------
    def render(self, seeds: dict[str, float], issue_terms: set[str], token_budget: int = 1200, min_share: float = 0.3,
               max_neighbours: int = 8) -> str:
        ranks = self.rank(seeds)
        indeg: Counter = Counter(g for e in self.edges.values() for g in e)
        # Widely-imported utility modules soak up random-walk mass without being relevant: damp them,
        # except where the issue's own evidence points.
        adj = {f: r / (1.0 + 0.35 * math.log1p(indeg.get(f, 0))) for f, r in ranks.items()}
        showable = [f for f in adj if not self.files[f].is_test and self.files[f].defs]
        evidence = sorted((f for f in showable if f in seeds), key=lambda f: -adj[f])
        others = sorted((f for f in showable if f not in seeds), key=lambda f: -adj[f])
        if others:  # neighbours compete among themselves; weak tails are noise (measured on real code)
            floor = adj[others[0]] * min_share
            others = [f for f in others if adj[f] >= floor][:max_neighbours]
        order = evidence + others
        lines: list[str] = []
        used = 0
        for rel in order:
            block = self._render_file(rel, issue_terms)
            cost = estimate_tokens(block)
            if used + cost > token_budget:
                if used == 0:  # always show something for the top file, trimmed to the budget
                    block = "\n".join(block.splitlines()[: max(3, token_budget // 25)])
                    lines.append(block)
                break
            lines.append(block)
            used += cost
        return "\n".join(lines)

    def _render_file(self, rel: str, issue_terms: set[str], per_file: int = 14) -> str:
        fi = self.files[rel]

        def boring(d: Definition) -> bool:
            return d.name.startswith("__") and d.name not in ("__init__", "__call__") and d.name not in issue_terms

        def key(d: Definition):
            return (d.name not in issue_terms, d.depth > 1, -self.use_files.get(d.name, 0), d.line)

        picked = {id(d): d for d in sorted((d for d in fi.defs if not boring(d)), key=key)[:per_file]}
        for d in list(picked.values()):  # a method is only readable under its class line
            while d.parent >= 0:
                d = fi.defs[d.parent]
                picked[id(d)] = d
        chosen = sorted(picked.values(), key=lambda d: d.line)
        body = "\n".join(f"{d.line:>6}{'  ' * d.depth} {d.signature}" for d in chosen)
        more = f"\n{'':>6} ... {len(fi.defs) - len(chosen)} more definitions" if len(fi.defs) > len(chosen) else ""
        return f"{rel}:\n{body}{more}"


_JS_EXT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
_JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")


def _module_table(files: dict) -> dict[str, str]:
    """Dotted Python module name -> file, including names with a leading src/ or lib/ stripped."""
    table: dict[str, str] = {}
    for rel in files:
        if not rel.endswith((".py", ".pyi")):
            continue
        mod = rel[: -4 if rel.endswith(".pyi") else -3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        for name in (mod, re.sub(r"^(src|lib|python)\.", "", mod)):
            table.setdefault(name, rel)
    return table


def _resolve_import(spec: str, rel: str, modules: dict[str, str], files: dict) -> str | None:
    if spec.startswith("."):  # JS/TS relative specifier or Python relative module
        if "/" in spec:
            base = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
            for cand in (base, *(base + e for e in _JS_EXT), *(base + "/index" + e for e in _JS_EXT)):
                if cand in files:
                    return cand
            return None
        level = len(spec) - len(spec.lstrip("."))
        pkg = rel.rsplit("/", level)[0].replace("/", ".") if rel.count("/") >= level else ""
        spec = (pkg + "." + spec[level:]).strip(".")
    parts = spec.split(".")
    for k in range(len(parts), 0, -1):  # "a.b.c" may be a symbol inside module "a.b"
        hit = modules.get(".".join(parts[:k]))
        if hit:
            return hit
    return None


def _python_index(text: str, rel: str) -> tuple[list[Definition], list[str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return _pattern_defs(text), []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = "." * (node.level or 0) + (node.module or "")
            imports.append(base)
            imports += [(base + "." if not base.endswith(".") else base) + a.name for a in node.names if a.name != "*"]
    return _python_defs_from(tree, text), imports


def _informative(name: str) -> bool:
    """Plain short lowercase words (end, pos, up) are too ambiguous to link files by."""
    core = name.strip("_")
    return "_" in core or any(c.isupper() for c in core) or len(core) >= 7


def _python_defs_from(tree, text: str) -> list[Definition]:
    lines = text.splitlines()
    out: list[Definition] = []

    def visit(node, depth: int, parent: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.Try, ast.With, ast.ExceptHandler)) and depth == 0:
                visit(child, depth, parent)  # module-level defs guarded by `if`/`try` (e.g. ImportError fallbacks)
                continue
            if type(child).__name__ == "TryStar" and depth == 0:
                visit(child, depth, parent)
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sig = lines[child.lineno - 1].strip() if child.lineno - 1 < len(lines) else child.name
                out.append(Definition(child.name, child.lineno, clip_line(sig.rstrip(":"), 110), depth, parent))
                if isinstance(child, ast.ClassDef) and depth < 2:
                    visit(child, depth + 1, len(out) - 1)
            elif depth == 0 and isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
                name = child.targets[0].id
                if name.isupper() and len(name) > 2:
                    out.append(Definition(name, child.lineno, name, 0))

    visit(tree, 0, -1)
    return out


def _pattern_defs(text: str) -> list[Definition]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _DECL.match(line) or _ARROW.match(line)
        if m:
            indent = len(line) - len(line.lstrip())
            out.append(Definition(m.group(1), i, clip_line(line.strip().rstrip("{").strip(), 110), min(2, indent // 4)))
    return out
