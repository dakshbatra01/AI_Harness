"""Workspace state tracking via a private "shadow" git repository.

The shadow repo lives in the run directory (never inside the target repo) and uses the
target repo as its work tree. This gives us, independent of the repo's own .git:
  * a base snapshot that includes any pre-existing (dirty) changes -> patches contain ONLY
    our changes;
  * a content hash for every candidate (git tree hash), so evidence binds to exact patches;
  * exact diffs including new files;
  * exact restore / base-swap (run tests on the base code) without `git reset --hard`,
    `git clean` or `git stash` in the user's repository.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
from pathlib import Path

from harness.util import scrubbed_env

SCRATCH_DIR = ".harness_scratch"

# Applied only to files that are untracked at base (the base snapshot already holds every
# pre-existing file), so they filter command side effects, never real source.
DEFAULT_EXCLUDES = [
    f"/{SCRATCH_DIR}/",
    "__pycache__/",
    "*.py[cod]",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".hypothesis/",
    ".tox/",
    ".nox/",
    ".venv/",
    "venv/",
    "node_modules/",
    "*.egg-info/",
    ".eggs/",
    ".coverage",
    ".coverage.*",
    "htmlcov/",
    "coverage.xml",
    "*.so",
    "*.o",
    "*.dylib",
    "*.pyd",
    ".DS_Store",
    "target/debug/",
    "target/release/",
    ".gradle/",
    ".idea/",
    ".vscode/",
]


class GitError(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: Path, state_dir: Path) -> None:
        self.root = Path(root).resolve()
        self.state_dir = Path(state_dir).resolve()
        self.git_dir = self.state_dir / "shadow.git"
        self.base_commit = ""
        self.base_tree = ""
        self.real_git_head = ""
        self.initially_dirty = False
        self.scratch = self.root / SCRATCH_DIR

    # ---- low level ---------------------------------------------------------------------
    def _env(self) -> dict:
        return scrubbed_env(
            {
                "GIT_DIR": str(self.git_dir),
                "GIT_WORK_TREE": str(self.root),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_LITERAL_PATHSPECS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "LC_ALL": "C",
            }
        )

    def git(self, *args: str, input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            "git",
            "-c", "core.quotepath=off",
            "-c", "core.autocrlf=false",
            "-c", "core.safecrlf=false",
            "-c", "gc.auto=0",
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            *args,
        ]
        proc = subprocess.run(cmd, cwd=self.root, env=self._env(), input=input, capture_output=True)
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {proc.stderr.decode('utf-8', 'replace')[:800]}")
        return proc

    def _out(self, *args: str) -> str:
        return self.git(*args).stdout.decode("utf-8", "surrogateescape").strip()

    # ---- lifecycle -------------------------------------------------------------------------
    def init(self) -> None:
        if shutil.which("git") is None:
            raise GitError("git is required but was not found on PATH")
        if not self.root.is_dir():
            raise GitError(f"repository path does not exist: {self.root}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._record_real_git()
        self.git("init", "-q")
        for k, v in (
            ("user.name", "harness"),
            ("user.email", "harness@localhost"),
            ("commit.gpgsign", "false"),
            ("core.bare", "false"),
        ):
            self.git("config", k, v)
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8")
        # Byte-exact snapshots: ignore the repo's .gitattributes (eol/text/filters/LFS) entirely.
        (info / "attributes").write_text("* -text -eol -filter -ident -working-tree-encoding\n", encoding="utf-8")
        self.scratch.mkdir(exist_ok=True)
        self.git("add", "-A", ".")
        self._add_real_tracked_files()
        self.git("commit", "-q", "--allow-empty", "--no-verify", "-m", "harness base snapshot")
        self.base_commit = self._out("rev-parse", "HEAD")
        self.base_tree = self._out("rev-parse", "HEAD^{tree}")
        self.git("update-ref", "refs/harness/base", self.base_commit)

    def _record_real_git(self) -> None:
        if not (self.root / ".git").exists():
            return
        env = scrubbed_env({"LC_ALL": "C"})
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(k, None)
        try:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, env=env, capture_output=True, timeout=30)
            self.real_git_head = head.stdout.decode().strip() if head.returncode == 0 else ""
            st = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=self.root, env=env, capture_output=True, timeout=60
            )
            self.initially_dirty = bool(st.stdout.strip())
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _add_real_tracked_files(self) -> None:
        """Files tracked by the real repo but matched by our excludes are still base content."""
        if not (self.root / ".git").exists():
            return
        env = scrubbed_env({"LC_ALL": "C"})
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(k, None)
        try:
            real = subprocess.run(["git", "ls-files", "-z"], cwd=self.root, env=env, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return
        if real.returncode != 0:
            return
        real_files = {p for p in real.stdout.decode("utf-8", "surrogateescape").split("\0") if p}
        mine = {p for p in self.git("ls-files", "-z").stdout.decode("utf-8", "surrogateescape").split("\0") if p}
        missing = [p for p in sorted(real_files - mine) if (self.root / p).is_file() and not p.startswith(SCRATCH_DIR + "/")]
        for i in range(0, len(missing), 200):
            self.git("add", "-f", "--", *missing[i : i + 200])

    # ---- state queries ---------------------------------------------------------------------
    def tree(self) -> str:
        """Content hash of the current work tree (excluding scratch/caches)."""
        self.git("add", "-A", "--ignore-errors", ".", check=False)
        return self._out("write-tree")

    def changed(self, a: str | None = None, b: str | None = None) -> list[tuple[str, str]]:
        a = a or self.base_tree
        b = b or self.tree()
        if a == b:
            return []
        raw = self.git("diff", "--name-status", "--no-renames", "-z", a, b).stdout.decode("utf-8", "surrogateescape")
        parts = [p for p in raw.split("\0") if p]
        return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]

    def diff(self, a: str | None = None, b: str | None = None, paths: list[str] | None = None, stat: bool = False) -> str:
        a = a or self.base_tree
        b = b or self.tree()
        args = ["diff", "--no-color", "--no-ext-diff", "--no-renames", "--binary"]
        if stat:
            args.append("--stat=120")
        args += [a, b]
        if paths:
            args += ["--", *paths]
        return self.git(*args).stdout.decode("utf-8", "surrogateescape")

    def commit(self, tree: str, label: str) -> str:
        c = self.git("commit-tree", tree, "-p", self.base_commit, "-m", label).stdout.decode().strip()
        self.git("update-ref", f"refs/harness/{label}", c)
        return c

    def base_content(self, path: str) -> bytes | None:
        proc = self.git("cat-file", "-p", f"{self.base_tree}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    # ---- mutation --------------------------------------------------------------------------
    def restore(self, target_tree: str) -> None:
        cur = self.tree()
        if cur == target_tree:
            return
        entries = self.changed(cur, target_tree)
        to_delete = [p for s, p in entries if s.startswith("D")]
        to_checkout = [p for s, p in entries if not s.startswith("D")]
        for p in to_delete:
            fp = self.root / p
            if fp.is_file() or fp.is_symlink():
                fp.unlink()
                self._prune_dirs(fp.parent)
        for i in range(0, len(to_checkout), 200):
            chunk = to_checkout[i : i + 200]
            for p in chunk:  # a directory in the way of a file would block checkout
                fp = self.root / p
                if fp.is_dir() and not fp.is_symlink():
                    shutil.rmtree(fp)
            self.git("checkout", target_tree, "--", *chunk)
        now = self.tree()
        if now != target_tree:
            raise GitError(f"restore mismatch: expected tree {target_tree[:12]}, got {now[:12]}")

    def _prune_dirs(self, d: Path) -> None:
        while d != self.root and d.is_dir() and self.root in d.parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent

    def revert_paths(self, paths: list[str]) -> list[str]:
        """Restore files/directories to base (modes and symlinks included); delete ones created since."""
        cur = self.tree()
        changes = self.changed(self.base_tree, cur)
        done = []
        for p in paths:
            p = p.strip().strip("/")
            hits = [(st, x) for st, x in changes if x == p or x.startswith(p + "/")]
            if not hits:
                continue
            for st, x in hits:
                fx = self.root / x
                if st.startswith("A"):
                    if fx.is_symlink() or fx.is_file():
                        fx.unlink()
                        self._prune_dirs(fx.parent)
                else:
                    if fx.is_dir() and not fx.is_symlink():
                        shutil.rmtree(fx)
                    self.git("checkout", self.base_tree, "--", x)
            done.append(p)
        return done

    @contextlib.contextmanager
    def base_swap(self, keep_paths: list[str] | None = None):
        """Temporarily put the work tree at base (optionally keeping candidate versions of
        some files, e.g. new/changed tests) and always restore the candidate afterwards."""
        cand = self.tree()
        keep: dict[str, tuple[bytes, int]] = {}
        for p in keep_paths or []:
            fp = self.root / p
            if fp.is_file() and not fp.is_symlink():
                keep[p] = (fp.read_bytes(), fp.stat().st_mode)
        try:
            self.restore(self.base_tree)
            for p, (data, mode) in keep.items():
                fp = self.root / p
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_bytes(data)
                os.chmod(fp, mode & 0o7777)
            yield
        finally:
            self.restore(cand)

    def finish(self, run_dir: Path, keep_scratch: bool = False) -> None:
        if self.scratch.is_dir():
            dest = run_dir / "scratch"
            if any(self.scratch.iterdir()):
                shutil.copytree(self.scratch, dest, dirs_exist_ok=True)
            if not keep_scratch:
                shutil.rmtree(self.scratch, ignore_errors=True)
