"""Provider factory. Selection precedence: explicit provider > model-name hint > API-key prefix."""
from __future__ import annotations

import os
import json

from harness.provider.base import ContextOverflow, ModelTurn, Provider, ProviderError, ToolCall, ToolsUnsupported
from harness.util import REDACT

DEFAULT_MODELS = {
    # Fallbacks ONLY when config/env leave model.name empty. Set the prescribed model in
    # config/harness.toml (or AI_MODEL) for the competition.
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5",
    "gemini": "gemini-2.5-pro",
    "groq": "openai/gpt-oss-120b",
    "deepseek": "deepseek-flash",
    "openai_compatible": "",
}


def detect_provider(cfg: dict, api_key: str) -> str:
    p = (cfg.get("provider") or "auto").lower()
    if p != "auto":
        return p
    name = (cfg.get("name") or "").lower()
    base = (cfg.get("base_url") or "").lower()
    if base:
        if "anthropic" in base:
            return "anthropic"
        if "generativelanguage" in base:
            return "gemini"
        if "groq.com" in base:
            return "groq"
        if "deepseek.com" in base:
            return "deepseek"
        return "openai_compatible"
    if api_key.startswith("gsk_"):  # the key format identifies the endpoint unambiguously
        return "groq"
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIza"):
        return "gemini"
    return "openai"


def build_provider(model_cfg: dict, log=None) -> Provider:
    api_key = os.environ.get("AI_API_KEY", "")
    if not api_key:
        raise ProviderError("AI_API_KEY is not set. Export it before `make run`.", fatal=True)
    if api_key.lstrip().startswith("["):
        try:
            keys = json.loads(api_key)
        except ValueError:
            raise ProviderError("AI_API_KEY key list must be valid JSON", fatal=True) from None
        if not isinstance(keys, list) or not 1 <= len(keys) <= 16 or not all(isinstance(k, str) and k.strip() for k in keys):
            raise ProviderError("AI_API_KEY must contain a nonempty list of up to 16 keys", fatal=True)
        model = model_cfg.get("name") or DEFAULT_MODELS["groq"]
        from harness.provider.openai_compat import OpenAICompatProvider
        from harness.provider.fallback import FallbackProvider

        clients = []
        for key in keys:
            REDACT.add(key)
            cfg = dict(model_cfg)
            cfg["max_retries"] = 0  # move to the next credential instead of sleeping inside one request
            cfg["tpm_limit"] = 0
            if key.startswith("gsk_"):
                cfg["base_url"] = "https://api.groq.com/openai/v1"
                kind = "groq"
            elif key.startswith("sk-or-v1-"):
                cfg["base_url"] = "https://openrouter.ai/api/v1"
                kind = "openai_compatible"
            else:
                raise ProviderError("AI_API_KEY list contains an unsupported key format", fatal=True)
            clients.append(OpenAICompatProvider(model, cfg, key, flavor=kind, log=log))
        return FallbackProvider(model, model_cfg, clients)
    kind = detect_provider(model_cfg, api_key)
    model = model_cfg.get("name") or DEFAULT_MODELS.get(kind, "")
    if not model:
        raise ProviderError(
            f"No model configured for provider '{kind}'. Set [model].name in config/harness.toml or AI_MODEL.", fatal=True
        )
    if not model_cfg.get("name") and log:
        log(f"WARNING: [model].name is empty; defaulting to '{model}'. Set the prescribed model in config/harness.toml.")
    if kind == "anthropic":
        from harness.provider.anthropic import AnthropicProvider

        return AnthropicProvider(model, model_cfg, api_key, log=log)
    if kind in ("openai", "gemini", "groq", "deepseek", "openai_compatible"):
        from harness.provider.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(model, model_cfg, api_key, flavor=kind, log=log)
    raise ProviderError(f"Unknown provider '{kind}' (use anthropic | openai | gemini | groq | openai_compatible)", fatal=True)


__all__ = [
    "Provider",
    "ProviderError",
    "ToolsUnsupported",
    "ContextOverflow",
    "ModelTurn",
    "ToolCall",
    "build_provider",
    "detect_provider",
]
