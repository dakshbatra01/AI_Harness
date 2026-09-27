"""Anthropic Messages API adapter (raw HTTP, no SDK dependency).

Notes that shape this adapter (Anthropic API docs, 2026):
- Newer models (Opus 4.7+, Opus 5.x, Sonnet 5, Fable) reject sampling params -> never send
  temperature to them; any other model gets it, and a 400 naming it drops it adaptively.
- Thinking blocks must be echoed back unchanged and history must stay append-only, so the
  raw assistant content is preserved in the internal message under "_raw".
- tool_result blocks must come first in a user message; parallel results go in ONE message.
"""
from __future__ import annotations

import json
import os
import re

from harness.provider.base import ContextOverflow, ModelTurn, Provider, ProviderError, ToolCall
from harness.provider.http import HTTPStatusError, post_json

NO_SAMPLING_RE = re.compile(r"claude-(opus-4-[7-9]|opus-5|sonnet-5|fable|mythos)")
EFFORT_RE = re.compile(r"claude-(opus-4-[5-9]|opus-5|sonnet-5|fable|mythos|opus-4-6|sonnet-4-6)")


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, cfg: dict, api_key: str, log=None) -> None:
        super().__init__(model, cfg)
        self.api_key = api_key
        base = (cfg.get("base_url") or "https://api.anthropic.com").rstrip("/")
        self.url = base if base.endswith("/messages") else (base + ("/messages" if base.endswith("/v1") else "/v1/messages"))
        self.log = log
        self.send_temperature = not NO_SAMPLING_RE.search(model)
        effort = str(cfg.get("effort") or "").strip()
        self.effort = effort if effort and EFFORT_RE.search(model) else ""
        self.caching = bool(cfg.get("prompt_caching", True))

    # ---- conversion -------------------------------------------------------------------
    def _convert(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []

        def push(role: str, blocks: list[dict]) -> None:
            if out and out[-1]["role"] == role:
                prev = out[-1]["content"]
                merged = prev + blocks
                if role == "user":  # tool_result blocks must lead the user turn
                    merged = [b for b in merged if b.get("type") == "tool_result"] + [
                        b for b in merged if b.get("type") != "tool_result"
                    ]
                out[-1]["content"] = merged
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            if role == "user":
                push("user", [{"type": "text", "text": m["content"] or "(empty)"}])
            elif role == "tool":
                push(
                    "user",
                    [
                        {
                            "type": "tool_result",
                            "tool_use_id": m["tool_call_id"],
                            "content": m["content"] or "(no output)",
                            **({"is_error": True} if m.get("is_error") else {}),
                        }
                    ],
                )
            elif role == "assistant":
                raw = m.get("_raw")
                if raw:
                    push("assistant", raw)
                    continue
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]})
                if not blocks:
                    blocks.append({"type": "text", "text": "(no content)"})
                push("assistant", blocks)
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(start)"}]})
        return out

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None) -> ModelTurn:
        wire_msgs = self._convert(messages)
        payload: dict = {
            "model": self.model,
            "max_tokens": int(self.cfg.get("max_output_tokens", 16000)),
            "system": [{"type": "text", "text": system}],
            "messages": wire_msgs,
        }
        if self.send_temperature:
            payload["temperature"] = float(self.cfg.get("temperature", 0.0))
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ]
        if self.caching:
            payload["system"][0]["cache_control"] = {"type": "ephemeral"}
            if payload.get("tools"):
                payload["tools"][-1]["cache_control"] = {"type": "ephemeral"}
            last = wire_msgs[-1]["content"]
            if last and isinstance(last[-1], dict) and last[-1].get("type") in ("text", "tool_result"):
                last[-1] = dict(last[-1], cache_control={"type": "ephemeral"})
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
        if os.environ.get("AI_AUTH_BEARER") == "1":
            headers = {"Authorization": f"Bearer {self.api_key}", "anthropic-version": "2023-06-01"}

        for _ in range(4):
            try:
                data = post_json(
                    self.url,
                    payload,
                    headers,
                    timeout=float(self.cfg.get("request_timeout_s", 600)),
                    max_retries=int(self.cfg.get("max_retries", 5)),
                    log=self.log,
                )
                break
            except HTTPStatusError as e:
                low = e.body.lower()
                if e.status == 400 and "temperature" in low and "temperature" in payload:
                    payload.pop("temperature", None)
                    self.send_temperature = False
                    continue
                if e.status == 400 and ("effort" in low or "output_config" in low) and "output_config" in payload:
                    payload.pop("output_config", None)
                    self.effort = ""
                    continue
                if e.status == 400 and "cache_control" in low and self.caching:
                    self.caching = False
                    return self.chat(system, messages, tools)
                if e.status in (400, 413) and any(
                    s in low for s in ("prompt is too long", "too many tokens", "context window", "exceed")
                ):
                    raise ContextOverflow(str(e), status=e.status) from None
                raise
        else:
            raise ProviderError("could not build an accepted request for this model")

        turn = ModelTurn()
        texts: list[str] = []
        raw_blocks = data.get("content") or []
        for block in raw_blocks:
            t = block.get("type")
            if t == "text":
                texts.append(block.get("text", ""))
            elif t == "tool_use":
                args = block.get("input")
                err = None
                if not isinstance(args, dict):
                    try:
                        args = json.loads(args) if isinstance(args, str) else {}
                    except (TypeError, ValueError) as ex:
                        args, err = {}, f"invalid JSON arguments: {ex}"
                turn.tool_calls.append(ToolCall(id=block.get("id", ""), name=block.get("name", ""), args=args, parse_error=err))
        turn.text = "\n".join(texts).strip()
        usage = data.get("usage") or {}
        turn.input_tokens = int(usage.get("input_tokens", 0)) + int(usage.get("cache_read_input_tokens", 0) or 0) + int(
            usage.get("cache_creation_input_tokens", 0) or 0
        )
        turn.cached_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
        turn.output_tokens = int(usage.get("output_tokens", 0))
        turn.stop_reason = data.get("stop_reason") or ""
        turn.truncated = turn.stop_reason == "max_tokens"
        turn.raw = raw_blocks
        return turn
