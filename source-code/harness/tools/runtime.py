"""Command execution: explicit cwd, scrubbed env, process-group timeout kill, output caps,
immutable raw logs outside the repository, and a small denylist for dangerous/leaky commands.
A text denylist is not a security boundary; it prevents common agent mistakes."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from harness.util import REDACT, head_tail, scrubbed_env

_CMD = r"(?:^|[;&|(]\s*|\bsudo\s+|\bxargs\s+)"  # command position
_GIT = r"\bgit\s+(?:-C\s+\S+\s+|-c\s+\S+\s+)*"
DENY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(_GIT + r"push\b"), "pushing is not allowed"),
    (re.compile(_GIT + r"(fetch|pull|remote\s+(add|set-url))\b"), "network git operations are not allowed"),
    (re.compile(_GIT + r"log\b[^|;&]*--all\b|" + _GIT + r"(reflog|fsck)\b"), "browsing other refs/history can leak future fixes; inspect the current code instead"),
    (re.compile(_GIT + r"(reset\s+(--hard|--merge|--keep)|clean\b|stash\b|restore\b|checkout\s+(.*\s)?(-f|--force|--)(\s|$)|checkout\s+\.(\s|$))"),
     "destructive git operations are not allowed (they can discard the user's uncommitted work); use repo_changes(action=revert) to undo your own edits"),
    (re.compile(r"\brm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~/?|\$HOME/?|\*|\./\*|\.|\.\.)(\s|$)"), "refusing broad recursive delete"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "piping downloads into a shell is not allowed"),
    (re.compile(_CMD + r"(shutdown|reboot|halt|poweroff|mkfs\w*)\b|\bdd\s+if=\S+\s+of=/dev/"), "system-level command not allowed"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"^\s*(sudo\s+)?(vi|vim|nvim|nano|emacs|less|more|top|htop|man)\b"), "interactive programs hang; use read_file / edit_file"),
]


@dataclass
class CmdResult:
    command: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    denied: str | None = None
    launch_error: str | None = None
    log_id: str = ""
    truncated: bool = False
    changed_paths: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.denied and not self.launch_error


class LogStore:
    """Immutable raw logs referenced by id; the model only ever sees bounded excerpts."""

    def __init__(self, log_dir: Path) -> None:
        self.dir = log_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._n = 0

    def new_id(self, prefix: str = "log") -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def path(self, log_id: str) -> Path:
        if not re.fullmatch(r"[a-z]+\d{4}", log_id or ""):
            raise ValueError(f"invalid log_id '{log_id}'")
        return self.dir / f"{log_id}.log"

    def write(self, log_id: str, text: str) -> None:
        self.path(log_id).write_text(REDACT(text), encoding="utf-8", errors="replace")

    def read(self, log_id: str) -> str:
        p = self.path(log_id)
        if not p.is_file():
            raise FileNotFoundError(f"no log with id {log_id}")
        return p.read_text(encoding="utf-8", errors="replace")


def check_denied(command: str) -> str | None:
    for rx, why in DENY_RULES:
        if rx.search(command):
            return why
    return None


class CommandRunner:
    def __init__(self, root: Path, logs: LogStore, head_chars: int, tail_chars: int, default_timeout: int) -> None:
        self.root = Path(root).resolve()
        self.logs = logs
        self.head = head_chars
        self.tail = tail_chars
        self.default_timeout = default_timeout
        self.shell = shutil.which("bash") or "/bin/sh"
        py_path = os.environ.get("PYTHONPATH", "")
        self.extra_env = {"PYTHONPATH": str(self.root) + (os.pathsep + py_path if py_path else "")}
        # Many agents (and issue reports) type `python`; provide it when only python3 exists.
        if shutil.which("python") is None and shutil.which("python3"):
            shim = self.logs.dir.parent / "bin"
            shim.mkdir(parents=True, exist_ok=True)
            link = shim / "python"
            if not link.exists():
                link.symlink_to(shutil.which("python3"))
            self.extra_env["PATH"] = str(shim) + os.pathsep + os.environ.get("PATH", "")

    def run(self, command: str, cwd: str | None = None, timeout: int | None = None, allow_denied: bool = False) -> CmdResult:
        timeout = int(timeout or self.default_timeout)
        log_id = self.logs.new_id("cmd")
        if not allow_denied:
            why = check_denied(command)
            if why:
                return CmdResult(command, None, "", "", 0.0, denied=why, log_id=log_id)
        workdir = self.root
        if cwd:
            cand = (self.root / cwd).resolve()
            if cand != self.root and self.root not in cand.parents:
                return CmdResult(command, None, "", "", 0.0, launch_error="cwd must be inside the repository", log_id=log_id)
            if not cand.is_dir():
                return CmdResult(command, None, "", "", 0.0, launch_error=f"cwd does not exist: {cwd}", log_id=log_id)
            workdir = cand
        out_path = self.logs.dir / f"{log_id}.stdout"
        err_path = self.logs.dir / f"{log_id}.stderr"
        t0 = time.time()
        timed_out = False
        try:
            with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
                proc = subprocess.Popen(
                    [self.shell, "-c", command],
                    cwd=workdir,
                    env=scrubbed_env(self.extra_env),
                    stdin=subprocess.DEVNULL,
                    stdout=fo,
                    stderr=fe,
                    start_new_session=True,
                )
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill_group(proc)
        except OSError as e:
            return CmdResult(command, None, "", "", time.time() - t0, launch_error=str(e), log_id=log_id)
        duration = time.time() - t0
        stdout = out_path.read_bytes().decode("utf-8", "replace")
        stderr = err_path.read_bytes().decode("utf-8", "replace")
        out_path.unlink(missing_ok=True)
        err_path.unlink(missing_ok=True)
        self.logs.write(
            log_id,
            f"$ {command}\n# cwd={workdir} exit={proc.returncode} timed_out={timed_out} duration={duration:.2f}s\n"
            f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}",
        )
        so, t1 = head_tail(REDACT(stdout), self.head, self.tail)
        se, t2 = head_tail(REDACT(stderr), self.head // 2, self.tail // 2)
        return CmdResult(
            command,
            None if timed_out else proc.returncode,
            so,
            se,
            duration,
            timed_out=timed_out,
            log_id=log_id,
            truncated=t1 or t2,
        )


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def format_cmd_result(r: CmdResult) -> str:
    if r.denied:
        return f"DENIED: {r.denied}. Command not executed."
    if r.launch_error:
        return f"LAUNCH_ERROR: {r.launch_error}"
    head = f"exit_code={r.exit_code}" if not r.timed_out else "TIMEOUT (process group killed)"
    parts = [f"[{head} | {r.duration_s:.1f}s | log_id={r.log_id}]"]
    if r.stdout.strip():
        parts.append(r.stdout.rstrip())
    if r.stderr.strip():
        parts.append("--- stderr ---\n" + r.stderr.rstrip())
    if not r.stdout.strip() and not r.stderr.strip():
        parts.append("(command produced no output)")
    if r.changed_paths:
        parts.append("files changed by this command: " + ", ".join(r.changed_paths[:15]))
    return "\n".join(parts)
