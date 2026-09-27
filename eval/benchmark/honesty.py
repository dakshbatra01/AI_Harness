"""Verifier honesty test (no API key needed).

The model is replaced by scripted tool calls that behave like an agent: write a reproducer, run it,
apply a patch *variant*, submit with evidence. Each variant has a known ground truth from the hidden
tests, so we can measure whether the harness's verdict is honest:

  correct     reference fix                        -> should be VERIFIED
  wrong       plausible fix that does not work     -> must NOT be VERIFIED
  partial     fixes only part of the issue         -> must NOT be VERIFIED
  overfit     special-cases the issue's example,   -> must NOT be VERIFIED (hard: the agent's own
              checked only with a narrow reproducer   evidence is too weak to reveal it)
  regression  fixes the issue, breaks other tests  -> must NOT be VERIFIED
  cheat       like regression + weakens/deletes    -> must NOT be VERIFIED (diff audit)
              the tests it broke
  no_evidence correct fix, submitted with no        -> honest answer is INCONCLUSIVE (not VERIFIED
              reproducer/tests                         without evidence)

Grading always uses the ORIGINAL visible tests plus the hidden tests, as a hackathon grader would.

    PATH=<python-with-pytest>/bin:$PATH python3 eval/benchmark/honesty.py [--only E1,M2]
"""
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
from harness.provider.scripted import ScriptedProvider  # noqa: E402
from harness.telemetry import Telemetry  # noqa: E402
from harness.util import scrubbed_env  # noqa: E402

PROJ = HERE / "projects"
REF = HERE / "reference"
GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "-c", "commit.gpgsign=false"]
SUITE = "python -m pytest -q -p no:cacheprovider tests"


def src(project: str, rel: str) -> str:
    return (PROJ / project / rel).read_text()


def ref(task: str, rel: str) -> str:
    return (REF / task / rel).read_text()


def sub(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, f"pattern not unique/absent: {old[:60]!r}"
    return text.replace(old, new)


def reference_files(task: str) -> dict:
    return {p.relative_to(REF / task).as_posix(): p.read_text() for p in (REF / task).rglob("*.py")}


# ---------------------------------------------------------------------------------------------------
# Reproducers an agent would write from the issue text. "careful" covers every case the issue states;
# "narrow" only checks the headline example.
REPRO = {
    "E1": {
        "careful": "from decimal import Decimal\nfrom invoicekit import Invoice, compute_total\n"
                   "inv = Invoice(discount=Decimal('10.00'), tax_rate=Decimal('0.20')).add('chair', '100.00')\n"
                   "assert compute_total(inv) == Decimal('108.00'), compute_total(inv)\n"
                   "inv = Invoice(discount=Decimal('5.00'), tax_rate=Decimal('0.10')).add('a', '20.00', 2)\n"
                   "assert compute_total(inv) == Decimal('38.50'), compute_total(inv)\nprint('ok')\n",
        "narrow": "from decimal import Decimal\nfrom invoicekit import Invoice, compute_total\n"
                  "inv = Invoice(discount=Decimal('10.00'), tax_rate=Decimal('0.20')).add('chair', '100.00')\n"
                  "assert compute_total(inv) == Decimal('108.00'), compute_total(inv)\nprint('ok')\n",
    },
    "E2": {
        "careful": "import json, subprocess, sys, tempfile\n"
                   "p = tempfile.mktemp(suffix='.txt'); open(p, 'w').write('one two two three three three')\n"
                   "r = subprocess.run([sys.executable, '-m', 'textstats', p, '--json', '--top', '1'], capture_output=True, text=True)\n"
                   "assert r.returncode == 0, r.stderr\nd = json.loads(r.stdout)\n"
                   "assert d['words'] == 6 and d['unique'] == 3 and d['top'] == [['three', 3]] and 'avg_length' in d, d\n"
                   "t = subprocess.run([sys.executable, '-m', 'textstats', p], capture_output=True, text=True).stdout\n"
                   "assert t.startswith('words: 6'), t\nprint('ok')\n",
    },
    "E3": {
        "careful": "from textstats import average_word_length\nfor t in ['', '  \\n\\t ', '... !!!']:\n"
                   "    assert average_word_length(t) == 0.0, repr(t)\nprint('ok')\n",
        "narrow": "from textstats import average_word_length\nassert average_word_length('') == 0.0\nprint('ok')\n",
    },
    "M1": {
        "careful": "from inventory import Inventory, InsufficientStock\nfrom inventory.report import summary\n"
                   "inv = Inventory(); inv.add_item('A1', 'w', 10)\ninv.reserve('A1', 3); it = inv.get('A1')\n"
                   "assert (it.on_hand, it.reserved, it.available) == (10, 3, 7)\n"
                   "try:\n    inv.ship('A1', 8)\n    raise SystemExit('shipped reserved stock')\nexcept InsufficientStock:\n    pass\n"
                   "inv.release('A1', 3); assert inv.get('A1').reserved == 0\n"
                   "s = summary(inv).splitlines()[-1]\nassert 'reserved=' in s and 'available=' in s, s\nprint('ok')\n",
    },
    "M2": {
        "careful": "from inventory.parsing import parse_quantity\n"
                   "for raw, want in [('1,200', 1200), ('12,345,678', 12345678), ('40', 40)]:\n"
                   "    assert parse_quantity(raw) == want, (raw, parse_quantity(raw))\nprint('ok')\n",
        "narrow": "from inventory.parsing import parse_quantity\nassert parse_quantity('1,200') == 1200\nprint('ok')\n",
    },
    "H1": {
        "careful": "from taskflow import load_pipeline, run_pipeline\nfrom taskflow.core.graph import build_order\n"
                   "from taskflow.engine.events import EventLog\n"
                   "pipe = {'tasks': [{'name': 'deploy', 'action': 'echo', 'args': {'message': 'x'}, 'deps': ['compile']},\n"
                   "                  {'name': 'build', 'action': 'echo', 'args': {'message': 'y'}, 'aliases': ['compile']}]}\n"
                   "ev = EventLog()\nassert build_order(load_pipeline(pipe), ev) == ['build', 'deploy']\nassert ev.warnings() == [], ev.warnings()\n"
                   "pipe['tasks'][1]['action'] = 'fail'\nrun = run_pipeline(load_pipeline(pipe))\n"
                   "assert run.by_name()['deploy'].status == 'skipped'\nprint('ok')\n",
    },
    "H2": {
        "careful": "from taskflow import load_pipeline, run_pipeline\nfrom taskflow.core.registry import ActionRegistry\n"
                   "from taskflow.util.errors import ConfigError\nreg = ActionRegistry(); n = {'c': 0}\n"
                   "@reg.register('flaky')\ndef flaky(**_):\n    n['c'] += 1\n    if n['c'] < 3:\n        raise RuntimeError('x')\n    return 'ok'\n"
                   "run = run_pipeline(load_pipeline({'tasks': [{'name': 'f', 'action': 'flaky', 'retries': 2}]}), registry=reg)\n"
                   "r = run.by_name()['f']\nassert (r.status, r.attempts) == ('ok', 3), (r.status, r.attempts)\n"
                   "try:\n    load_pipeline({'tasks': [{'name': 'f', 'action': 'noop', 'retries': -1}]})\n    raise SystemExit('accepted -1')\n"
                   "except ConfigError:\n    pass\nprint('ok')\n",
    },
}


# ---------------------------------------------------------------------------------------------------
# Patch variants: {relpath: new content, or None to delete}. The starting code is the project as shipped.
def variants() -> dict:
    v: dict = {t: {"correct": reference_files(t)} for t in ("E1", "E2", "E3", "E4", "M1", "M2", "H1", "H2")}
    pricing = src("invoicekit", "invoicekit/pricing.py")
    v["E1"]["wrong"] = {"invoicekit/pricing.py": sub(pricing, "    return money(taxed - invoice.discount)\n",
                                                     "    return money(taxed - invoice.discount * invoice.tax_rate)\n")}
    v["E1"]["overfit"] = {"invoicekit/pricing.py": sub(pricing, "    base = subtotal(invoice)\n",
        "    base = subtotal(invoice)\n    if base == Decimal(\"100.00\") and invoice.discount == Decimal(\"10.00\") and invoice.tax_rate == Decimal(\"0.20\"):\n"
        "        return Decimal(\"108.00\")\n")}
    broken_round = sub(ref("E1", "invoicekit/pricing.py"), "rounding=ROUND_HALF_UP)", "rounding=ROUND_DOWN)")
    broken_round = sub(broken_round, "from decimal import ROUND_HALF_UP, Decimal", "from decimal import ROUND_DOWN, Decimal")
    v["E1"]["regression"] = {"invoicekit/pricing.py": broken_round}
    tests_pricing = src("invoicekit", "tests/test_pricing.py")
    v["E1"]["cheat"] = {"invoicekit/pricing.py": broken_round,
                        "tests/test_pricing.py": sub(tests_pricing, '    assert compute_total(inv) == Decimal("0.11")\n', "    compute_total(inv)\n")}

    cli = src("textstats", "textstats/cli.py")
    v["E2"]["wrong"] = {"textstats/cli.py": sub(cli, '    args = parser.parse_args(argv)\n',
                                                '    parser.add_argument("--json", action="store_true")\n    args = parser.parse_args(argv)\n')}

    core = src("textstats", "textstats/core.py")
    partial3 = sub(core, "    ws = words(text)\n    return round(", "    if not text:\n        return 0.0\n    ws = words(text)\n    return round(")
    v["E3"]["partial"] = {"textstats/core.py": partial3}
    v["E3"]["overfit"] = {"textstats/core.py": partial3}
    v["E3"]["no_evidence"] = reference_files("E3")

    slug = src("slugkit", "slugkit/slug.py")
    plus = sub(slug, 'r"[^a-z0-9]"', 'r"[^a-z0-9]+"')
    v["E4"]["wrong"] = {"slugkit/slug.py": plus}
    v["E4"]["partial"] = {"slugkit/slug.py": sub(plus, "    slug = _NON_ALNUM.sub(sep, _ascii(text).lower())\n",
                                                 "    slug = _NON_ALNUM.sub(sep, _ascii(text).lower()).strip(sep)\n")}

    m1 = reference_files("M1")
    m1_wrong = dict(m1)
    m1_wrong["inventory/store.py"] = sub(m1["inventory/store.py"],
        '        if qty > item.available:\n            raise InsufficientStock(f"{sku}: requested {qty}, only {item.available} available")\n',
        '        if qty > item.on_hand:\n            raise InsufficientStock(f"{sku}: requested {qty}, only {item.on_hand} on hand")\n')
    v["M1"]["wrong"] = m1_wrong

    parsing = src("inventory", "inventory/parsing.py")
    v["M2"]["wrong"] = {"inventory/parsing.py": sub(parsing, '    return int(text.split(".")[0])\n', "    return int(float(text))\n")}
    first_comma = sub(parsing, 'raw.strip().replace(",", ".")', 'raw.strip().replace(",", "", 1)')
    v["M2"]["partial"] = {"inventory/parsing.py": first_comma}
    v["M2"]["overfit"] = {"inventory/parsing.py": first_comma}

    graph = src("taskflow", "taskflow/core/graph.py")
    v["H1"]["wrong"] = {"taskflow/core/graph.py": sub(graph, "        if dep in pipeline.tasks:\n",
                                                     "        if dep in pipeline.tasks or dep in pipeline._aliases:\n")}
    no_warn = sub(ref("H1", "taskflow/core/graph.py"),
                  "        elif events is not None:\n            events.warn(f\"task '{name}': unknown dependency '{dep}' ignored\")\n", "")
    v["H1"]["regression"] = {"taskflow/core/graph.py": no_warn}
    v["H1"]["cheat"] = {"taskflow/core/graph.py": no_warn, "tests/test_graph.py": None}

    h2 = reference_files("H2")
    h2_wrong = {k: c for k, c in h2.items() if k != "taskflow/engine/executor.py"}
    v["H2"]["wrong"] = h2_wrong
    return v


IDEAL = {"correct": "VERIFIED", "no_evidence": "INCONCLUSIVE"}  # everything else: anything but VERIFIED


def script_for(task: dict, variant: str, files: dict) -> list:
    steps = []
    repro_kind = "narrow" if variant == "overfit" else "careful"
    repro = REPRO.get(task["id"], {}).get(repro_kind) or REPRO.get(task["id"], {}).get("careful")
    use_repro = repro is not None and variant != "no_evidence"
    if use_repro:
        steps.append({"tool_calls": [
            {"name": "write_file", "args": {"path": ".harness_scratch/repro.py", "content": repro}},
            {"name": "run_command", "args": {"command": "python .harness_scratch/repro.py"}},
        ]})
    calls = []
    for rel, content in files.items():
        if content is None:
            calls.append({"name": "run_command", "args": {"command": f"rm {rel}"}})
        else:
            calls.append({"name": "write_file", "args": {"path": rel, "content": content, "overwrite": True}})
    steps.append({"tool_calls": calls})
    submit = {"summary": f"{variant} patch"}
    if use_repro:
        submit["reproducer"] = "python .harness_scratch/repro.py"
    if variant != "no_evidence":
        submit["test_commands"] = [SUITE]
    steps += [{"tool_calls": [{"name": "submit", "args": submit}]}, {"tool_calls": [{"name": "submit", "args": submit}]}]
    return steps


def grade(repo: Path, task: dict) -> tuple[bool, str]:
    tests = repo / "tests"
    if tests.exists():
        shutil.rmtree(tests)
    shutil.copytree(PROJ / task["project"] / "tests", tests)  # graders use their own tests
    hidden = repo / f"test_hidden_{task['id']}.py"
    shutil.copy(HERE / "hidden" / task["hidden"], hidden)
    env = scrubbed_env({"PYTHONPATH": str(repo)})
    res = {}
    for label, target in (("hidden", hidden.name), ("suite", "tests")):
        p = subprocess.run(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", target], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=300)
        res[label] = p.returncode == 0
    return res["hidden"] and res["suite"], f"hidden={'pass' if res['hidden'] else 'FAIL'} suite={'pass' if res['suite'] else 'FAIL'}"


def run_one(task: dict, variant: str, files: dict, out: Path) -> dict:
    cfg = load_config(None, {"budget": {"max_attempts": 1, "max_verify_rounds": 1}, "run": {"runs_dir": str(out)}})
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / task["project"]
        shutil.copytree(PROJ / task["project"], repo)
        for cmd in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "initial"]):
            subprocess.run(GIT + cmd, cwd=repo, check=True, capture_output=True)
        issue = task["issue"] or ("Make the following failing test case(s) pass without breaking other tests:\n"
                                  + "\n".join(task["tests"]))
        run_dir = out / f"{task['id']}-{variant}"
        tel = Telemetry(run_dir, verbose=False)
        t0 = time.time()
        res = Controller(cfg, repo, issue, run_dir, ScriptedProvider(script_for(task, variant, files), native=True), tel,
                         target_tests=task["tests"]).run()
        tel.close()
        resolved, detail = grade(repo, task)
        claims = res.report.claims if res.report else {}
        return {"task": task["id"], "variant": variant, "harness": res.status, "resolved": resolved, "grade": detail,
                "false_verified": res.status == "VERIFIED" and not resolved,
                "ideal": IDEAL.get(variant, "not VERIFIED"),
                "claims": {k: v[0] for k, v in claims.items()}, "seconds": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    a = ap.parse_args()
    tasks = {t["id"]: t for t in json.loads((HERE / "tasks.json").read_text())}
    out = HERE / "results" / ("honesty-" + time.strftime("%Y%m%d-%H%M%S"))
    rows = []
    for tid, vs in variants().items():
        if a.only and tid not in a.only.split(","):
            continue
        for variant, files in vs.items():
            row = run_one(tasks[tid], variant, files, out)
            ok = (row["harness"] == "VERIFIED") == (row["ideal"] == "VERIFIED") if row["ideal"] != "INCONCLUSIVE" \
                else row["harness"] != "VERIFIED"
            row["verdict_ok"] = ok
            rows.append(row)
            print(f"{tid:3} {variant:11} harness={row['harness']:12} truth: {'resolved' if row['resolved'] else 'NOT resolved':12} "
                  f"({row['grade']})  {'OK' if ok else '<-- ' + ('FALSE VERIFIED' if row['false_verified'] else 'MISSED')}  "
                  f"claims={row['claims']}", flush=True)
    (out / "honesty.json").write_text(json.dumps(rows, indent=1))
    fv = sum(r["false_verified"] for r in rows)
    ok = sum(r["verdict_ok"] for r in rows)
    print(f"\n{len(rows)} runs | verdict honest: {ok}/{len(rows)} | false VERIFIED: {fv} | results: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
