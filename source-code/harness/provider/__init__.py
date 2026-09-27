"""Provider factory. Selection precedence: explicit provider > model-name hint > API-key prefix."""
from __future__ import annotations

import os
import json

from harness.provider.base import ContextOverflow, ModelTurn, Provider, ProviderError, ToolCall, ToolsUnsupported
from harness.util import REDACT

DEFAULT_MODELS = {
    # Fallbacks ONLY when config/env leave model.name empty. Set the prescribed model in
    # configuration-files/harness.toml (or AI_MODEL) for the competition.
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5",
    "gemini": "gemini-2.5-pro",
    "groq": "openai/gpt-oss-120b",
    "deepseek": "deepseek-flash",
    "openrouter": "",
    "qwen": "",
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
        if "openrouter.ai" in base:
            return "openrouter"
        if "deepseek.com" in base:
            return "deepseek"
        if "dashscope" in base or "maas.aliyuncs.com" in base:
            return "qwen"
        return "openai_compatible"
    if api_key.startswith("gsk_"):  # the key format identifies the endpoint unambiguously
        return "groq"
    if api_key.startswith("sk-or-v1-"):
        return "openrouter"
    if api_key.startswith("sk-ant-"):
        return "anthropic"
    if api_key.startswith("AIza"):
        return "gemini"
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith("deepseek"):
        return "deepseek"
    if name.startswith("qwen"):
        return "qwen"
    if name.startswith("gemini"):
        return "gemini"
    if name.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
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
        kinds = {"groq" if key.startswith("gsk_") else "openrouter" if key.startswith("sk-or-v1-") else "unknown"
                 for key in keys}
        if "unknown" in kinds:
            raise ProviderError("AI_API_KEY list contains an unsupported key format; use one key with AI_PROVIDER and AI_BASE_URL for other endpoints", fatal=True)
        model = model_cfg.get("name") or (DEFAULT_MODELS["groq"] if "groq" in kinds else "")
        if not model:
            raise ProviderError("Set AI_MODEL to the OpenRouter model ID when using an OpenRouter key list", fatal=True)
        if "openrouter" in kinds and model.startswith("deepseek-"):
            raise ProviderError("OpenRouter requires its model ID (for example deepseek/deepseek-v4.1-flash), not a direct DeepSeek API ID", fatal=True)
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
                kind = "openrouter"
            clients.append(OpenAICompatProvider(model, cfg, key, flavor=kind, log=log))
        return FallbackProvider(model, model_cfg, clients)
    REDACT.add(api_key)
    kind = detect_provider(model_cfg, api_key)
    if api_key.startswith("sk-or-v1-") and kind != "openrouter":
        raise ProviderError("OpenRouter key requires AI_PROVIDER=openrouter (or provider=auto without a conflicting AI_BASE_URL)", fatal=True)
    if api_key.startswith("gsk_") and kind != "groq":
        raise ProviderError("Groq key requires AI_PROVIDER=groq (or provider=auto without a conflicting AI_BASE_URL)", fatal=True)
    model = model_cfg.get("name") or DEFAULT_MODELS.get(kind, "")
    if not model:
        raise ProviderError(
            f"No model configured for provider '{kind}'. Set [model].name in configuration-files/harness.toml or AI_MODEL.", fatal=True
        )
    if not model_cfg.get("name") and log:
        log(f"WARNING: [model].name is empty; defaulting to '{model}'. Set the prescribed model in configuration-files/harness.toml.")
    if kind == "qwen" and not model_cfg.get("base_url"):
        raise ProviderError("Qwen API keys are region-specific; set AI_BASE_URL to the key's OpenAI-compatible endpoint", fatal=True)
    if kind == "openrouter" and model.startswith("deepseek-"):
        raise ProviderError("OpenRouter requires its model ID (for example deepseek/deepseek-v4.1-flash), not a direct DeepSeek API ID", fatal=True)
    if kind == "anthropic":
        from harness.provider.anthropic import AnthropicProvider

        return AnthropicProvider(model, model_cfg, api_key, log=log)
    if kind in ("openai", "gemini", "groq", "deepseek", "openrouter", "qwen", "openai_compatible"):
        from harness.provider.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(model, model_cfg, api_key, flavor=kind, log=log)
    raise ProviderError(f"Unknown provider '{kind}' (use anthropic | openai | gemini | groq | deepseek | openrouter | qwen | openai_compatible)", fatal=True)


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
