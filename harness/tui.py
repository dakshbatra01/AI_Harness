"""Optional standard-library terminal dashboard for interactive harness runs."""
from __future__ import annotations

import curses
from dataclasses import dataclass
import json
import os
from pathlib import Path
from queue import Empty, Queue
import time
import textwrap
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
            "report": (result.run_dir / "report.md").read_text(encoding="utf-8", errors="replace")
                      if (result.run_dir / "report.md").is_file() else "",
            "metrics": result.metrics,
            "run_dir": str(result.run_dir),
        }))
    except Exception as exc:
        updates.put(("error", REDACT(f"Run failed: {type(exc).__name__}: {exc}")[:220]))
    finally:
        if tel is not None:
            tel.close()


def _write(screen, row: int, col: int, value: object, width: int, *, bold: bool = False,
           selected: bool = False) -> None:
    height, total = screen.getmaxyx()
    if row < 0 or row >= height or col < 0 or col >= total or width <= 0:
        return
    clean = "".join(" " if c in "\r\n\t" else c for c in REDACT(str(value)) if c.isprintable() or c in "\r\n\t")
    visible = clean[:max(0, min(width, total - col - 1))]
    try:
        style = (curses.A_BOLD if bold else curses.A_NORMAL) | (curses.A_REVERSE if selected else 0)
        screen.addstr(row, col, visible, style)
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
            elif key == "\x01":
                cursor = 0
            elif key == "\x05":
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
    """Editable issue pane. F2/Ctrl-G saves; a single '.' line keeps the old finish shortcut."""
    lines = current.split("\n") if current else [""]
    row, col, top = len(lines) - 1, len(lines[-1]), 0
    screen.timeout(-1)
    try:
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            if height < 10 or width < 45:
                _write(screen, 1, 2, "Resize terminal to edit the issue (Esc cancels)", width - 4)
                screen.refresh()
                key = screen.get_wch()
                if key == "\x1b":
                    return None
                continue
            visible = height - 7
            top = min(top, row)
            top = max(top, row - visible + 1)
            _write(screen, 0, 2, "EDIT ISSUE  |  text, GitHub issue URL, or file path", width - 4, bold=True)
            _write(screen, 1, 2, "Enter new line  F2 / Ctrl-G save  Esc cancel  Ctrl-U clear line", width - 4)
            _write(screen, 2, 2, "A line containing only . also saves (the dot is removed)", width - 4)
            _write(screen, 3, 2, "=" * (width - 4), width - 4)
            left = 7
            usable = width - left - 3
            offset = max(0, col - usable + 1)
            for i in range(top, min(len(lines), top + visible)):
                _write(screen, 4 + i - top, 2, f"{i + 1:>3} ", 5, selected=i == row)
                _write(screen, 4 + i - top, left, lines[i][offset if i == row else 0:], usable)
            _write(screen, height - 2, 2, f"Line {row + 1}/{len(lines)}  Column {col + 1}  "
                   f"{sum(map(len, lines))} characters", width - 4)
            screen.move(4 + row - top, min(width - 2, left + col - offset))
            screen.refresh()
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in (curses.KEY_F0 + 2, "\x07"):
                return "\n".join(lines).strip()
            if key == "\x1b":
                return None
            if key in ("\n", "\r", curses.KEY_ENTER):
                if lines[row] == ".":
                    lines.pop(row)
                    return "\n".join(lines).strip()
                lines[row:row + 1] = [lines[row][:col], lines[row][col:]]
                row, col = row + 1, 0
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                if col:
                    lines[row] = lines[row][:col - 1] + lines[row][col:]
                    col -= 1
                elif row:
                    col = len(lines[row - 1])
                    lines[row - 1] += lines.pop(row)
                    row -= 1
            elif key == curses.KEY_DC:
                if col < len(lines[row]):
                    lines[row] = lines[row][:col] + lines[row][col + 1:]
                elif row + 1 < len(lines):
                    lines[row] += lines.pop(row + 1)
            elif key == curses.KEY_LEFT:
                if col:
                    col -= 1
                elif row:
                    row -= 1
                    col = len(lines[row])
            elif key == curses.KEY_RIGHT:
                if col < len(lines[row]):
                    col += 1
                elif row + 1 < len(lines):
                    row, col = row + 1, 0
            elif key == curses.KEY_UP:
                row = max(0, row - 1)
                col = min(col, len(lines[row]))
            elif key == curses.KEY_DOWN:
                row = min(len(lines) - 1, row + 1)
                col = min(col, len(lines[row]))
            elif key in (curses.KEY_HOME, "\x01"):
                col = 0
            elif key in (curses.KEY_END, "\x05"):
                col = len(lines[row])
            elif key == "\x15":
                lines[row], col = "", 0
            elif isinstance(key, str) and key.isprintable() and len(lines) < 500 \
                    and sum(map(len, lines)) < 50000:
                lines[row] = lines[row][:col] + key + lines[row][col:]
                col += 1
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(150)


def _short(text: str, width: int) -> str:
    value = " ".join(text.split())
    return value if len(value) <= width else value[:max(0, width - 3)] + "..."


def _wrapped(text: str, width: int, count: int) -> list[str]:
    return (textwrap.wrap(" ".join(text.split()), max(10, width), break_long_words=True) or [""])[:count]


def _dashboard(screen, cfg, scripted: str | None, form: TaskForm) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    screen.timeout(150)
    updates: Queue[tuple[str, Any]] = Queue()
    events: list[str] = []
    message = "Tab selects a field; Enter edits it. Press ? for help."
    result: dict[str, Any] | None = None
    running = False
    started_at = 0.0
    view = "main"
    previous_view = "main"
    selected = 0
    activity_offset = 0
    viewer_scroll = 0
    viewer_column = 0

    while True:
        try:
            while True:
                kind, payload = updates.get_nowait()
                if kind == "event":
                    events.append(str(payload))
                    if activity_offset:
                        activity_offset += 1  # keep the same lines visible while reading older events
                    events = events[-500:]
                elif kind == "result":
                    result, running = payload, False
                    message = f"Finished: {result['status']}. D shows the patch; V shows the evidence report."
                else:
                    running = False
                    message = str(payload)
        except Empty:
            pass

        screen.erase()
        height, width = screen.getmaxyx()
        if height < 20 or width < 70:
            _write(screen, 0, 1, "Resize terminal to at least 70 x 20 (Q quits when idle)", width - 2)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")) and not running:
                return
            continue

        _write(screen, 0, 2, "AI CODING HARNESS  |  Evaluator dashboard", width - 4, bold=True)
        _write(screen, 1, 2, f"Model: {cfg.model.get('name') or '(provider default)'}  |  "
               f"API key: {'set' if os.environ.get('AI_API_KEY') else ('scripted demo' if scripted else 'MISSING')}",
               width - 4)
        _write(screen, 2, 2, "Tab choose  Enter edit  R run  D patch  V report  ? help  Q quit", width - 4)
        _write(screen, 3, 2, "=" * (width - 4), width - 4)
        if view == "help":
            _write(screen, 4, 2, "KEYBOARD HELP", width - 4, bold=True)
            help_lines = [
                "Tab / Shift-Tab   Select workspace, issue, tests, or Run",
                "Enter             Edit selected field or start selected Run",
                "W / I / T / R     Direct shortcuts for those four actions",
                "Issue editor      Enter adds a line; F2 or Ctrl-G saves; Esc cancels",
                "                  A single . line also saves; Ctrl-U clears a line",
                "Up / Down         Scroll recent activity on the dashboard",
                "PgUp / PgDn       Scroll a page; Home / End jump to oldest / live",
                "D                 Open final patch (after a run)",
                "V                 Open the evidence report (after a run)",
                "Patch / report    Arrows, PgUp/PgDn, Home/End; Left/Right pan",
                "Q                 Quit when no run is active",
            ]
            for i, line in enumerate(help_lines[:height - 8]):
                _write(screen, 6 + i, 3, line, width - 6)
            _write(screen, height - 2, 2, "Press ? or Esc to return", width - 4)
        elif view in ("patch", "report"):
            name = "Patch viewer" if view == "patch" else "Evidence report"
            _write(screen, 4, 2, f"{name}  |  Arrows/PgUp/PgDn scroll  Home/End jump  Esc returns", width - 4, bold=True)
            content = result.get(view, "") if result else ""
            lines = content.splitlines() or [f"No {view} available"]
            visible = height - 7
            viewer_scroll = max(0, min(viewer_scroll, max(0, len(lines) - visible)))
            for i, line in enumerate(lines[viewer_scroll:viewer_scroll + visible]):
                _write(screen, 5 + i, 2, line[viewer_column:], width - 4)
            _write(screen, height - 2, 2,
                   f"Lines {viewer_scroll + 1}-{min(len(lines), viewer_scroll + visible)}/{len(lines)}"
                   f"  |  Column {viewer_column + 1}  |  Esc return", width - 4)
        else:
            fields = [
                f"Workspace: {form.workspace or '(path or Git URL; optional for a GitHub issue URL)'}",
                f"Issue: {_short(form.issue, width - 18) or '(text, file path, or GitHub issue URL)'}",
                f"Target tests: {form.tests or '(optional; test IDs or commands)'}",
                "[ Run task ]  Starts the coding agent and verifier",
            ]
            for i, field in enumerate(fields):
                _write(screen, 4 + i, 2, ("> " if i == selected else "  ") + field,
                       width - 4, selected=i == selected)
            _write(screen, 8, 2, "=" * (width - 4), width - 4)
            status = "RUNNING" if running else (result["status"] if result else "READY")
            duration = f"  ({time.monotonic() - started_at:.0f}s elapsed)" if running else ""
            _write(screen, 9, 2, f"Status: {status}{duration}", width - 4, bold=True)
            if result:
                metrics = result.get("metrics") or {}
                _write(screen, 10, 2, f"Model calls: {metrics.get('model_calls', '?')}  |  "
                       f"Tokens in/out: {metrics.get('tokens_in', '?')}/{metrics.get('tokens_out', '?')}  |  "
                       f"Cached: {metrics.get('tokens_cached', '?')}  |  Time: {metrics.get('wall_s', '?')}s",
                       width - 4)
                for i, line in enumerate(_wrapped("Reason: " + result["summary"], width - 5, 2)):
                    _write(screen, 11 + i, 2, line, width - 4)
                _write(screen, 13, 2, "Changed: " + _short(", ".join(result["changed_files"]) or "(none)", width - 15), width - 4)
                _write(screen, 14, 2, "Artifacts: " + _short(result["run_dir"], width - 16), width - 4)
            else:
                _write(screen, 11, 2, "Enter an issue or test, then run. Results and evidence appear here.", width - 4)
            _write(screen, 15, 2, "=" * (width - 4), width - 4)
            visible = height - 19
            activity_offset = max(0, min(activity_offset, max(0, len(events) - visible)))
            start = max(0, len(events) - visible - activity_offset)
            shown = events[start:start + visible]
            label = "LIVE" if activity_offset == 0 else "PAUSED (End resumes live)"
            _write(screen, 16, 2, f"Recent activity  {start + 1 if events else 0}-{start + len(shown)}/{len(events)}  "
                   f"| {label}", width - 4, bold=True)
            for i, event in enumerate(shown):
                _write(screen, 17 + i, 3, event, width - 6)
            _write(screen, height - 2, 2, _short(message, width - 5), width - 4)
        screen.refresh()
        key = screen.getch()
        if key == -1:
            continue
        if key in (ord("?"), ord("h"), ord("H")):
            if view == "help":
                view = previous_view
            else:
                previous_view, view = view, "help"
            continue
        if view == "help":
            if key == 27:
                view = previous_view
            continue
        if view in ("patch", "report"):
            if key == 27:
                view = "main"
            elif key in (ord("d"), ord("D"), ord("v"), ord("V")):
                requested = "patch" if key in (ord("d"), ord("D")) else "report"
                view = "main" if requested == view else requested
                viewer_scroll = viewer_column = 0
            elif key in (curses.KEY_DOWN, ord("j")):
                viewer_scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                viewer_scroll -= 1
            elif key == curses.KEY_NPAGE:
                viewer_scroll += height - 7
            elif key == curses.KEY_PPAGE:
                viewer_scroll -= height - 7
            elif key == curses.KEY_HOME:
                viewer_scroll = 0
            elif key == curses.KEY_END:
                viewer_scroll = len(lines)
            elif key == curses.KEY_RIGHT:
                viewer_column += 8
            elif key == curses.KEY_LEFT:
                viewer_column = max(0, viewer_column - 8)
            continue
        if key in (ord("q"), ord("Q")):
            if running:
                message = "Run in progress; wait for completion before quitting."
            else:
                return
        elif key in (ord("d"), ord("D"), ord("v"), ord("V")):
            name = "patch" if key in (ord("d"), ord("D")) else "report"
            if result and result.get(name):
                view, viewer_scroll, viewer_column = name, 0, 0
            else:
                message = f"No {name} available yet. Run a task first."
        elif key in (9, curses.KEY_BTAB):
            selected = (selected + (-1 if key == curses.KEY_BTAB else 1)) % 4
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE,
                     curses.KEY_HOME, curses.KEY_END):
            if key == curses.KEY_UP:
                activity_offset += 1
            elif key == curses.KEY_DOWN:
                activity_offset -= 1
            elif key == curses.KEY_PPAGE:
                activity_offset += height - 19
            elif key == curses.KEY_NPAGE:
                activity_offset -= height - 19
            elif key == curses.KEY_HOME:
                activity_offset = len(events)
            else:
                activity_offset = 0
        elif key in (ord("w"), ord("W"), ord("i"), ord("I"), ord("t"), ord("T"),
                     ord("r"), ord("R"), 10, 13, curses.KEY_ENTER):
            action = {ord("w"): 0, ord("W"): 0, ord("i"): 1, ord("I"): 1,
                      ord("t"): 2, ord("T"): 2, ord("r"): 3, ord("R"): 3}.get(key, selected)
            selected = action
            if running:
                message = "Run in progress; fields are locked. You can scroll activity or press ? for help."
            elif action == 0:
                entered = _ask(screen, "Workspace path or git URL", form.workspace)
                if entered is not None and entered != form.workspace:
                    form.workspace, result = entered, None
                    events.clear()
                    activity_offset = 0
                    message = "Workspace updated. Enter the issue and press R to run."
            elif action == 1:
                entered = _issue(screen, form.issue)
                if entered is not None and entered != form.issue:
                    form.issue, result = entered, None
                    events.clear()
                    activity_offset = 0
                    message = "Issue updated. Press R when ready."
            elif action == 2:
                entered = _ask(screen, "Failing test IDs or commands (comma separated)", form.tests)
                if entered is not None and entered != form.tests:
                    form.tests, result = entered, None
                    events.clear()
                    activity_offset = 0
                    message = "Target tests updated. Press R when ready."
            elif not form.issue.strip() and not form.tests.strip():
                message = "Enter an issue or at least one failing test first."
            elif not scripted and not os.environ.get("AI_API_KEY"):
                message = "AI_API_KEY is missing. Export it before make run, then reopen the dashboard."
            else:
                events.clear()
                activity_offset = 0
                result = None
                running = True
                started_at = time.monotonic()
                message = "Running... follow recent activity below."
                snapshot = TaskForm(form.workspace, form.issue, form.tests)
                Thread(target=_run_task, args=(snapshot, cfg, scripted, updates), daemon=True).start()


def launch(cfg, *, scripted: str | None = None, form: TaskForm | None = None) -> None:
    curses.wrapper(_dashboard, cfg, scripted, form or TaskForm())
