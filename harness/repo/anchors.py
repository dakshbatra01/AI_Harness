"""Deterministic extraction of issue anchors: paths, stack frames, symbols, errors, tests, strings."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_PATH_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|js|jsx|mjs|ts|tsx|go|rs|java|kt|rb|php|c|cc|cpp|h|hpp|cs|scala|swift|"
    r"sh|toml|cfg|ini|ya?ml|json|txt|rst|md|html|css|sql))(?::(\d+))?\b"
)
_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+), in ([\w<>.]+)')
_JS_FRAME = re.compile(r"at (?:[\w.$<>\[\] ]+ )?\(?((?:/|\.{1,2}/|[\w-]+/)[^():\s]+\.[jt]sx?):(\d+):\d+\)?")
_GO_FRAME = re.compile(r"^\s+(\S+\.go):(\d+)", re.M)
_JAVA_FRAME = re.compile(r"at ([\w.$]+)\.([\w$<>]+)\((\w+\.(?:java|kt|scala)):(\d+)\)")
_EXC_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Warning|Failure|Fault|Panic))\b")
_TEST_RE = re.compile(r"\b(test_[A-Za-z0-9_]+|Test[A-Z][A-Za-z0-9_]*)\b")
_BACKTICK_RE = re.compile(r"`([^`\n]{2,120})`")
_QUOTE_RE = re.compile(r"(?<![\w])[\"']([^\"'\n]{6,160})[\"'](?![\w])")
_CODEBLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_DOTTED_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){1,5})\b")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\(")
_CAMEL_RE = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+|[a-z]+[A-Z][A-Za-z0-9]*)\b")
_SNAKE_RE = re.compile(r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b")

STOP = {
    "e.g", "i.e", "etc", "http", "https", "www", "github.com", "self", "None", "True", "False", "print", "return",
    "import", "from", "class", "def", "function", "const", "this", "true", "false", "null", "len", "str", "int", "dict",
    "list", "type", "object", "value", "values", "result", "results", "example", "issue", "error", "test", "tests",
    "python", "version", "expected", "actual", "output", "input", "the", "and", "for", "with", "not", "when", "should",
}


@dataclass
class Frame:
    path: str
    line: int
    func: str = ""


@dataclass
class Anchors:
    paths: list[tuple[str, int | None]] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    strings: list[str] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def brief(self) -> str:
        parts = []
        if self.paths:
            parts.append("paths: " + ", ".join(p + (f":{ln}" if ln else "") for p, ln in self.paths[:10]))
        if self.frames:
            parts.append("stack frames: " + ", ".join(f"{f.path}:{f.line} ({f.func})" for f in self.frames[-8:]))
        if self.exceptions:
            parts.append("exceptions: " + ", ".join(self.exceptions[:6]))
        if self.tests:
            parts.append("tests: " + ", ".join(self.tests[:8]))
        if self.symbols:
            parts.append("identifiers: " + ", ".join(self.symbols[:16]))
        if self.strings:
            parts.append("quoted strings: " + " | ".join(repr(s) for s in self.strings[:6]))
        return "\n".join(parts) or "(no explicit anchors found; localize from the behaviour described)"


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def extract_anchors(issue: str) -> Anchors:
    a = Anchors()
    for m in _PY_FRAME.finditer(issue):
        a.frames.append(Frame(m.group(1), int(m.group(2)), m.group(3)))
    for m in _JS_FRAME.finditer(issue):
        a.frames.append(Frame(m.group(1), int(m.group(2))))
    for m in _GO_FRAME.finditer(issue):
        a.frames.append(Frame(m.group(1), int(m.group(2))))
    for m in _JAVA_FRAME.finditer(issue):
        a.frames.append(Frame(m.group(3), int(m.group(4)), m.group(2)))
    paths = []
    for m in _PATH_RE.finditer(issue):
        p = m.group(1)
        if p.lower().startswith(("http", "www.")) or p.count(".") > 4:
            continue
        paths.append((p, int(m.group(2)) if m.group(2) else None))
    a.paths = _uniq(paths)
    a.exceptions = _uniq(_EXC_RE.findall(issue))
    a.tests = _uniq(_TEST_RE.findall(issue))
    a.code_blocks = [b.strip() for b in _CODEBLOCK_RE.findall(issue) if b.strip()][:6]

    strings = [s.strip() for s in _BACKTICK_RE.findall(issue)]
    strings += [s.strip() for s in _QUOTE_RE.findall(issue)]
    # error-message-like lines from tracebacks: "SomeError: message"
    for m in re.finditer(r"\b\w+(?:Error|Exception): ([^\n]{6,160})", issue):
        strings.append(m.group(1).strip())
    a.strings = _uniq(s for s in strings if 3 <= len(s) <= 160)[:20]

    syms: list[str] = []
    for f in a.frames:
        if f.func and f.func not in ("<module>", "<lambda>"):
            syms.append(f.func)
    text_for_syms = issue
    for s in _BACKTICK_RE.findall(issue):
        m = re.match(r"^[A-Za-z_][\w.]*", s.strip())
        if m:
            syms.append(m.group(0))
    for rx in (_DOTTED_RE, _CALL_RE, _CAMEL_RE, _SNAKE_RE):
        syms += rx.findall(text_for_syms)
    cleaned = []
    for s in syms:
        s = s.strip(".")
        if not s or s in STOP or s.lower() in STOP or len(s) < 3:
            continue
        if re.fullmatch(r"[\w.-]+\.(py|js|ts|go|rs|java|rb|md|txt|html|json|toml|yml|yaml|cfg)", s):
            continue
        if s.startswith(("http", "www")):
            continue
        cleaned.append(s)
    # Prefer the most specific: dotted names contribute their last components too.
    expanded = []
    for s in cleaned:
        expanded.append(s)
        if "." in s:
            expanded += [p for p in s.split(".")[-2:] if len(p) >= 3 and p not in STOP]
    counts: dict[str, int] = {}
    for s in expanded:
        counts[s] = counts.get(s, 0) + 1
    ordered = _uniq(expanded)
    ordered.sort(key=lambda s: (-(("_" in s) or any(c.isupper() for c in s[1:]) or "." in s), -counts[s]))
    a.symbols = ordered[:30]
    return a
