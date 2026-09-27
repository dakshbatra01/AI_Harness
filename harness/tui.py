"""Optional standard-library terminal dashboard for interactive harness runs."""
from __future__ import annotations

import curses
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import time
from threading import Thread
from typing import Any
import uuid

from harness import __version__
from harness.util import REDACT


@dataclass
class TaskForm:
    workspace: str = ""
    issue: str = ""
    tests: str = ""

    def task(self):
        from harness.cli import Task, _issue_from_text, _resolve_repo, _split_tests

        tests = _split_tests(self.tests)
        if not self.issue.strip() and not tests:
            raise ValueError("Enter an issue or at least one failing test.")
        issue, derived_repo = _issue_from_text(self.issue) if self.issue.strip() else ("", "")
        if not issue.strip():
            issue = "Make the following failing test case(s) pass without breaking other tests:\n" + "\n".join(tests)
        root = _resolve_repo(self.workspace, derived_repo, None)
        return Task(root, issue, tests)


class QueueStream:
    """Send redacted progress lines to the UI without writing into curses' screen."""

    def __init__(self, updates: Queue[tuple[str, Any]]) -> None:
        self.updates = updates

    def write(self, value: str) -> int:
        for line in value.splitlines():
            if line.strip():
                self.updates.put(("event", REDACT(line.strip())[:240]))
        return len(value)

    def flush(self) -> None:
        pass


def _run_task(form: TaskForm, cfg, scripted: str | None, updates: Queue[tuple[str, Any]]) -> None:
    from harness.controller.controller import Controller
    from harness.provider import build_provider
    from harness.telemetry import Telemetry

    tel = None
    try:
        task = form.task()
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        run_dir = cfg.runs_dir() / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "issue.md").write_text(REDACT(task.issue), encoding="utf-8")
        (run_dir / "config.json").write_text(json.dumps(cfg.redacted(), indent=2), encoding="utf-8")
        tel = Telemetry(run_dir, verbose=True, stream=QueueStream(updates))
        if scripted:
            from harness.provider.scripted import ScriptedProvider

            provider = ScriptedProvider(json.loads(Path(scripted).read_text(encoding="utf-8")))
        else:
            provider = build_provider(cfg.model, log=tel.say)
        tel.say(f"harness {__version__} | model {provider.describe()} | repo {task.root}")
        tel.event("run_start", version=__version__, model=provider.describe(), repo=str(task.root), config=cfg.redacted())
        result = Controller(cfg, task.root, task.issue, run_dir, provider, tel, target_tests=task.tests).run()
        report = result.report
        updates.put(("result", {
            "status": result.status,
            "summary": result.summary,
            "changed_files": report.changed_files if report else [],
            "patch": result.patch,
            "run_dir": str(result.run_dir),
        }))
    except Exception as exc:
        updates.put(("error", REDACT(f"Run failed: {type(exc).__name__}: {exc}")[:220]))
    finally:
        if tel is not None:
            tel.close()


def _write(screen, row: int, col: int, value: object, width: int, *, bold: bool = False) -> None:
    height, total = screen.getmaxyx()
    if row < 0 or row >= height or col < 0 or col >= total or width <= 0:
        return
    clean = "".join(" " if c in "\r\n\t" else c for c in REDACT(str(value)) if c.isprintable() or c in "\r\n\t")
    visible = clean[:max(0, min(width, total - col - 1))]
    try:
        screen.addstr(row, col, visible, curses.A_BOLD if bold else curses.A_NORMAL)
    except curses.error:
        pass


def _ask(screen, label: str, initial: str = "") -> str | None:
    value = list(initial)
    cursor = len(value)
    screen.timeout(-1)
    try:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        while True:
            height, width = screen.getmaxyx()
            if height < 4 or width < 20:
                return None
            screen.move(height - 2, 0)
            screen.clrtoeol()
            _write(screen, height - 2, 1, label + " (Enter saves, Esc cancels, Ctrl-U clears)", width - 2, bold=True)
            screen.move(height - 1, 0)
            screen.clrtoeol()
            usable = width - 3
            offset = max(0, cursor - usable + 1)
            _write(screen, height - 1, 1, "".join(value[offset:offset + usable]), usable)
            try:
                screen.move(height - 1, min(cursor - offset + 1, width - 2))
                screen.refresh()
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return "".join(value)
            if key == "\x1b":
                return None
            if key == "\x15":
                value, cursor = [], 0
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                if cursor:
                    value.pop(cursor - 1)
                    cursor -= 1
            elif key == curses.KEY_DC:
                if cursor < len(value):
                    value.pop(cursor)
            elif key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == curses.KEY_HOME:
                cursor = 0
            elif key == curses.KEY_END:
                cursor = len(value)
            elif isinstance(key, str) and key.isprintable() and len(value) < 8192:
                value.insert(cursor, key)
                cursor += 1
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(150)


def _issue(screen, current: str) -> str | None:
    lines: list[str] = []
    for index in range(80):
        label = "Issue line, URL, or file path (single . to finish)" if index == 0 else "Next line (. to finish)"
        line = _ask(screen, label)
        if line is None:
            return None
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip() or current


def _dashboard(screen, cfg, scripted: str | None, form: TaskForm) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(150)
    updates: Queue[tuple[str, Any]] = Queue()
    events: list[str] = []
    message = "Set a workspace and issue, then press R to run."
    result: dict[str, Any] | None = None
    running = False
    view_patch = False
    scroll = 0

    while True:
        try:
            while True:
                kind, payload = updates.get_nowait()
                if kind == "event":
                    events.append(str(payload))
                    events = events[-100:]
                elif kind == "result":
                    result, running = payload, False
                    message = f"Finished: {result['status']}. Press D for the patch."
                else:
                    running = False
                    message = str(payload)
        except Empty:
            pass

        screen.erase()
        height, width = screen.getmaxyx()
        if height < 16 or width < 60:
            _write(screen, 0, 1, "Resize terminal to at least 60 x 16 (Q quits when idle)", width - 2)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")) and not running:
                return
            continue

        _write(screen, 0, 2, "AI CODING HARNESS  |  Evaluator dashboard", width - 4, bold=True)
        _write(screen, 1, 2, f"Model: {cfg.model.get('name') or '(provider default)'}  |  AI_API_KEY: {'set' if os.environ.get('AI_API_KEY') else 'missing'}", width - 4)
        _write(screen, 2, 2, "W workspace  I issue  T target tests  R run  D patch  Q quit", width - 4)
        _write(screen, 3, 2, "=" * (width - 4), width - 4)
        if view_patch:
            _write(screen, 4, 2, "Patch viewer  |  Up/Down scroll  D return", width - 4, bold=True)
            lines = (result.get("patch", "") if result else "No result yet").splitlines() or ["No patch"]
            max_scroll = max(0, len(lines) - (height - 7))
            scroll = max(0, min(scroll, max_scroll))
            for offset, line in enumerate(lines[scroll:scroll + height - 7]):
                _write(screen, 5 + offset, 2, line, width - 4)
            _write(screen, height - 2, 2, f"Line {scroll + 1}/{len(lines)}", width - 4)
        else:
            _write(screen, 4, 2, f"Workspace: {form.workspace or '(choose with W)'}", width - 4)
            _write(screen, 5, 2, f"Issue: {form.issue.replace(chr(10), ' / ') or '(enter with I)'}", width - 4)
            _write(screen, 6, 2, f"Target tests: {form.tests or '(optional)'}", width - 4)
            _write(screen, 8, 2, f"Status: {'RUNNING' if running else (result['status'] if result else 'READY')}", width - 4, bold=True)
            if result:
                _write(screen, 9, 2, f"Reason: {result['summary']}", width - 4)
                _write(screen, 10, 2, f"Changed: {', '.join(result['changed_files']) or '(none)'}", width - 4)
                _write(screen, 11, 2, f"Artifacts: {result['run_dir']}", width - 4)
            _write(screen, 13, 2, "Recent activity", width - 4, bold=True)
            for offset, event in enumerate(events[-max(0, height - 17):]):
                _write(screen, 14 + offset, 3, event, width - 6)
            _write(screen, height - 2, 2, message, width - 4)
        screen.refresh()
        key = screen.getch()
        if key == -1:
            continue
        if view_patch:
            if key in (ord("d"), ord("D"), 27):
                view_patch = False
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                scroll -= 1
            continue
        if key in (ord("q"), ord("Q")):
            if running:
                message = "Run in progress; wait for completion before quitting."
            else:
                return
        elif key in (ord("d"), ord("D")):
            view_patch, scroll = True, 0
        elif running:
            message = "Run in progress; fields are locked."
        elif key in (ord("w"), ord("W")):
            entered = _ask(screen, "Workspace path or git URL", form.workspace)
            if entered is not None:
                form.workspace = entered
        elif key in (ord("i"), ord("I")):
            entered = _issue(screen, form.issue)
            if entered is not None:
                form.issue = entered
        elif key in (ord("t"), ord("T")):
            entered = _ask(screen, "Failing test IDs or commands (comma separated)", form.tests)
            if entered is not None:
                form.tests = entered
        elif key in (ord("r"), ord("R")):
            if not form.issue.strip() and not form.tests.strip():
                message = "Enter an issue or at least one failing test."
            else:
                events.clear()
                result = None
                running = True
                message = "Running..."
                snapshot = TaskForm(form.workspace, form.issue, form.tests)
                Thread(target=_run_task, args=(snapshot, cfg, scripted, updates), daemon=True).start()


def launch(cfg, *, scripted: str | None = None, form: TaskForm | None = None) -> None:
    curses.wrapper(_dashboard, cfg, scripted, form or TaskForm())
