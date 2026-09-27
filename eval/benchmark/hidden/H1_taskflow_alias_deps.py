from taskflow import load_pipeline, run_pipeline
from taskflow.core.graph import build_order, required_for
from taskflow.engine.events import EventLog

PIPE = {"tasks": [
    {"name": "deploy", "action": "echo", "args": {"message": "shipped"}, "deps": ["compile"]},
    {"name": "build", "action": "echo", "args": {"message": "built"}, "aliases": ["compile"]},
]}


def test_alias_dependency_orders_tasks():
    events = EventLog()
    assert build_order(load_pipeline(PIPE), events) == ["build", "deploy"]
    assert events.warnings() == []


def test_alias_dependency_is_case_insensitive():
    pipe = {"tasks": [
        {"name": "test", "action": "noop", "deps": ["Compile"]},
        {"name": "build", "action": "noop", "aliases": ["COMPILE"]},
    ]}
    assert build_order(load_pipeline(pipe)) == ["build", "test"]


def test_failure_through_alias_skips_dependent():
    pipe = {"tasks": [
        {"name": "deploy", "action": "noop", "deps": ["compile"]},
        {"name": "build", "action": "fail", "aliases": ["compile"]},
    ]}
    run = run_pipeline(load_pipeline(pipe))
    assert run.by_name()["deploy"].status == "skipped"


def test_required_for_follows_alias_dependencies():
    assert required_for(load_pipeline(PIPE), ["deploy"]) == ["build", "deploy"]


def test_unknown_dependency_still_only_warns():
    events = EventLog()
    pipe = {"tasks": [{"name": "a", "action": "noop", "deps": ["ghost"]}]}
    assert build_order(load_pipeline(pipe), events) == ["a"]
    assert len(events.warnings()) == 1
