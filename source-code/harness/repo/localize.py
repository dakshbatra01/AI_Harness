"""LLM-free candidate localization from issue anchors (hierarchical, evidence-labelled priors).

Scores are a *prior* for ranking and orientation, never proof. Every reason is recorded so
the model (and the trace) can see why a file was suggested."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from harness.repo.anchors import Anchors
from harness.repo.manifest import DOC_EXT, LANG_BY_EXT, RepoManifest, is_test_path


@dataclass
class Candidate:
    path: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)

    def add(self, pts: float, reason: str, line: int | None = None) -> None:
        self.score += pts
        if reason not in self.reasons:
            self.reasons.append(reason)
        if line and line not in self.lines:
            self.lines.append(line)


@dataclass
class Localization:
    sources: list[Candidate]
    tests: list[Candidate]
    confidence: str  # high | medium | low

    def prior(self) -> dict[str, float]:
        return {c.path: c.score for c in self.sources + self.tests}

    def render(self, limit: int = 10) -> str:
        if not self.sources and not self.tests:
            return "(no candidates from anchors; start with search_code on key behaviour words)"
        out = []
        for c in self.sources[:limit]:
            ln = f" lines~{','.join(map(str, sorted(c.lines)[:5]))}" if c.lines else ""
            out.append(f"- {c.path}{ln}  [score {c.score:.0f}: {'; '.join(c.reasons[:4])}]")
        if self.tests:
            out.append("Possibly related tests:")
            for c in self.tests[:6]:
                out.append(f"- {c.path}  [{'; '.join(c.reasons[:3])}]")
        return "\n".join(out)


def _match_file(files_by_suffix: dict[str, list[str]], files: set[str], p: str) -> list[str]:
    p = p.replace("\\", "/").lstrip("./")
    if p in files:
        return [p]
    parts = p.split("/")
    # Longest suffix match first (handles absolute paths from stack traces / site-packages).
    for k in range(min(len(parts), 6), 0, -1):
        suf = "/".join(parts[-k:])
        hits = files_by_suffix.get(suf)
        if hits:
            return hits if len(hits) <= 3 else []
    return []


def localize(anchors: Anchors, manifest: RepoManifest, searcher, max_symbol_queries: int = 14) -> Localization:
    files = set(manifest.files)
    by_suffix: dict[str, list[str]] = {}
    for f in manifest.files:
        parts = f.split("/")
        for k in range(1, min(len(parts), 6) + 1):
            by_suffix.setdefault("/".join(parts[-k:]), []).append(f)
    cands: dict[str, Candidate] = {}

    def cand(path: str) -> Candidate:
        if path not in cands:
            cands[path] = Candidate(path)
        return cands[path]

    for p, ln in anchors.paths:
        for f in _match_file(by_suffix, files, p):
            cand(f).add(10, "named in issue", ln)
    nframes = len(anchors.frames)
    for i, fr in enumerate(anchors.frames):
        for f in _match_file(by_suffix, files, fr.path):
            deep = i == nframes - 1
            cand(f).add(9 + (2 if deep else 0), "in stack trace" + (" (innermost frame)" if deep else ""), fr.line)

    # Symbol definitions: one combined query per batch keeps this to a few searches.
    syms = [s.split(".")[-1] for s in anchors.symbols if re.fullmatch(r"[A-Za-z_][\w.]*", s)]
    syms = list(dict.fromkeys(s for s in syms if len(s) >= 3))[:max_symbol_queries]
    if syms:
        alt = "|".join(re.escape(s) for s in syms)
        pattern = (
            rf"^\s*(async\s+)?(def|class|function|func|fn|struct|interface|enum|trait|type)\s+({alt})\b"
            rf"|^\s*(export\s+)?(const|let|var)\s+({alt})\s*="
        )
        defs = searcher.count_files(pattern, regex=True)
        for f, n in defs.items():
            if f in files or os.path.exists(os.path.join(str(manifest.root), f)):
                cand(f).add(min(3, n) * 5, "defines an identifier from the issue")

    for s in anchors.strings[:6]:
        if len(s) < 6 or s.count(" ") > 12:
            continue
        hits = searcher.count_files(s, regex=False)
        if 0 < len(hits) <= 8:
            for f in hits:
                cand(f).add(7, f"contains issue text {s[:40]!r}")

    for s in [x for x in anchors.symbols if len(x) >= 4][:8]:
        hits = searcher.count_files(s.split(".")[-1], regex=False)
        if 0 < len(hits) <= 25:
            for f, n in hits.items():
                cand(f).add(min(2.0, 0.5 * n), f"mentions {s.split('.')[-1]}")

    for t in anchors.tests[:6]:
        hits = searcher.count_files(t, regex=False)
        for f in list(hits)[:4]:
            cand(f).add(6, f"contains test {t}")

    sources, tests = [], []
    for c in cands.values():
        ext = os.path.splitext(c.path)[1].lower()
        if ext in DOC_EXT:
            c.score *= 0.3
        elif ext not in LANG_BY_EXT and ext not in (".toml", ".cfg", ".ini", ".json", ".yml", ".yaml"):
            c.score *= 0.5
        (tests if is_test_path(c.path) else sources).append(c)
    sources.sort(key=lambda c: -c.score)
    tests.sort(key=lambda c: -c.score)
    conf = "low"
    if sources:
        top = sources[0].score
        second = sources[1].score if len(sources) > 1 else 0
        if top >= 10 and top >= 1.5 * second:
            conf = "high"
        elif top >= 6:
            conf = "medium"
    return Localization(sources=sources[:20], tests=tests[:10], confidence=conf)
