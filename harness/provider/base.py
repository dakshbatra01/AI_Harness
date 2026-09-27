"""Provider-neutral model interface.

Internal message format (provider adapters convert to/from wire formats):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCall-as-dict, ...]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ProviderError(Exception):
    def __init__(self, message: str, *, fatal: bool = False, status: int | None = None) -> None:
        super().__init__(message)
        self.fatal = fatal
        self.status = status


class ToolsUnsupported(ProviderError):
    pass


class ContextOverflow(ProviderError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "args": self.args}


@dataclass
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    stop_reason: str = ""
    truncated: bool = False
    raw: list | dict | None = None  # provider-native assistant content to echo back verbatim


class Provider:
    name = "base"
    supports_native_tools = True

    def __init__(self, model: str, cfg: dict) -> None:
        self.model = model
        self.cfg = cfg

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None) -> ModelTurn:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name}:{self.model}"
