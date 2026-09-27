import pytest

from taskflow import load_pipeline, run_pipeline
from taskflow.core.registry import ActionRegistry
from taskflow.io.jsonio import to_dict
from taskflow.io.reporter import render
from taskflow.util.errors import ConfigError


def flaky_registry(failures: int):
    reg = ActionRegistry()
    calls = {"n": 0}

    @reg.register("flaky")
    def flaky(**_):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise RuntimeError(f"attempt {calls['n']} failed")
        return "ok"

    @reg.register("noop")
    def noop(**_):
        return None

    return reg, calls


def pipe(retries=None):
    task = {"name": "fetch", "action": "flaky"}
    if retries is not None:
        task["retries"] = retries
    return load_pipeline({"tasks": [task, {"name": "use", "action": "noop", "deps": ["fetch"]}]})


def test_retries_until_success():
    reg, calls = flaky_registry(2)
    run = run_pipeline(pipe(2), registry=reg)
    r = run.by_name()["fetch"]
    assert (r.status, r.attempts, calls["n"]) == ("ok", 3, 3)
    assert run.by_name()["use"].status == "ok"


def test_gives_up_after_retries():
    reg, calls = flaky_registry(5)
    run = run_pipeline(pipe(1), registry=reg)
    assert run.by_name()["fetch"].status == "failed"
    assert run.by_name()["fetch"].attempts == 2
    assert calls["n"] == 2
    assert run.by_name()["use"].status == "skipped"


def test_default_is_single_attempt():
    reg, calls = flaky_registry(1)
    run = run_pipeline(pipe(), registry=reg)
    assert run.by_name()["fetch"].attempts == 1
    assert calls["n"] == 1


@pytest.mark.parametrize("bad", [-1, "2", True, 1.5])
def test_invalid_retries_rejected(bad):
    with pytest.raises(ConfigError):
        pipe(bad)


def test_report_and_json_show_attempts():
    reg, _ = flaky_registry(2)
    run = run_pipeline(pipe(3), registry=reg)
    lines = render(run).splitlines()
    assert any(l.startswith("[+] fetch") and "(attempts: 3)" in l for l in lines)
    assert any(l.startswith("[+] use") and "attempts" not in l for l in lines)
    data = to_dict(run)
    assert [r["attempts"] for r in data["results"]] == [3, 1]
