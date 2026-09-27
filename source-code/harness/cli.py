"""Command-line entry point (`make run` -> `python -m harness run`).

Issue input (first match wins): --issue TEXT | --issue-file PATH | --issue-url URL | env ISSUE / ISSUE_FILE /
ISSUE_URL | piped stdin | interactive prompt (TTY). Repository: --repo PATH_OR_GIT_URL | env REPO |
derived from a GitHub issue URL (cloned) | interactive prompt | current directory.
Headless whenever stdin is not a TTY."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from harness import __version__
from harness.config import HARNESS_ROOT, load_config
from harness.util import REDACT

GH_ISSUE_RE = re.compile(r"https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)(?:[/?#]\S*)?")
GIT_URL_RE = re.compile(r"^(https?://|git@|ssh://).+|.+\.git$")
GH_SLUG_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
FETCH_ERRORS = (ValueError, OSError, urllib.error.URLError, subprocess.CalledProcessError)


def _print(msg: str = "") -> None:
    print(REDACT(msg), flush=True)


def _is_issue_url(text: str) -> bool:
    return bool(GH_ISSUE_RE.fullmatch(text.strip()))


def _fetch_github_issue(url: str) -> tuple[str, str]:
    """GitHub issue/PR URL -> (title + body, clone URL). Raises ValueError with an actionable message."""
    m = GH_ISSUE_RE.search(url)
    if not m:
        raise ValueError(f"not a GitHub issue URL: {url}")
    owner, repo, num = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
    req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "harness"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = " (GitHub rate limit: export GITHUB_TOKEN, or paste the issue text instead)" if e.code in (403, 429) else ""
        raise ValueError(f"could not fetch {url}: HTTP {e.code}{hint}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"could not fetch {url}: {e} (no network? paste the issue text instead)") from None
    text = f"{(data.get('title') or '').strip()}\n\n{(data.get('body') or '').strip()}\n"
    return text, f"https://github.com/{owner}/{repo}.git"


def _clone(url: str, commit: str | None) -> Path:
    """Give each task a clean clone without discarding a previous task's patch."""
    name = re.sub(r"[^\w.-]+", "_", url.rstrip("/").split("github.com/")[-1].removesuffix(".git"))[-80:]
    dest = HARNESS_ROOT / "workspaces" / f"{name}-{uuid.uuid4().hex[:10]}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _print(f"cloning {url} -> {dest}")
    subprocess.run(["git", "clone", "--quiet", url, str(dest)], check=True)
    if commit:
        known = subprocess.run(["git", "-C", str(dest), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True)
        if known.returncode != 0:
            subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin", commit], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "--force", commit], check=True)
    return dest


@dataclass
class Task:
    root: Path
    issue: str
    tests: list = field(default_factory=list)  # test ids / commands that must pass ("test case")


def _split_tests(raw) -> list[str]:
    from harness.verify.testrun import split_test_list

    return split_test_list(raw) if raw else []


def _resolve_repo(repo: str, derived: str, commit: str | None) -> Path:
    repo = (repo or derived or "").strip()
    if repo and GH_SLUG_RE.match(repo) and not Path(repo).expanduser().exists():
        repo = f"https://github.com/{repo}.git"  # "owner/name" shorthand
    if repo and GIT_URL_RE.match(repo) and not Path(repo).expanduser().exists():
        root = _clone(repo, commit)
    elif repo:
        root = Path(repo).expanduser().resolve()
    else:
        cwd = Path.cwd().resolve()
        if cwd == HARNESS_ROOT:
            raise ValueError("no repository given (pass a path/git URL, REPO=..., or a GitHub issue URL)")
        root = cwd
    if not root.is_dir():
        raise ValueError(f"repository path does not exist: {root}")
    if root == HARNESS_ROOT:
        raise ValueError("refusing to run the harness on its own source tree; give the target repository")
    return root


def _issue_from_text(raw: str) -> tuple[str, str]:
    """Accepts issue text, a GitHub issue URL, or a path to an issue file. Returns (issue, derived_repo)."""
    raw = raw.strip()
    if _is_issue_url(raw):
        return _fetch_github_issue(raw)
    if raw and "\n" not in raw and len(raw) < 1024 and Path(raw).expanduser().is_file():
        return Path(raw).expanduser().read_text(encoding="utf-8", errors="replace"), ""
    return raw, ""


def _from_json(spec: dict) -> dict:
    """Task JSON, including SWE-bench-shaped records (problem_statement / FAIL_TO_PASS / base_commit)."""
    def listish(v):
        if isinstance(v, str) and v.strip().startswith("["):
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v

    return {
        "repo": spec.get("repo") or spec.get("repository") or "",
        "commit": spec.get("commit") or spec.get("base_commit"),
        "tests": _split_tests(listish(spec.get("tests") or spec.get("FAIL_TO_PASS") or spec.get("fail_to_pass"))),
        "issue_url": spec.get("issue_url") or "",
        "issue": spec.get("issue") or spec.get("problem_statement") or spec.get("text") or "",
    }


def _from_piped_lines(text: str) -> dict:
    """Plain piped input. If it starts with a GitHub issue URL, following lines may give the repository and
    test cases (the same answers the interactive prompts ask for); otherwise it is the issue text."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if lines and _is_issue_url(lines[0]):
        out = {"issue_url": lines[0], "repo": "", "tests": []}
        rest = lines[1:]
        if rest and (GIT_URL_RE.match(rest[0]) or Path(rest[0]).expanduser().is_dir() or GH_SLUG_RE.match(rest[0])):
            out["repo"] = rest.pop(0)
        out["tests"] = _split_tests(rest)
        return out
    return {"issue": text}


def task_from_sources(args) -> Task | None:
    """Non-interactive inputs: CLI flags, env vars (REPO, ISSUE, ISSUE_FILE, ISSUE_URL, TESTS, COMMIT), piped
    stdin (issue text, a GitHub issue URL [+ repo + tests lines], or task JSON)."""
    issue = args.issue or os.environ.get("ISSUE") or ""
    issue_file = args.issue_file or os.environ.get("ISSUE_FILE") or ""
    issue_url = args.issue_url or os.environ.get("ISSUE_URL") or ""
    repo = args.repo or os.environ.get("REPO") or os.environ.get("HARNESS_REPO") or ""
    tests = _split_tests(args.test) + _split_tests(os.environ.get("TESTS"))
    commit = args.commit or os.environ.get("COMMIT") or None
    derived = ""
    if not issue and issue_file:
        issue = Path(issue_file).expanduser().read_text(encoding="utf-8", errors="replace")
    if not issue and not issue_url and not sys.stdin.isatty():
        piped = sys.stdin.read()
        spec: dict = {}
        if piped.strip().startswith("{"):
            try:
                loaded = json.loads(piped)
                spec = _from_json(loaded) if isinstance(loaded, dict) else {"issue": piped}
            except ValueError:
                spec = {"issue": piped}
        elif piped.strip():
            spec = _from_piped_lines(piped)
        repo = repo or spec.get("repo", "")
        commit = commit or spec.get("commit")
        tests += spec.get("tests", [])
        issue_url = spec.get("issue_url", "")
        issue = spec.get("issue", "") or ""
    if not issue and issue_url:
        issue, derived = _fetch_github_issue(issue_url)
    if issue.strip():
        issue, d2 = _issue_from_text(issue)
        derived = derived or d2
    if not issue.strip() and not tests:
        return None
    if not issue.strip():
        issue = "Make the following failing test case(s) pass without breaking other tests:\n" + "\n".join(tests)
    return Task(_resolve_repo(repo, derived, commit), issue, tests)


def _ask(prompt: str, default: str = "") -> str:
    try:
        val = input(prompt).strip()
    except EOFError:
        raise SystemExit(0)
    return val or default


def _read_block(prompt: str) -> str:
    _print(prompt)
    _print("  (paste, then finish with a line containing only EOF - or Ctrl-D)")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            if not lines:
                return "quit"  # Ctrl-D on an empty prompt ends the session
            break
        if line.strip() in ("EOF", "END"):
            break
        if not lines and line.strip().lower() in ("quit", "exit", "q"):
            return line.strip().lower()
        if not lines and (_is_issue_url(line) or (line.strip() and Path(line.strip()).expanduser().is_file())):
            return line.strip()  # a single URL/path needs no terminator
        lines.append(line)
    return "\n".join(lines).strip()


def interactive_task(last_repo: str) -> Task | None:
    """Ask for issue, repository and optional test cases. Bad input re-asks only the part that failed."""
    while True:
        _print("")
        raw = _read_block("Issue / test case: paste a GitHub issue URL, a path to an issue file, or the issue text.\n"
                          "  (type 'quit' to exit)")
        if raw.strip().lower() in ("quit", "exit", "q"):
            return None
        try:
            issue, derived = _issue_from_text(raw) if raw else ("", "")
        except FETCH_ERRORS as e:
            _print(f"{e}")
            continue
        default_repo = derived or last_repo or (str(Path.cwd()) if Path.cwd().resolve() != HARNESS_ROOT else "")
        while True:
            repo = _ask(f"Repository path or git URL{f' [{default_repo}]' if default_repo else ''}: ", default_repo)
            try:
                root = _resolve_repo(repo, "", os.environ.get("COMMIT") or None)
                break
            except FETCH_ERRORS as e:
                _print(f"Cannot use that repository: {e}")
                default_repo = ""
        tests = _split_tests(_ask("Failing test(s) that must pass (ids or commands, comma-separated; Enter to skip): "))
        if not issue.strip() and not tests:
            _print("Nothing to do: give an issue or at least one test case.")
            continue
        if not issue.strip():
            issue = "Make the following failing test case(s) pass without breaking other tests:\n" + "\n".join(tests)
        return Task(root, issue, tests)


def _install_signal_handlers() -> None:
    import signal

    def _raise(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, _raise)
            except (ValueError, OSError):
                pass


def cmd_run(args) -> int:
    from harness.controller.controller import Controller
    from harness.provider import ProviderError, build_provider
    from harness.telemetry import Telemetry

    overrides: dict = {}
    if args.max_steps:
        overrides.setdefault("budget", {})["max_steps"] = args.max_steps
    if args.max_wall:
        overrides.setdefault("budget", {})["max_wall_s"] = args.max_wall
    cfg = load_config(args.config, overrides)
    try:
        task = task_from_sources(args)
    except (ValueError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as e:
        _print(f"ERROR: {e}")
        return 2
    if task is not None:
        return execute_task(cfg, task, args)
    if not sys.stdin.isatty() or args.headless:
        _print("No issue provided. Pipe it on stdin, or use ISSUE_URL=... / ISSUE_FILE=... / ISSUE='...' / TESTS=... "
               "(with REPO=...), or run `make run` in a terminal for the interactive session.")
        return 2
    if not args.plain and sys.stdout.isatty():
        from harness.tui import launch

        try:
            launch(cfg, scripted=args.scripted)
        except KeyboardInterrupt:
            return 130
        return 0
    return interactive_session(cfg, args)


def cmd_tui(args) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _print("The terminal dashboard needs an interactive terminal. Use `make run` for piped or headless tasks.")
        return 2
    from harness.tui import TaskForm, launch

    cfg = load_config(args.config)
    form = TaskForm(workspace=args.repo or "", issue=args.issue or args.issue_file or "",
                    tests=", ".join(args.test or []))
    try:
        launch(cfg, scripted=args.scripted, form=form)
    except KeyboardInterrupt:
        return 130
    return 0


def interactive_session(cfg, args) -> int:
    from harness.provider import detect_provider

    key = os.environ.get("AI_API_KEY", "")
    _print("=" * 78)
    _print(f"  Evidence-Gated Coding Harness {__version__}")
    _print(f"  model: {cfg.model.get('name') or '(provider default)'} | provider: {detect_provider(cfg.model, key)} | "
           f"AI_API_KEY: {'set' if key else 'MISSING'}")
    _print(f"  budget: {cfg.budget['max_steps']} steps, {cfg.budget['max_wall_s']}s, {cfg.budget['max_attempts']} attempt(s) | "
           "results are verified by running tests on original vs patched code")
    _print("=" * 78)
    last_repo = os.environ.get("REPO", "")
    code = 0
    while True:
        task = interactive_task(last_repo)
        if task is None:
            _print("bye")
            return code
        last_repo = str(task.root)
        code = execute_task(cfg, task, args)
        if _ask("\nSolve another issue? [y/N]: ", "n").lower() not in ("y", "yes"):
            return code


def execute_task(cfg, task: "Task", args) -> int:
    from harness.controller.controller import Controller
    from harness.provider import ProviderError, build_provider
    from harness.telemetry import Telemetry

    root, issue = task.root, task.issue
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^\w-]", "", root.name)[:30]
    run_dir = cfg.runs_dir() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "issue.md").write_text(REDACT(issue) + ("\n\nTest case(s): " + ", ".join(task.tests) if task.tests else ""), encoding="utf-8")
    (run_dir / "config.json").write_text(json.dumps(cfg.redacted(), indent=2), encoding="utf-8")
    tel = Telemetry(run_dir, verbose=not args.quiet)
    try:
        if args.scripted:
            from harness.provider.scripted import ScriptedProvider

            provider = ScriptedProvider(json.loads(Path(args.scripted).read_text(encoding="utf-8")))
        else:
            provider = build_provider(cfg.model, log=tel.say)
    except ProviderError as e:
        _print(f"ERROR: {e}")
        return 2
    tel.say(f"harness {__version__} | model {provider.describe()} | repo {root} | run {run_dir}")
    tel.event("run_start", version=__version__, model=provider.describe(), repo=str(root), config=cfg.redacted())
    ctrl = Controller(cfg, root, issue, run_dir, provider, tel, target_tests=task.tests)
    _install_signal_handlers()
    try:
        result = ctrl.run()
    except KeyboardInterrupt:
        _print("interrupted: the working tree was restored to the last candidate state")
        try:
            ctrl.ws.finish(run_dir)
        except Exception:
            pass
        return 130
    finally:
        tel.close()
    _print("")
    _print("=" * 78)
    _print(f"STATUS: {result.status}")
    _print(result.summary)
    _print("-" * 78)
    if result.patch:
        _print(result.patch if len(result.patch) < 20000 else result.patch[:20000] + "\n[... patch truncated in console; see patch.diff]")
    else:
        _print("(no patch)")
    _print("-" * 78)
    m = result.metrics
    _print(f"model calls={m.get('model_calls')} tokens in/out={m.get('tokens_in')}/{m.get('tokens_out')} "
           f"(cached {m.get('tokens_cached')}) steps={m.get('steps')} wall={m.get('wall_s')}s attempts={m.get('attempts')}")
    _print(f"artifacts: {result.run_dir}  (patch.diff, result.json, report.md, trace.jsonl, ledger.jsonl, logs/)")
    _print(f"The patch is applied in the working tree of {root}.")
    if cfg.run.get("strict_exit_code") and result.status != "VERIFIED":
        return 1
    return 0


def cmd_self_check(args) -> int:
    from harness.provider import detect_provider
    from harness.skills import load_skills

    ok = True
    cfg = load_config(args.config)
    _print(f"harness {__version__} self-check")
    _print(f"- python {sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 9):
        _print("  ERROR: Python >= 3.9 required")
        ok = False
    git = shutil.which("git")
    _print(f"- git: {git or 'MISSING (required)'}")
    ok = ok and bool(git)
    _print(f"- ripgrep: {shutil.which('rg') or 'not found (falls back to git grep)'}")
    _print(f"- config: {cfg.source}")
    key = os.environ.get("AI_API_KEY", "")
    _print(f"- AI_API_KEY: {'present' if key else 'NOT SET (required for make run)'}")
    prov = detect_provider(cfg.model, key)
    _print(f"- provider: {prov} | model: {cfg.model.get('name') or '(default for provider)'} | tool_mode: {cfg.model.get('tool_mode')}")
    skills = load_skills()
    _print(f"- skills loaded: {len(skills)} ({', '.join(skills)})")
    cfg.runs_dir().mkdir(parents=True, exist_ok=True)
    _print(f"- runs dir: {cfg.runs_dir()}")
    _print("self-check " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def cmd_smoke(args) -> int:
    """Offline end-to-end run on the bundled fixture with a scripted model (no API key needed)."""
    import tempfile

    from tests.fixtures.fixture_repo import SCRIPTED_SOLUTION, make_fixture_repo
    from harness.controller.controller import Controller
    from harness.provider.scripted import ScriptedProvider
    from harness.telemetry import Telemetry

    with tempfile.TemporaryDirectory() as td:
        repo, issue = make_fixture_repo(Path(td) / "repo")
        cfg = load_config(args.config, {"run": {"runs_dir": str(Path(td) / "runs")}})
        run_dir = cfg.runs_dir() / "smoke"
        tel = Telemetry(run_dir, verbose=not args.quiet)
        res = Controller(cfg, repo, issue, run_dir, ScriptedProvider(list(SCRIPTED_SOLUTION)), tel).run()
        tel.close()
        _print(f"smoke: {res.status}")
        return 0 if res.status == "VERIFIED" else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness", description="Evidence-gated AI coding harness")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--self-check", action="store_true", help="validate the installation and exit")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="solve an issue in a repository")
    r.add_argument("--repo", help="path or git URL of the repository")
    r.add_argument("--commit", help="commit to check out after cloning")
    r.add_argument("--issue", help="issue text")
    r.add_argument("--issue-file", help="file containing the issue")
    r.add_argument("--issue-url", help="GitHub issue URL")
    r.add_argument("--test", action="append", help="failing test id/command that must pass (repeatable; the 'test case')")
    r.add_argument("--config", help="config TOML (default configuration-files/harness.toml)")
    r.add_argument("--max-steps", type=int)
    r.add_argument("--max-wall", type=int, help="wall-clock budget in seconds")
    r.add_argument("--scripted", help="JSON file of scripted model replies (offline demo/testing)")
    r.add_argument("--headless", action="store_true", help="never prompt interactively")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--plain", action="store_true", help="use the line-oriented session instead of the dashboard")
    t = sub.add_parser("tui", help="interactive terminal dashboard")
    t.add_argument("--repo", help="initial repository path or git URL")
    t.add_argument("--issue", help="initial issue text or URL")
    t.add_argument("--issue-file", help="initial issue file")
    t.add_argument("--test", action="append", help="initial failing test ID or command")
    t.add_argument("--config", help="config TOML (default configuration-files/harness.toml)")
    t.add_argument("--scripted", help="scripted model replies for offline testing")
    s = sub.add_parser("smoke", help="offline end-to-end smoke test")
    s.add_argument("--config")
    s.add_argument("--quiet", action="store_true")
    c = sub.add_parser("self-check")
    c.add_argument("--config")
    args = p.parse_args(argv)
    if args.self_check or args.cmd == "self-check":
        args.config = getattr(args, "config", None)
        return cmd_self_check(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "tui":
        return cmd_tui(args)
    if args.cmd == "smoke":
        return cmd_smoke(args)
    p.print_help()
    return 0
