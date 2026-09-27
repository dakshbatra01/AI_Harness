"""Tool schemas (one static superset, never mutated mid-run) + the model-agnostic text protocol.

Native mode: schemas are sent as provider tools.
Text mode:   the model writes XML-ish blocks that need no escaping of code:

  <tool name="edit_file">
  <path>pkg/mod.py</path>
  <old_text>
  exact existing lines
  </old_text>
  <new_text>
  replacement lines
  </new_text>
  </tool>
"""
from __future__ import annotations

import json
import re

from harness.provider.base import ToolCall
from harness.toolsyntax import loose_args, parse_function_tags

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
SA = {"type": "array", "items": {"type": "string"}}


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": desc,
        # No additionalProperties/empty required: the harness validates strictly itself (validate_args), and every
        # schema byte is resent on each call.
        "parameters": {"type": "object", "properties": props, **({"required": required} if required else {})},
    }


def tool_specs(skill_names: list[str]) -> list[dict]:
    # Descriptions are resent on every model call: every word must earn its place.
    specs = [
        _tool(
            "search_code",
            "Search the repo. mode: regex (default) | literal | symbol (definitions) | file (paths by substring/glob).",
            {"query": S, "mode": {"type": "string", "enum": ["regex", "literal", "symbol", "file"]}, "path": S, "glob": S},
            ["query"],
        ),
        _tool(
            "read_file",
            "Numbered view of ~100 lines from start (give end for more), or mode=skeleton for an outline. A directory is listed.",
            {"path": S, "start": I, "end": I, "mode": {"type": "string", "enum": ["lines", "skeleton"]}},
            ["path"],
        ),
        _tool(
            "edit_file",
            "Replace text in one file. Each old_text must match EXACTLY ONCE (copy from read_file without line numbers, "
            "same indentation, enough context). Edits apply all-or-nothing; syntax-breaking edits are rejected.",
            {
                "path": S,
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"old_text": S, "new_text": S},
                        "required": ["old_text", "new_text"],
                        "additionalProperties": False,
                    },
                },
            },
            ["path", "edits"],
        ),
        _tool(
            "write_file",
            "Create a file (reproducer in .harness_scratch/, new test/module). overwrite=true replaces a small existing file.",
            {"path": S, "content": S, "overwrite": B},
            ["path", "content"],
        ),
        _tool(
            "run_command",
            "Run a non-interactive bash command at the repo root (cwd: repo-relative dir). Long output is cut; see read_log.",
            {"command": S, "timeout": I, "cwd": S},
            ["command"],
        ),
        _tool(
            "run_tests",
            "Run tests (files, node ids, modules) or a full command; returns per-test outcomes, compared with the "
            "original code once you have changes (fixed / regressed / still failing).",
            {"tests": SA, "command": S, "timeout": I},
            [],
        ),
        _tool(
            "read_log",
            "Read an earlier truncated output by log_id: a line range or lines matching grep.",
            {"log_id": S, "start": I, "end": I, "grep": S},
            ["log_id"],
        ),
        _tool(
            "repo_changes",
            "action=diff (default): your patch vs the original. action=revert: restore paths to original (deletes created files).",
            {"action": {"type": "string", "enum": ["diff", "revert"]}, "paths": SA},
            [],
        ),
        _tool(
            "update_plan",
            "Pin your hypothesis, acceptance criteria, plan and confirmed facts/ruled-out places (kept across restarts).",
            {"hypothesis": S, "acceptance": S, "plan": S, "notes": S},
            [],
        ),
        _tool(
            "submit",
            "Request verification (tests run on original vs patched code; the harness decides). Evidence: test_commands "
            "/ fail_to_pass ids failing before and passing after, and/or reproducer (command of a .harness_scratch/ script).",
            {"summary": S, "test_commands": SA, "fail_to_pass": SA, "reproducer": S},
            ["summary"],
        ),
    ]
    if skill_names:
        specs.append(
            _tool(
                "activate_skill",
                "Load a skill's full procedure.",
                {"name": {"type": "string", "enum": sorted(skill_names)}},
                ["name"],
            )
        )
    return specs


def text_protocol_doc(specs: list[dict]) -> str:
    lines = [
        "Respond with a brief THOUGHT (what you learned, what question the next action answers), then one or more tool",
        "blocks (at most a few per reply; they run in order). Parameters are child tags; values are raw text (no",
        "escaping, no JSON). Array parameters: one item per line, or repeat the tag. For edit_file, repeat",
        "<old_text>/<new_text> pairs for several edits in the same file.",
        "",
        "Available tools:",
    ]
    for s in specs:
        params = s["parameters"]["properties"]
        req = set(s["parameters"].get("required", []))
        ps = []
        for k, v in params.items():
            t = v.get("type")
            if k == "edits":
                ps.append("<old_text>…</old_text><new_text>…</new_text> (required, repeatable)")
                continue
            enum = f" one of {'|'.join(v['enum'])}" if "enum" in v else ""
            ps.append(f"<{k}>{t}{enum}{' (required)' if k in req else ''}</{k}>")
        lines.append(f"- {s['name']}: {s['description']}\n    params: {' '.join(ps) or '(none)'}")
    lines += [
        "",
        "Example:",
        "THOUGHT: The traceback points at parse_duration; I need to see how units are combined.",
        '<tool name="read_file">',
        "<path>src/timeparse.py</path>",
        "<start>40</start>",
        "</tool>",
    ]
    return "\n".join(lines)


_TOOL_BLOCK = re.compile(r"<tool\s+name\s*=\s*[\"']?([A-Za-z_]+)[\"']?\s*>(.*?)</tool>", re.S)
_PARAM = re.compile(r"<([a-z_]+)>(.*?)</\1>", re.S)
_BASH_BLOCK = re.compile(r"```(?:bash|sh|shell)\n(.*?)```", re.S)


def _strip1(v: str) -> str:
    if v.startswith("\r\n"):
        v = v[2:]
    elif v.startswith("\n"):
        v = v[1:]
    if v.endswith("\r\n"):
        v = v[:-2]
    elif v.endswith("\n"):
        v = v[:-1]
    return v


def parse_text_actions(text: str, specs_by_name: dict[str, dict], limit: int) -> tuple[list[ToolCall], str]:
    """Parse tool blocks from model text. Returns (calls, thought_text)."""
    calls: list[ToolCall] = []
    first = None
    for i, m in enumerate(_TOOL_BLOCK.finditer(text)):
        if first is None:
            first = m.start()
        name, body = m.group(1), m.group(2)
        raw: dict[str, list[str]] = {}
        order: list[tuple[str, str]] = []
        for pm in _PARAM.finditer(body):
            k, v = pm.group(1), _strip1(pm.group(2))
            raw.setdefault(k, []).append(v)
            order.append((k, v))
        spec = specs_by_name.get(name)
        args, err = _coerce_text_args(spec, raw, order) if spec else ({}, None)
        calls.append(ToolCall(id=f"t{i}", name=name, args=args, parse_error=err))
        if len(calls) >= limit:
            break
    if not calls:
        for i, (name, params, start) in enumerate(parse_function_tags(text)[:limit]):
            # `<function=NAME><parameter=K>V</parameter></function>` dialect: same coercion as our own format.
            if first is None:
                first = start
            raw: dict[str, list[str]] = {}
            for k, v in params:
                raw.setdefault(k, []).append(v)
            spec = specs_by_name.get(name)
            args, err = _coerce_text_args(spec, raw, params) if spec else (loose_args(params), None)
            calls.append(ToolCall(id=f"f{i}", name=name, args=args, parse_error=err))
    if not calls:
        # Lenient fallback: a fenced bash block is treated as run_command.
        bm = _BASH_BLOCK.search(text)
        if bm and bm.group(1).strip():
            calls.append(ToolCall(id="t0", name="run_command", args={"command": bm.group(1).strip()}))
            first = bm.start()
        else:
            jm = re.search(r"\{\s*\"tool\"\s*:\s*\"(\w+)\"\s*,\s*\"args\"\s*:\s*(\{.*\})\s*\}", text, re.S)
            if jm:
                try:
                    calls.append(ToolCall(id="t0", name=jm.group(1), args=json.loads(jm.group(2))))
                    first = jm.start()
                except ValueError:
                    pass
    thought = text[:first].strip() if first is not None else text.strip()
    return calls, thought


def _coerce_text_args(spec: dict, raw: dict[str, list[str]], order: list[tuple[str, str]]):
    props = spec["parameters"]["properties"]
    args: dict = {}
    if "edits" in props:
        olds = [v for k, v in order if k == "old_text"]
        news = [v for k, v in order if k == "new_text"]
        if len(olds) != len(news):
            return {}, f"edit_file needs matching <old_text>/<new_text> pairs (got {len(olds)} old, {len(news)} new)"
        args["edits"] = [{"old_text": o, "new_text": n} for o, n in zip(olds, news)]
    for k, vals in raw.items():
        if k in ("old_text", "new_text") and "edits" in props:
            continue
        if k not in props:
            continue
        t = props[k].get("type")
        if t == "array":
            items: list[str] = []
            for v in vals:
                v = v.strip()
                if v.startswith("["):
                    try:
                        items += [str(x) for x in json.loads(v)]
                        continue
                    except ValueError:
                        pass
                items += [ln.strip() for ln in v.splitlines() if ln.strip()]
            args[k] = items
        elif t == "integer":
            try:
                args[k] = int(vals[-1].strip())
            except ValueError:
                return {}, f"parameter <{k}> must be an integer"
        elif t == "boolean":
            args[k] = vals[-1].strip().lower() in ("true", "1", "yes")
        elif k in ("content", "command"):
            args[k] = vals[-1]
        else:
            args[k] = vals[-1].strip() if k not in ("old_text", "new_text") else vals[-1]
    return args, None


def validate_args(spec: dict, args: dict) -> str | None:
    """Strict schema check; returns an actionable error or None."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    params = spec["parameters"]
    props = params["properties"]
    # Convenience: accept flat old_text/new_text for edit_file.
    if spec["name"] == "edit_file" and "edits" not in args and "old_text" in args and "new_text" in args:
        args["edits"] = [{"old_text": args.pop("old_text"), "new_text": args.pop("new_text")}]
    unknown = [k for k in args if k not in props]
    if unknown:
        return f"unknown parameter(s): {', '.join(unknown)}; allowed: {', '.join(props)}"
    missing = [k for k in params.get("required", []) if k not in args or args[k] in (None, "")]
    if missing:
        return f"missing required parameter(s): {', '.join(missing)}"
    for k, v in args.items():
        t = props[k].get("type")
        if t == "string" and not isinstance(v, str):
            if isinstance(v, (int, float)):
                args[k] = str(v)
            else:
                return f"parameter '{k}' must be a string"
        if t == "integer" and not isinstance(v, int):
            try:
                args[k] = int(v)
            except (TypeError, ValueError):
                return f"parameter '{k}' must be an integer"
        if t == "boolean" and not isinstance(v, bool):
            args[k] = str(v).lower() in ("true", "1", "yes")
        if t == "array":
            if isinstance(v, str) and v.strip().startswith("["):
                # Some models send the array JSON-encoded inside a string ("[{...}]"): decode it.
                try:
                    decoded = json.loads(v)
                    if isinstance(decoded, list):
                        args[k] = v = decoded
                except ValueError:
                    pass
            if isinstance(v, str):
                args[k] = [x for x in (v.splitlines() if "\n" in v else [v]) if x.strip()]
            elif not isinstance(v, list):
                return f"parameter '{k}' must be an array"
        if "enum" in props[k] and args[k] not in props[k]["enum"]:
            return f"parameter '{k}' must be one of {props[k]['enum']}"
    if spec["name"] == "edit_file":
        for i, e in enumerate(args.get("edits") or [], 1):
            if not isinstance(e, dict) or "old_text" not in e or "new_text" not in e:
                return f"edits[{i}] must have old_text and new_text"
    return None


def render_call_as_text(name: str, args: dict) -> str:
    """Used when converting a native-mode history to text mode."""
    parts = [f'<tool name="{name}">']
    for k, v in args.items():
        if k == "edits":
            for e in v:
                parts.append(f"<old_text>\n{e.get('old_text', '')}\n</old_text>\n<new_text>\n{e.get('new_text', '')}\n</new_text>")
        elif isinstance(v, list):
            parts.append(f"<{k}>\n" + "\n".join(map(str, v)) + f"\n</{k}>")
        else:
            parts.append(f"<{k}>{v}</{k}>")
    parts.append("</tool>")
    return "\n".join(parts)
