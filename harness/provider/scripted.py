"""Deterministic scripted provider for tests, smoke runs and offline demos (no network).

Text mode: each response is a string. Native mode: each response is
{"text": "...", "tool_calls": [{"name": ..., "args": {...}}, ...]}."""
from __future__ import annotations

from harness.provider.base import ModelTurn, Provider, ToolCall


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, responses: list, native: bool = False) -> None:
        super().__init__("scripted", {})
        self.responses = list(responses)
        self.supports_native_tools = native
        self.calls: list[dict] = []
        self._n = 0

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None) -> ModelTurn:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        tokens_in = sum(len(str(m.get("content", ""))) for m in messages) // 4
        if not self.responses:
            if tools:
                self._n += 1
                return ModelTurn(tool_calls=[ToolCall(f"s{self._n}", "submit", {"summary": "no more scripted steps"})], input_tokens=tokens_in)
            return ModelTurn(text='<tool name="submit"><summary>no more scripted steps</summary></tool>', input_tokens=tokens_in)
        r = self.responses.pop(0)
        if isinstance(r, str):
            return ModelTurn(text=r, input_tokens=tokens_in, output_tokens=len(r) // 4)
        calls = []
        for c in r.get("tool_calls", []):
            self._n += 1
            calls.append(ToolCall(f"s{self._n}", c["name"], dict(c.get("args", {}))))
        return ModelTurn(text=r.get("text", ""), tool_calls=calls, input_tokens=tokens_in, output_tokens=50)
