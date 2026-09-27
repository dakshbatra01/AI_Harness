"""Offline evaluation runner (dev-time L0 loop): run the harness over a task set, then grade each
patch with hidden tests the agent never saw. Reports resolution, false-VERIFIED rate and cost.

Task file (JSON list):
  {"id": "...", "repo": "<path | git URL | fixture:durations>", "commit": "<optional>",
   "issue": "<text>" | "issue_file": "<path>",
   "hidden_tests": {"relative/path.py": "<content>"},      # written AFTER the run
   "grade_command": "python -m pytest -q tests/test_hidden.py"}
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from harness.config import HARNESS_ROOT, load_config
from harness.util import scrubbed_env


def _materialize(task: dict, dest: Path) -> tuple[Path, str]:
    repo = task["repo"]
    if repo.startswith("fixture:"):
        from tests.fixtures.fixture_repo import make_fixture_repo

        root, issue = make_fixture_repo(dest)
        return root, task.get("issue") or issue
    if repo.startswith(("http", "git@")):
        subprocess.run(["git", "clone", "--quiet", repo, str(dest)], check=True)
    else:
        shutil.copytree(Path(repo).expanduser(), dest, symlinks=True)
    if task.get("commit"):
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", task["commit"]], check=True)
    issue = task.get("issue") or Path(task["issue_file"]).read_text(encoding="utf-8")
    return dest, issue


def run_task(task: dict, cfg, out_dir: Path, scripted: list | None) -> dict:
    from harness.controller.controller import Controller
    from harness.provider import build_provider
    from harness.provider.scripted import ScriptedProvider
    from harness.telemetry import Telemetry

    with tempfile.TemporaryDirectory() as td:
        root, issue = _materialize(task, Path(td) / "repo")
        run_dir = out_dir / task["id"]
        tel = Telemetry(run_dir, verbose=True)
        provider = ScriptedProvider(list(scripted)) if scripted is not None else build_provider(cfg.model, log=tel.say)
        t0 = time.time()
        try:
            res = Controller(cfg, root, issue, run_dir, provider, tel).run()
            status, metrics = res.status, res.metrics
        except Exception as e:  # an infra error is a result, not a crash of the whole eval
            status, metrics = f"HARNESS_ERROR: {e!r}", {}
        finally:
            tel.close()
        resolved = None
        grade_log = ""
        if task.get("grade_command"):
            for rel, content in (task.get("hidden_tests") or {}).items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            g = subprocess.run(task["grade_command"], shell=True, cwd=root, env=scrubbed_env({"PYTHONPATH": str(root)}),
                               capture_output=True, text=True, timeout=1800)
            resolved = g.returncode == 0
            grade_log = (g.stdout + g.stderr)[-3000:]
        return {"id": task["id"], "status": status, "resolved": resolved, "false_verified": status == "VERIFIED" and resolved is False,
                "wall_s": round(time.time() - t0, 1), "metrics": metrics, "grade_tail": grade_log}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness.eval")
    ap.add_argument("--tasks", required=True, help="JSON task set for this optional local evaluator")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--only", help="comma-separated task ids")
    ap.add_argument("--scripted", help="JSON list of scripted replies (offline pipeline check)")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    tasks = json.loads(Path(a.tasks).read_text(encoding="utf-8"))
    if a.only:
        keep = set(a.only.split(","))
        tasks = [t for t in tasks if t["id"] in keep]
    cfg = load_config(a.config)
    out = HARNESS_ROOT / "eval" / "runs" / a.run_id
    out.mkdir(parents=True, exist_ok=True)
    scripted = json.loads(Path(a.scripted).read_text()) if a.scripted else None
    rows = [run_task(t, cfg, out, scripted) for t in tasks]
    graded = [r for r in rows if r["resolved"] is not None]
    summary = {
        "run_id": a.run_id,
        "tasks": len(rows),
        "resolved": sum(1 for r in graded if r["resolved"]),
        "resolve_rate": round(sum(1 for r in graded if r["resolved"]) / max(1, len(graded)), 3),
        "verified": sum(1 for r in rows if r["status"] == "VERIFIED"),
        "false_verified": sum(1 for r in rows if r["false_verified"]),
        "model_calls": sum(r["metrics"].get("model_calls", 0) for r in rows),
        "tokens_in": sum(r["metrics"].get("tokens_in", 0) for r in rows),
        "tokens_out": sum(r["metrics"].get("tokens_out", 0) for r in rows),
        "wall_s": round(sum(r["wall_s"] for r in rows), 1),
        "per_task": rows,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "per_task"}, indent=2))
    for r in rows:
        print(f"  {r['id']:<30} {r['status']:<14} resolved={r['resolved']} calls={r['metrics'].get('model_calls')} wall={r['wall_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
