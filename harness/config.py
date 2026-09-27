"""Immutable runtime configuration.

Precedence (lowest -> highest): built-in defaults < config/harness.toml < environment
variables < CLI flags. The API key is read from AI_API_KEY only and is never stored in
the config object's printable form.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = HARNESS_ROOT / "config" / "harness.toml"

DEFAULTS: dict = {
    "model": {
        "provider": "auto",  # auto | anthropic | openai | gemini | groq | deepseek | openai_compatible
        "name": "",  # prescribed model id (AI_MODEL overrides)
        "base_url": "",  # AI_BASE_URL overrides
        "temperature": 0.0,
        "seed": 7,
        "max_output_tokens": 8192,
        "context_window": 200000,
        "tool_mode": "auto",  # auto | native | text
        "request_timeout_s": 600,
        "max_retries": 5,
        "prompt_caching": True,
        "effort": "high",  # Anthropic output_config.effort for models that support it ("" = omit)
        "mask_observations": "auto",
        "tpm_limit": 0,  # client-side tokens-per-minute pacing for rate-limited endpoints (0 = off)  # auto (off for anthropic: append-only history) | on | off
    },
    "budget": {
        "max_steps": 100,
        "max_model_calls": 120,
        "max_total_tokens": 6000000,
        "max_wall_s": 3000,
        "verification_reserve": 0.2,
        "max_attempts": 2,
        "max_verify_rounds": 4,
    },
    "tools": {
        "cmd_timeout_s": 180,
        "test_timeout_s": 900,
        "obs_head_chars": 5000,
        "obs_tail_chars": 5000,
        "search_max_hits": 60,
        "read_window_lines": 100,  # big enough for a whole function, small enough to stay focused
        "keep_full_observations": 6,  # Complexity Trap: mask older observations, keep ~last 6
        "max_actions_per_turn": 6,
        "auto_check_after_edit": True,  # re-run the last failing reproducer/test after each source edit
        "auto_check_timeout_s": 120,
        "auto_check_max_s": 30,  # skip auto-check when the check itself took longer than this
        "cache_checks": True,  # identical check on identical code returns the earlier result
        "pin_views_fraction": 0.15,  # share of the context window that may keep current file views unmasked
        "profile": "full",  # full (structured tools) | bash (shell + submit only; for A/B comparisons)
    },
    "verify": {
        "full_suite": "auto",  # auto | always | never
        "full_suite_timeout_s": 1200,
        "max_test_files_for_auto_full": 40,
        "related_test_limit": 6,
        "flaky_reruns": 1,
        "review_on_submit": True,
        "requirement_checks": "auto",  # auto (issues describing several behaviours) | on | off
        "max_requirements": 4,  # self-review: first submit of a new patch shows the diff + checklist
    },
    "context": {
        "codemap": "auto",  # auto (repos with >= codemap_min_files source files) | on | off
        "codemap_tokens": 1200,
        "codemap_min_files": 15,
        "codemap_time_s": 8,
        "codemap_max_files": 6000,
    },
    "loop": {
        "same_action_same_obs": 4,
        "same_action_error": 3,
        "no_action_msgs": 3,
        "ping_pong": 6,
        "same_failure_signature": 3,
        "locate_nudge_steps": 14,
        "edit_without_check_nudge": 3,
        "implicit_submit": True,  # a prose final answer with pending changes is treated as submit
    },
    "run": {
        "runs_dir": "runs",
        "strict_exit_code": False,
        "keep_scratch": False,
        "remove_scratch_on_finish": True,
    },
}

# Flat env overrides that are convenient for evaluators/teams.
ENV_SHORTCUTS = {
    "AI_MODEL": ("model", "name"),
    "AI_PROVIDER": ("model", "provider"),
    "AI_BASE_URL": ("model", "base_url"),
    "AI_TOOL_MODE": ("model", "tool_mode"),
    "AI_CONTEXT_WINDOW": ("model", "context_window"),
    "HARNESS_MAX_STEPS": ("budget", "max_steps"),
    "HARNESS_MAX_WALL_S": ("budget", "max_wall_s"),
    "HARNESS_MAX_ATTEMPTS": ("budget", "max_attempts"),
    "HARNESS_RUNS_DIR": ("run", "runs_dir"),
}


def _parse_scalar(raw: str):
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return bytes(raw[1:-1], "utf-8").decode("unicode_escape")
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        if re.fullmatch(r"[+-]?\d+", raw):
            return int(raw)
        return float(raw)
    except ValueError:
        return raw


def _mini_toml(text: str) -> dict:
    """Tiny TOML subset parser (tables + scalar keys) for Python < 3.11 without tomli."""
    data: dict = {}
    cur = data
    for line in text.splitlines():
        line = line.split(" #", 1)[0].strip() if not line.strip().startswith(("'", '"')) else line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[([A-Za-z0-9_.\-]+)\]", line)
        if m:
            cur = data
            for part in m.group(1).split("."):
                cur = cur.setdefault(part, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            cur[k.strip()] = _parse_scalar(v)
    return data


def load_toml(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import tomllib  # type: ignore

        return tomllib.loads(text)
    except ModuleNotFoundError:
        try:
            import tomli  # type: ignore

            return tomli.loads(text)
        except ModuleNotFoundError:
            return _mini_toml(text)


def _coerce(value, like):
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on") if not isinstance(value, bool) else value
    if isinstance(like, int) and not isinstance(like, bool):
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    return value if not isinstance(like, str) else str(value)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        elif k in base and not isinstance(base[k], dict):
            base[k] = _coerce(v, base[k])
        else:
            base[k] = v
    return base


@dataclass(frozen=True)
class RuntimeConfig:
    data: dict = field(default_factory=dict)
    source: str = "defaults"

    def get(self, section: str, key: str):
        return self.data[section][key]

    @property
    def model(self) -> dict:
        return self.data["model"]

    @property
    def budget(self) -> dict:
        return self.data["budget"]

    @property
    def tools(self) -> dict:
        return self.data["tools"]

    @property
    def verify(self) -> dict:
        return self.data["verify"]

    @property
    def loop(self) -> dict:
        return self.data["loop"]

    @property
    def run(self) -> dict:
        return self.data["run"]

    def runs_dir(self) -> Path:
        p = Path(self.run["runs_dir"])
        return p if p.is_absolute() else HARNESS_ROOT / p

    def redacted(self) -> dict:
        d = copy.deepcopy(self.data)
        d["api_key_present"] = bool(os.environ.get("AI_API_KEY"))
        return d


def load_config(path: str | None = None, overrides: dict | None = None) -> RuntimeConfig:
    data = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path) if path else Path(os.environ.get("HARNESS_CONFIG", DEFAULT_CONFIG_PATH))
    source = "defaults"
    if cfg_path.is_file():
        _merge(data, load_toml(cfg_path))
        source = str(cfg_path)
    for env_name, (sec, key) in ENV_SHORTCUTS.items():
        val = os.environ.get(env_name)
        if val not in (None, ""):
            data[sec][key] = _coerce(val, DEFAULTS[sec][key])
    # Generic: HARNESS__SECTION__KEY=value
    for k, v in os.environ.items():
        if k.startswith("HARNESS__"):
            parts = k.split("__")
            if len(parts) == 3:
                sec, key = parts[1].lower(), parts[2].lower()
                if sec in data and key in data[sec]:
                    data[sec][key] = _coerce(v, DEFAULTS[sec][key])
    if overrides:
        _merge(data, overrides)
    return RuntimeConfig(data=data, source=source)
