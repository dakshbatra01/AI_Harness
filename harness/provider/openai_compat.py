"""OpenAI Chat Completions adapter; also serves any OpenAI-compatible endpoint
(Gemini's /v1beta/openai, vLLM, Ollama, OpenRouter, LiteLLM proxy, Azure-style gateways).

Unsupported parameters are dropped adaptively when the API names them in a 400.
"""
from __future__ import annotations

import json
import re
import time

from harness.toolsyntax import loose_args, parse_function_tags
from harness.provider.base import ContextOverflow, ModelTurn, Provider, ProviderError, ToolCall, ToolsUnsupported
from harness.provider.http import HTTPStatusError, post_json

DEFAULT_BASES = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com",
}


def clean_tool_name(name: str) -> str:
    """Some models prefix tool names with a namespace ("functions.read_file", "repo_browser.run_tests")."""
    name = (name or "").strip()
    return name.rsplit(".", 1)[-1] if "." in name else name


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, model: str, cfg: dict, api_key: str, flavor: str = "openai", log=None) -> None:
        super().__init__(model, cfg)
        self.name = flavor
        self.api_key = api_key
        base = (cfg.get("base_url") or DEFAULT_BASES.get(flavor, DEFAULT_BASES["openai"])).rstrip("/")
        self.url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self.log = log
        self.params: dict = {
            "temperature": float(cfg.get("temperature", 0.0)),
            "max_tokens": int(cfg.get("max_output_tokens", 8192)),
        }
        if cfg.get("seed") not in (None, ""):
            self.params["seed"] = int(cfg["seed"])
        # Reasoning-model families on OpenAI reject temperature and max_tokens.
        low = model.lower()
        if flavor == "openai" and (low.startswith(("o1", "o3", "o4", "gpt-5")) or "reasoning" in low):
            self.params.pop("temperature", None)
            self.params["max_completion_tokens"] = self.params.pop("max_tokens")
        # Optional client-side pacing for tokens-per-minute limited endpoints (free tiers). The allowance
        # refills continuously, so we wait only as long as needed; the server's rate-limit headers keep the
        # estimate honest. Waiting a few seconds is far cheaper than a rejected request plus backoff.
        self.tpm_limit = int(cfg.get("tpm_limit", 0) or 0)
        self._available = float(self.tpm_limit)
        self._stamp = time.time()

    def _refill(self) -> None:
        now = time.time()
        self._available = min(float(self.tpm_limit), self._available + (now - self._stamp) * self.tpm_limit / 60.0)
        self._stamp = now

    def _pace(self, est_tokens: int) -> None:
        if self.tpm_limit <= 0:
            return
        est_tokens = min(est_tokens, self.tpm_limit)
        self._refill()
        if self._available >= est_tokens:
            return
        wait = (est_tokens - self._available) * 60.0 / self.tpm_limit + 0.5
        if self.log:
            self.log(f"pacing: waiting {wait:.0f}s for the tokens-per-minute allowance")
        time.sleep(wait)
        self._refill()

    def _account(self, spent: int, headers: dict | None) -> None:
        if self.tpm_limit <= 0:
            return
        self._refill()
        self._available -= spent
        rem = (headers or {}).get("x-ratelimit-remaining-tokens")
        if rem is not None:
            try:
                self._available = float(rem)
            except ValueError:
                pass

    def _salvage_tool_call(self, body: str) -> ModelTurn:
        """Turn an endpoint-side 'tool_use_failed' error into a normal turn: recover the intended call when the
        generation is parseable, otherwise return it as text so the controller asks for a valid call."""
        gen = ""
        try:
            gen = (json.loads(body).get("error") or {}).get("failed_generation") or ""
        except ValueError:
            pass
        try:
            obj = json.loads(gen)
            if isinstance(obj, dict) and obj.get("name"):
                args = obj.get("arguments") or obj.get("parameters") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                return ModelTurn(tool_calls=[ToolCall(id=f"salvaged_{int(time.time() * 1000)}", name=clean_tool_name(obj["name"]),
                                                      args=args if isinstance(args, dict) else {})], stop_reason="salvaged")
        except (ValueError, TypeError):
            pass
        tagged = parse_function_tags(gen)
        if tagged:
            return ModelTurn(tool_calls=[ToolCall(id=f"salvaged_{int(time.time() * 1000)}_{i}", name=clean_tool_name(n),
                                                  args=loose_args(p)) for i, (n, p, _) in enumerate(tagged)],
                             stop_reason="salvaged")
        return ModelTurn(text=gen or "(the model produced an invalid tool call)", stop_reason="tool_use_failed")

    @staticmethod
    def _convert(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"] or "(empty)"})
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"] or "(no output)"})
            elif m["role"] == "assistant":
                msg: dict = {"role": "assistant", "content": m.get("content") or None}
                raw = m.get("_raw")
                if isinstance(raw, dict) and raw.get("reasoning_content"):
                    msg["reasoning_content"] = raw["reasoning_content"]
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                        }
                        for tc in m["tool_calls"]
                    ]
                elif not msg["content"]:
                    msg["content"] = "(no content)"
                out.append(msg)
        return out

    def chat(self, system: str, messages: list[dict], tools: list[dict] | None) -> ModelTurn:
        payload: dict = {"model": self.model, "messages": self._convert(system, messages), **self.params}
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        est = len(json.dumps(payload)) // 4 + int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0)
        self._pace(est)
        meta: dict = {}
        data = None
        for _ in range(6):
            try:
                data = post_json(
                    self.url,
                    payload,
                    headers,
                    timeout=float(self.cfg.get("request_timeout_s", 600)),
                    max_retries=int(self.cfg.get("max_retries", 5)),
                    log=self.log,
                    meta=meta,
                )
                break
            except HTTPStatusError as e:
                low = e.body.lower()
                if e.status in (400, 413) and ("output tokens per minute" in low or "reduce max_tokens" in low):
                    # The requested max output exceeds a per-minute output allowance: shrink it and retry.
                    key = "max_tokens" if "max_tokens" in payload else "max_completion_tokens"
                    m = re.search(r"limit\D{0,12}(\d+)", low)
                    cur = int(payload.get(key) or 0)
                    new = max(256, int(int(m.group(1)) * 0.9)) if m else cur // 2
                    if cur and new < cur:
                        payload[key] = self.params[key] = new
                        if self.log:
                            self.log(f"endpoint output limit: max output tokens {cur} -> {new}")
                        continue
                if e.status in (400, 413) and any(
                    s in low for s in ("context_length", "maximum context", "too many tokens", "context length", "too long",
                                       "request too large", "tokens per minute", "reduce your message size")
                ):
                    raise ContextOverflow(str(e), status=e.status) from None
                if e.status in (400, 422):
                    changed = False
                    if "temperature" in low and "temperature" in payload:
                        payload.pop("temperature")
                        self.params.pop("temperature", None)
                        changed = True
                    if "max_tokens" in low and "max_tokens" in payload:
                        payload["max_completion_tokens"] = payload.pop("max_tokens")
                        self.params["max_completion_tokens"] = self.params.pop("max_tokens")
                        changed = True
                    if "seed" in low and "seed" in payload:
                        payload.pop("seed")
                        self.params.pop("seed", None)
                        changed = True
                    if "tool_use_failed" in low or "failed_generation" in low:
                        # The model produced a malformed tool call; recover it instead of giving up on tools.
                        turn = self._salvage_tool_call(e.body)
                        turn.input_tokens = len(json.dumps(payload)) // 4  # the provider reports no usage here
                        self._account(turn.input_tokens, None)
                        return turn
                    unsupported = ("does not support tools", "tools are not supported", "tool calling is not supported",
                                   "function calling is not supported", "unsupported parameter: 'tools'",
                                   "does not support function", "tool use is not supported")
                    if tools and any(u in low for u in unsupported) and not changed:
                        raise ToolsUnsupported(str(e), status=e.status) from None
                    if changed:
                        continue
                raise
        if data is None:
            raise ProviderError("could not build an accepted request for this model")

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"model API returned no choices: {str(data)[:500]}")
        msg = choices[0].get("message") or {}
        turn = ModelTurn(text=(msg.get("content") or "").strip())
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            err = None
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(args, dict):
                    args, err = {}, "arguments must be a JSON object"
            except ValueError as ex:
                args, err = {}, f"invalid JSON arguments: {ex}"
            turn.tool_calls.append(ToolCall(id=tc.get("id") or f"call_{len(turn.tool_calls)}", name=clean_tool_name(fn.get("name", "")),
                                            args=args, parse_error=err))
        usage = data.get("usage") or {}
        turn.input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        turn.output_tokens = int(usage.get("completion_tokens", 0) or 0)
        details = usage.get("prompt_tokens_details") or {}
        turn.cached_tokens = int(details.get("cached_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0)
        # Thinking models (e.g. DeepSeek) return their reasoning separately; with tools, the API requires it to be
        # sent back on later turns (otherwise HTTP 400). Keep it with the turn so history can replay it verbatim.
        if msg.get("reasoning_content"):
            turn.raw = {"reasoning_content": msg["reasoning_content"]}
        self._account(turn.input_tokens + int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 0),
                      meta.get("headers"))
        turn.stop_reason = choices[0].get("finish_reason") or ""
        turn.truncated = turn.stop_reason == "length"
        return turn
