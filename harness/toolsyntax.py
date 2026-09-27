"""Tool-call dialects that models emit as plain text. Some models (and some server-side parsers) produce
`<function=NAME><parameter=KEY>VALUE</parameter></function>` instead of the requested format; accepting it
turns a lost turn into a normal action."""
from __future__ import annotations

import json
import re

_FUNC_TAG = re.compile(r"<function=([\w.\-]+)>(.*?)</function>", re.S)
_FUNC_PARAM = re.compile(r"<parameter=([\w\-]+)>(.*?)</parameter>", re.S)


def _strip1(v: str) -> str:
    if v.startswith("\n"):
        v = v[1:]
    if v.endswith("\n"):
        v = v[:-1]
    return v


def parse_function_tags(text: str) -> list[tuple[str, list[tuple[str, str]], int]]:
    """[(tool_name, [(param, raw_value), ...], start_offset)] for every <function=...> block."""
    out = []
    for m in _FUNC_TAG.finditer(text or ""):
        name = m.group(1).rsplit(".", 1)[-1]
        params = [(p.group(1), _strip1(p.group(2))) for p in _FUNC_PARAM.finditer(m.group(2))]
        out.append((name, params, m.start()))
    return out


def loose_args(params: list[tuple[str, str]]) -> dict:
    """Schema-free argument decoding: JSON when a value parses as a list/object/number, else the raw string."""
    args: dict = {}
    for k, v in params:
        s = v.strip()
        val = v
        if s[:1] in "[{" or re.fullmatch(r"-?\d+", s) or s in ("true", "false"):
            try:
                val = json.loads(s)
            except ValueError:
                val = v
        args[k] = val
    return args
