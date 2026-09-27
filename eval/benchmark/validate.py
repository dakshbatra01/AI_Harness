"""Validate every benchmark task: hidden tests FAIL on the starting code and PASS (with the full visible suite)
once the reference solution is applied. Run: PATH=<python with pytest>:$PATH python3 eval/benchmark/validate.py"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest(repo: Path, *targets: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets], cwd=repo,
                          capture_output=True, text=True, env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin"})


def main() -> int:
    ok = True
    for t in json.loads((HERE / "tasks.json").read_text()):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / t["project"]
            shutil.copytree(HERE / "projects" / t["project"], repo)
            hidden = repo / f"test_hidden_{t['id']}.py"
            shutil.copy(HERE / "hidden" / t["hidden"], hidden)
            base_fail = pytest(repo, hidden.name).returncode != 0
            for f in (HERE / "reference" / t["id"]).rglob("*.py"):
                shutil.copy(f, repo / f.relative_to(HERE / "reference" / t["id"]))
            ref_hidden = pytest(repo, hidden.name)
            ref_suite = pytest(repo, "tests")
            good = base_fail and ref_hidden.returncode == 0 and ref_suite.returncode == 0
            ok &= good
            print(f"{t['id']:3} {t['level']:6} hidden fails on start={base_fail}  reference passes hidden={ref_hidden.returncode == 0}"
                  f"  reference passes suite={ref_suite.returncode == 0}  -> {'OK' if good else 'BROKEN'}")
            if not good:
                print(ref_hidden.stdout[-1500:], ref_suite.stdout[-1500:])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
