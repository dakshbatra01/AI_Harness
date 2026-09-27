"""Small shared helpers: hashing, truncation, token estimates, secret redaction."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid

SECRET_ENV_NAMES = (
    "AI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(text: str, n: int = 12) -> str:
    return sha256_text(text)[:n]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def now_ms() -> int:
    return int(time.time() * 1000)


def estimate_tokens(text: str) -> int:
    # Conservative, provider-neutral estimate (~3.5 chars/token for code-heavy text).
    return int(len(text) / 3.5) + 1


def head_tail(text: str, head: int, tail: int) -> tuple[str, bool]:
    """Keep the first `head` and last `tail` characters. Returns (text, truncated)."""
    if len(text) <= head + tail:
        return text, False
    omitted = len(text) - head - tail
    return (
        text[:head]
        + f"\n\n[... {omitted} characters omitted; use read_log with this log_id to see them ...]\n\n"
        + text[-tail:],
        True,
    )


def clip_line(line: str, limit: int = 400) -> str:
    return line if len(line) <= limit else line[:limit] + f" [...{len(line) - limit} chars]"


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


class Redactor:
    """Removes known secret values from any string before it is logged or shown."""

    def __init__(self) -> None:
        self._secrets: list[str] = []
        for name in SECRET_ENV_NAMES:
            val = os.environ.get(name, "")
            if len(val) >= 8:
                self._secrets.append(val)

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 8 and secret not in self._secrets:
            self._secrets.append(secret)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for s in self._secrets:
            if s in text:
                text = text.replace(s, "[REDACTED]")
        return text

    def contains_secret(self, text: str) -> bool:
        return any(s in text for s in self._secrets)


REDACT = Redactor()


def scrubbed_env(extra: dict | None = None) -> dict:
    """Child-process environment without model credentials."""
    env = {}
    secret_values = set(REDACT._secrets)
    for k, v in os.environ.items():
        if k in SECRET_ENV_NAMES or v in secret_values:
            continue
        env[k] = v
    env.update(
        {
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "MANPAGER": "cat",
            "TQDM_DISABLE": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_NO_INPUT": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "DEBIAN_FRONTEND": "noninteractive",
            "NO_COLOR": "1",
            "TERM": "dumb",
        }
    )
    if extra:
        env.update(extra)
    return env


_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"\b0x[0-9a-f]+\b|\b\d+(\.\d+)?\b", re.I)
_PATHISH_RE = re.compile(r"/tmp/\S+|/private/\S+")


def normalize_signature(text: str, limit: int = 300) -> str:
    """Normalize an error/failure text so repeats compare equal (numbers, addresses, temp paths)."""
    t = _PATHISH_RE.sub("<tmp>", text)
    t = _NUM_RE.sub("N", t)
    t = _WS_RE.sub(" ", t).strip()
    return t[:limit]
