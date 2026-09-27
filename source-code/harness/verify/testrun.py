"""Test framework detection, command construction and output parsing into test identities.

Outcomes are normalized to PASS | FAIL | ERROR | SKIP. XFAIL/XPASS count as PASS-equivalent
(SWE-bench grading semantics). When no parser applies, the whole command becomes one coarse
test identity whose outcome is its exit status (weaker evidence, flagged as such)."""
from __future__ import annotations

import configparser
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PASS, FAIL, ERROR, SKIP = "PASS", "FAIL", "ERROR", "SKIP"
PASSLIKE = {PASS}
FAILLIKE = {FAIL, ERROR}


@dataclass
class TestFramework:
    name: str  # pytest | unittest | django | go | cargo | jest | vitest | mocha | npm | maven | gradle | rspec | make | none
    base_cmd: str
    note: str = ""


@dataclass
class TestRun:
    command: str
    exit_code: int | None
    outcomes: dict[str, str] = field(default_factory=dict)
    parser: str = "none"
    timed_out: bool = False
    duration_s: float = 0.0
    log_id: str = ""
    output_tail: str = ""
    failure_excerpt: str = ""
    coarse: bool = False
    collection_error: bool = False
    launch_failed: bool = False

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for v in self.outcomes.values():
            c[v] = c.get(v, 0) + 1
        return c

    def failing(self) -> list[str]:
        return sorted(k for k, v in self.outcomes.items() if v in FAILLIKE)

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "parser": self.parser,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration_s, 2),
            "log_id": self.log_id,
            "counts": self.counts(),
            "coarse": self.coarse,
            "collection_error": self.collection_error,
            "outcomes": self.outcomes if len(self.outcomes) <= 400 else {k: v for k, v in list(self.outcomes.items())[:400]},
        }


# ---- detection -------------------------------------------------------------------------------
def _python() -> str:
    for cand in ("python", "python3"):
        if shutil.which(cand):
            return cand
    return sys.executable


def _has_module(py: str, mod: str, cwd: Path) -> bool:
    try:
        return subprocess.run([py, "-c", f"import {mod}"], cwd=cwd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def detect_framework(root: Path, files: list[str]) -> TestFramework:
    fs = set(files)
    py = _python()
    has_py = any(f.endswith(".py") for f in files)
    if "tests/runtests.py" in fs and "django/__init__.py" in fs:
        return TestFramework("django", f"{py} tests/runtests.py --verbosity 2 --parallel 1", "Django's own test runner; pass test labels like `module.tests`")
    if "bin/test" in fs and "sympy/__init__.py" in fs:
        return TestFramework("sympy", f"{py} bin/test -C --no-colors", "sympy runner; pass file paths")
    if has_py:
        pytest_markers = ("pytest.ini", "conftest.py", "tox.ini", "setup.cfg", "pyproject.toml")
        wants_pytest = False
        for mk in pytest_markers:
            p = root / mk
            if not p.is_file():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if mk in ("pytest.ini", "conftest.py") or "pytest" in txt:
                wants_pytest = True
                break
        if any(f.endswith("conftest.py") for f in files):
            wants_pytest = True
        if _has_module(py, "pytest", root):
            return TestFramework("pytest", f"{py} -m pytest", "pytest" + ("" if wants_pytest else " (also runs unittest tests)"))
        return TestFramework("unittest", f"{py} -m unittest", "pytest not installed; stdlib unittest")
    if "go.mod" in fs:
        return TestFramework("go", "go test", "go test; pass package paths like ./pkg/...")
    if "Cargo.toml" in fs:
        return TestFramework("cargo", "cargo test", "cargo test; pass a test-name filter")
    if "package.json" in fs:
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        script = (pkg.get("scripts") or {}).get("test", "")
        if "vitest" in deps or "vitest" in script:
            return TestFramework("vitest", "npx vitest run", "vitest")
        if "jest" in deps or "jest" in script:
            return TestFramework("jest", "npx jest", "jest")
        if "mocha" in deps or "mocha" in script:
            return TestFramework("mocha", "npx mocha", "mocha")
        if script:
            return TestFramework("npm", "npm test --", f"npm test ({script[:60]})")
    if "pom.xml" in fs:
        return TestFramework("maven", "mvn -q -B test", "maven surefire")
    if "build.gradle" in fs or "build.gradle.kts" in fs:
        gw = "./gradlew" if "gradlew" in fs else "gradle"
        return TestFramework("gradle", f"{gw} test", "gradle")
    if "Gemfile" in fs and any(f.startswith("spec/") for f in files):
        return TestFramework("rspec", "bundle exec rspec", "rspec")
    mk = root / "Makefile"
    if mk.is_file():
        try:
            if re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="replace"), re.M):
                return TestFramework("make", "make test", "Makefile test target")
        except OSError:
            pass
    return TestFramework("none", "", "no test framework detected")


def build_command(fw: TestFramework, targets: list[str]) -> str:
    """Command running specific test targets (files / node ids / packages) with parseable output."""
    t = " ".join(shlex.quote(x) for x in targets)
    if fw.name == "pytest":
        return f"{fw.base_cmd} -rA --tb=short -p no:cacheprovider {t}".strip()
    if fw.name == "unittest":
        mods = [_py_path_to_module(x) for x in targets] if targets else []
        return f"{fw.base_cmd} -v {' '.join(shlex.quote(m) for m in mods)}".strip()
    if fw.name == "django":
        labels = [_django_label(x) for x in targets]
        return f"{fw.base_cmd} {' '.join(shlex.quote(l) for l in labels)}".strip()
    if fw.name == "go":
        return f"go test -v {t or './...'}"
    if fw.name == "cargo":
        return f"cargo test {t}".strip()
    if fw.name in ("jest", "vitest", "mocha"):
        return f"{fw.base_cmd} {t}".strip()
    return f"{fw.base_cmd} {t}".strip()


def normalize_command(fw: TestFramework, command: str) -> str:
    """Add flags that make output parseable without changing which tests run."""
    c = command.strip()
    if re.search(r"(^|\s|/)(py\.test|pytest)(\s|$)|-m\s+pytest", c):
        if " -rA" not in c and " -ra" not in c.lower():
            c = re.sub(r"(-m\s+pytest|(?<![\w-])py\.?test\b)", r"\1 -rA", c, count=1)
        if "--tb" not in c:
            c = re.sub(r"(-m\s+pytest|(?<![\w-])py\.?test\b)", r"\1 --tb=short", c, count=1)
        if "cacheprovider" not in c:
            c = re.sub(r"(-m\s+pytest|(?<![\w-])py\.?test\b)", r"\1 -p no:cacheprovider", c, count=1)
    elif re.search(r"-m\s+unittest", c) and " -v" not in c:
        c = re.sub(r"(-m\s+unittest)", r"\1 -v", c, count=1)
    elif re.search(r"(^|\s)go\s+test(\s|$)", c) and " -v" not in c:
        c = re.sub(r"go\s+test", "go test -v", c, count=1)
    elif "runtests.py" in c and "--verbosity" not in c and "-v" not in c.split():
        c += " --verbosity 2"
    return c


def _py_path_to_module(x: str) -> str:
    """tests/test_x.py -> tests.test_x ; tests/test_x.py::Cls::test -> tests.test_x.Cls.test"""
    return canonical_test_id(x)


# ---- supplied test cases -------------------------------------------------------------------------
_RUNNER_RE = re.compile(
    r"^\s*(?:[A-Z_][A-Z0-9_]*=\S*\s+)*(?:python[\d.]*|py\.test|pytest|tox|nox|npm|npx|yarn|pnpm|node|go|cargo|make|mvn|"
    r"gradle|\./\S+|bash|sh|bundle|rspec|ruby|php|java|dotnet|deno|bun)(?:\s|$)"
)
_UNITTEST_ID = re.compile(r"^(\w+) \(([\w.]+)\)$")


def split_test_list(raw) -> list[str]:
    """Split a user-supplied list on newlines/semicolons, and on commas outside [] or () - so parametrized
    ids like test_x[1,2] stay whole."""
    chunks = raw if isinstance(raw, (list, tuple)) else [raw or ""]
    out: list[str] = []
    for chunk in chunks:
        for part in re.split(r"[\n;]+", str(chunk)):
            depth, cur = 0, ""
            for ch in part:
                depth += ch in "[(" 
                depth -= ch in "])"
                if ch == "," and depth <= 0:
                    out.append(cur)
                    cur = ""
                else:
                    cur += ch
            out.append(cur)
    return [normalize_test_id(x) for x in out if x.strip()]


def normalize_test_id(t: str) -> str:
    """`test_x (pkg.mod.Class)` (unittest/Django display form) -> `pkg.mod.Class.test_x`."""
    t = t.strip()
    m = _UNITTEST_ID.match(t)
    return f"{m.group(2)}.{m.group(1)}" if m else t


def is_test_command(t: str) -> bool:
    """A supplied entry is a shell command only when it starts with a known runner or an executable path."""
    return bool(_RUNNER_RE.match(t))


def canonical_test_id(t: str) -> str:
    """Framework-neutral id: `a/b.py::C::t` and `a.b.C.t` both become `a.b.C.t`."""
    t = t.strip()
    if "::" in t:
        path, rest = t.split("::", 1)
        base = path[:-3] if path.endswith(".py") else path
        return base.replace("/", ".") + "." + rest.replace("::", ".")
    if t.endswith(".py"):
        return t[:-3].replace("/", ".")
    return t


def test_id_matches(target: str, key: str) -> bool:
    """Does reported test `key` belong to the requested `target`? Match whole tests and
    file/class prefixes on component boundaries, never arbitrary substrings."""
    ct, ck = canonical_test_id(target), canonical_test_id(key)
    if "[" not in ct:
        ck = re.sub(r"\[.*\]$", "", ck)
    return ck == ct or ck.endswith("." + ct) or ck.startswith(ct + ".")


def _django_label(x: str) -> str:
    if x.endswith(".py"):
        x = x[:-3]
    if x.startswith("tests/"):
        x = x[len("tests/") :]
    return x.replace("/", ".").replace("::", ".")


# ---- parsing ---------------------------------------------------------------------------------
_PYTEST_VERBOSE = re.compile(r"^(\S+?::\S.*?)\s+(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)(?:\s+\[\s*\d+%\])?\s*$")
_PYTEST_COLLECT_ERR = re.compile(r"^ERROR\s+(\S+\.py)(?:\s+-\s+.*)?$")
_GO = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
_CARGO = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored)", re.M)
_JEST_FILE = re.compile(r"^\s*(PASS|FAIL)\s+(\S+\.[jt]sx?)")
_JEST_TEST = re.compile(r"^\s+(✓|✕|√|×|○)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$")
_MAVEN = re.compile(r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+).*? - in (\S+)")

_MAP = {
    "PASSED": PASS, "FAILED": FAIL, "ERROR": ERROR, "XFAIL": PASS, "XPASS": PASS, "SKIPPED": SKIP,
    "ok": PASS, "FAIL": FAIL, "expected failure": PASS, "unexpected success": FAIL,
    "PASS": PASS, "SKIP": SKIP, "FAILED": FAIL, "ignored": SKIP,
}


def parse_output(command: str, stdout: str, stderr: str, exit_code: int | None) -> tuple[dict[str, str], str, bool]:
    """Returns (outcomes, parser_name, collection_error)."""
    text = stdout + "\n" + stderr
    outcomes: dict[str, str] = {}
    collection_error = False
    # pytest
    if "pytest" in command or "short test summary info" in text or re.search(r"=+ .*(passed|failed|error).* in [\d.]+s", text):
        for line in text.splitlines():
            line = line.rstrip()
            m = _PYTEST_VERBOSE.match(line)
            if m:
                outcomes[m.group(1)] = _MAP[m.group(2)]
                continue
            m = re.match(r"^(PASSED|FAILED|ERROR|XFAIL|XPASS|SKIPPED)\s+(\S+?::\S.*)$", line)
            if m:
                outcomes[_pytest_summary_id(m.group(2))] = _MAP[m.group(1)]
                continue
            m = _PYTEST_COLLECT_ERR.match(line)
            if m:
                outcomes[m.group(1)] = ERROR
                collection_error = True
        if outcomes or "no tests ran" in text or "collected 0 items" in text:
            if "errors during collection" in text or "ERROR collecting" in text:
                collection_error = True
            return outcomes, "pytest", collection_error
    # unittest / django (verbose)
    if " ... " in text:
        pending = None
        for line in text.splitlines():
            m = re.match(r"^(\w+) \(([\w.]+)\)(.*)$", line)
            if m:
                name, cls, rest = m.groups()
                tid = cls if cls.endswith("." + name) else f"{cls}.{name}"
                pending = tid
                r = re.search(r"\.\.\.\s*(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$", rest)
                if r:
                    outcomes[tid] = _MAP.get(r.group(1), SKIP if r.group(1).startswith("skipped") else FAIL)
                    pending = None
                continue
            if pending:
                r = re.search(r"\.\.\.\s*(ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$", line)
                if r:
                    outcomes[pending] = _MAP.get(r.group(1), SKIP if r.group(1).startswith("skipped") else FAIL)
                    pending = None
        for m in re.finditer(r"^(ERROR|FAIL): (\w+) \(([\w.]+)\)", text, re.M):
            kind, name, cls = m.groups()
            tid = cls if cls.endswith("." + name) else f"{cls}.{name}"
            outcomes[tid] = ERROR if kind == "ERROR" else FAIL
            if name in ("setUpClass", "setUpModule") or "ImportError" in text[m.end() : m.end() + 2000]:
                collection_error = collection_error or name.startswith("setUp")
        if re.search(r"^ERROR: \S+ \(unittest\.loader\._FailedTest", text, re.M):
            collection_error = True
        if outcomes:
            return outcomes, "unittest", collection_error
    # go
    if "go test" in command or re.search(r"^(ok|FAIL)\s+\S+", text, re.M):
        for line in text.splitlines():
            m = _GO.match(line)
            if m:
                outcomes[m.group(2)] = _MAP[m.group(1)]
        for m in re.finditer(r"^FAIL\s+(\S+)\s+\[(build failed|setup failed)\]", text, re.M):
            outcomes[f"[build] {m.group(1)}"] = ERROR
            collection_error = True
        if outcomes:
            return outcomes, "go", collection_error
    # cargo
    ms = _CARGO.findall(text)
    if ms:
        return {name: _MAP[st] for name, st in ms}, "cargo", False
    # jest / vitest
    cur = None
    for line in text.splitlines():
        m = _JEST_FILE.match(line)
        if m:
            cur = m.group(2)
            continue
        m = _JEST_TEST.match(line)
        if m and cur:
            sym = m.group(1)
            outcomes[f"{cur} > {m.group(2).strip()}"] = PASS if sym in "✓√" else (SKIP if sym == "○" else FAIL)
    if outcomes:
        return outcomes, "jest", False
    # maven
    for m in _MAVEN.finditer(text):
        run, fails, errs, _skip, cls = m.groups()
        outcomes[cls] = FAIL if int(fails) + int(errs) > 0 else PASS
    if outcomes:
        return outcomes, "maven", False
    return {}, "none", False


def _pytest_summary_id(rest: str) -> str:
    """`path::test[a - b] - message` -> `path::test[a - b]` (' - ' may occur inside parameters)."""
    rest = rest.strip()
    i = rest.find("[")
    if i >= 0 and (" - " not in rest or i < rest.find(" - ")):
        depth = 0
        for j in range(i, len(rest)):
            depth += (rest[j] == "[") - (rest[j] == "]")
            if depth == 0 and (j + 1 == len(rest) or rest[j + 1 :].startswith(" - ")):
                return rest[: j + 1]
    return rest.split(" - ", 1)[0].strip()


def failure_excerpt(stdout: str, stderr: str, limit: int = 3500) -> str:
    """The most useful failure region: pytest FAILURES section, unittest tracebacks, or the tail."""
    text = stdout + ("\n" + stderr if stderr.strip() else "")
    for marker in ("= FAILURES =", "= ERRORS =", "=== FAILURES ===", "==================== ERRORS"):
        i = text.find(marker)
        if i >= 0:
            j = text.find("short test summary info", i)
            seg = text[i : j if j > 0 else len(text)]
            return seg[:limit] + ("\n[...]" if len(seg) > limit else "")
    i = text.find("\n======================================================================\n")
    if i >= 0 and ("FAIL:" in text[i : i + 400] or "ERROR:" in text[i : i + 400]):
        seg = text[i:]
        return seg[:limit] + ("\n[...]" if len(seg) > limit else "")
    for marker in ("--- FAIL:", "panic:", "thread '", "● "):
        i = text.find(marker)
        if i >= 0:
            return text[i : i + limit]
    return text[-limit:]


def classify_transitions(base: dict[str, str], cand: dict[str, str]) -> dict[str, list[str]]:
    """Per-test base->candidate transitions (identity-based, never count-based)."""
    res: dict[str, list[str]] = {
        "fixed": [], "regressed": [], "still_failing": [], "still_passing": [], "new_pass": [], "new_fail": [],
        "to_skip": [], "disappeared": [], "other": [],
    }
    for tid in sorted(set(base) | set(cand)):
        b, c = base.get(tid), cand.get(tid)
        if b in FAILLIKE and c == PASS:
            res["fixed"].append(tid)
        elif b == PASS and c in FAILLIKE:
            res["regressed"].append(tid)
        elif b in FAILLIKE and c in FAILLIKE:
            res["still_failing"].append(tid)
        elif b == PASS and c == PASS:
            res["still_passing"].append(tid)
        elif b is None and c == PASS:
            res["new_pass"].append(tid)
        elif b is None and c in FAILLIKE:
            res["new_fail"].append(tid)
        elif c == SKIP and b != SKIP:
            res["to_skip"].append(tid)
        elif c is None and b is not None:
            res["disappeared"].append(tid)
        else:
            res["other"].append(tid)
    return res


def related_test_files(changed: list[str], manifest_files: list[str], test_files: list[str], limit: int = 6) -> list[str]:
    """Heuristic source->test association: name conventions, then import/reference mentions."""
    out: list[str] = []
    tset = set(test_files)
    for src in changed:
        if src in tset:
            if src not in out:
                out.append(src)
            continue
        stem = Path(src).stem
        if stem in ("__init__", "index", "main", "mod", "lib"):
            stem = Path(src).parent.name
        pats = [f"test_{stem}.py", f"{stem}_test.py", f"tests_{stem}.py", f"{stem}_test.go", f"{stem}.test.", f"{stem}.spec.",
                f"{stem}Test.", f"{stem}_spec.rb", f"test_{stem}s.py"]
        for t in test_files:
            base = t.rsplit("/", 1)[-1]
            if any(base.startswith(p) or base == p for p in pats) and t not in out:
                out.append(t)
        # tests living in a directory named after the module (e.g. tests/<stem>/)
        for t in test_files:
            if f"/{stem}/" in f"/{t}" and t not in out and len(out) < limit * 2:
                out.append(t)
    return out[:limit]


def env_fingerprint() -> str:
    import hashlib
    import platform

    blob = f"{sys.version}|{platform.platform()}|{os.environ.get('PATH', '')}|{os.environ.get('VIRTUAL_ENV', '')}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def read_setup_cfg_testpaths(root: Path) -> list[str]:
    cfg = configparser.ConfigParser()
    try:
        cfg.read(root / "setup.cfg")
        return cfg.get("tool:pytest", "testpaths", fallback="").split()
    except (configparser.Error, OSError):
        return []
