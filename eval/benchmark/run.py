"""Run the harness on the self-authored benchmark and grade it with hidden tests.

    PATH=<python-with-pytest>/bin:$PATH AI_API_KEY=... HARNESS_CONFIG=config/profiles/tight-8k.toml \
        python3 eval/benchmark/run.py [--only E1,M2] [--run-id NAME]

Per task: fresh copy of the project (git-initialised, like a cloned repo) -> the real Controller (same code
path as `make run`) -> hidden tests + the project's full visible suite. A task is RESOLVED only if both pass.
FALSE VERIFIED = the harness said VERIFIED but grading failed (the most important error to avoid)."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from harness.config import load_config  # noqa: E402
from harness.controller.controller import Controller  # noqa: E402
from harness.provider import ProviderError, build_provider  # noqa: E402
from harness.telemetry import Telemetry  # noqa: E402
from harness.util import scrubbed_env  # noqa: E402

GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "-c", "commit.gpgsign=false"]


def grade(repo: Path, task: dict) -> tuple[bool, str]:
    hidden = repo / f"test_hidden_{task['id']}.py"
    shutil.copy(HERE / "hidden" / task["hidden"], hidden)
    env = scrubbed_env({"PYTHONPATH": str(repo)})
    out = []
    ok = True
    for target in (hidden.name, "tests"):
        p = subprocess.run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", target], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=600)
        summary = [ln for ln in p.stdout.splitlines() if ln.strip()][-1:] or [p.stderr[-200:]]
        out.append(f"{'hidden' if target != 'tests' else 'suite'}: {summary[0]}")
        ok &= p.returncode == 0
    hidden.unlink()
    return ok, " | ".join(out)


def run_task(task: dict, cfg, provider, out_dir: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / task["project"]
        shutil.copytree(HERE / "projects" / task["project"], repo)
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "initial"]):
            subprocess.run(GIT + cmd, cwd=repo, check=True, capture_output=True)
        issue = task["issue"] or ("Make the following failing test case(s) pass without breaking other tests:\n"
                                  + "\n".join(task["tests"]))
        run_dir = out_dir / task["id"]
        tel = Telemetry(run_dir, verbose=True)
        t0 = time.time()
        try:
            res = Controller(cfg, repo, issue, run_dir, provider, tel, target_tests=task["tests"]).run()
            status, metrics = res.status, res.metrics
        except ProviderError as e:
            status, metrics = f"PROVIDER_ERROR: {e}", {}
        finally:
            tel.close()
        wall = round(time.time() - t0, 1)
        resolved, detail = grade(repo, task)
        return {
            "id": task["id"], "level": task["level"], "kind": task["kind"], "status": status, "resolved": resolved,
            "false_verified": status == "VERIFIED" and not resolved, "grade": detail, "wall_s": wall,
            "model_calls": metrics.get("model_calls"), "tokens_in": metrics.get("tokens_in"),
            "tokens_out": metrics.get("tokens_out"), "steps": metrics.get("steps"), "attempts": metrics.get("attempts"),
            "end_reason": metrics.get("end_reason"), "route": metrics.get("route"), "tool_mode": metrics.get("tool_mode"),
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    args = ap.parse_args()
    tasks = json.loads((HERE / "tasks.json").read_text())
    if args.only:
        wanted = args.only.split(",")
        tasks = [t for t in tasks if t["id"] in wanted]
    cfg = load_config()
    provider = build_provider(cfg.model, log=lambda m: print(m, flush=True))  # one provider: shared rate budget
    out_dir = HERE / "results" / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    results_file = out_dir / "results.json"
    for t in tasks:
        print(f"\n===== {t['id']} ({t['level']}, {t['kind']}) =====", flush=True)
        row = run_task(t, cfg, provider, out_dir)
        rows.append(row)
        results_file.write_text(json.dumps({"model": provider.describe(), "config": cfg.source, "rows": rows}, indent=1))
        print(f"--> {row['id']}: harness={row['status']} resolved={row['resolved']} calls={row['model_calls']} "
              f"tokens={row['tokens_in']}/{row['tokens_out']} wall={row['wall_s']}s  [{row['grade']}]", flush=True)
        if "retry-after" in f"{row['status']} {row['end_reason']}":
            print("daily quota reached - stopping; rerun later with --only for the remaining tasks")
            break
    print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
