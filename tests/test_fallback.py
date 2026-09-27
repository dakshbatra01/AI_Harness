"""Credential fallback never changes the selected model or exposes key values."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from harness.provider import build_provider, detect_provider
from harness.provider.base import ModelTurn, ProviderError


class FallbackTests(unittest.TestCase):
    def test_routes_same_model_and_rotates_after_rate_limit(self):
        keys = ["gsk_test_one", "sk-or-v1-test_two", "gsk_test_three"]
        with patch.dict(os.environ, {"AI_API_KEY": json.dumps(keys)}):
            provider = build_provider({"name": "openai/gpt-oss-120b", "provider": "auto"})
        self.assertEqual([c.model for c in provider.clients], ["openai/gpt-oss-120b"] * 3)
        self.assertEqual([c.name for c in provider.clients], ["groq", "openrouter", "groq"])
        self.assertIn("openrouter.ai", provider.clients[1].url)
        with patch.object(provider.clients[0], "chat", side_effect=ProviderError("rate limited", status=429)), \
             patch.object(provider.clients[1], "chat", return_value=ModelTurn(text="ok")) as second:
            self.assertEqual(provider.chat("system", [], None).text, "ok")
        self.assertEqual(provider.current, 1)
        second.assert_called_once()

    def test_auth_rejection_is_skipped_on_later_calls(self):
        with patch.dict(os.environ, {"AI_API_KEY": json.dumps(["gsk_test_one", "gsk_test_two"])}):
            provider = build_provider({"name": "openai/gpt-oss-120b"})
        with patch.object(provider.clients[0], "chat", side_effect=ProviderError("rejected", status=401)) as first, \
             patch.object(provider.clients[1], "chat", return_value=ModelTurn(text="ok")):
            self.assertEqual(provider.chat("system", [], None).text, "ok")
            self.assertEqual(provider.chat("system", [], None).text, "ok")
        first.assert_called_once()

    def test_invalid_key_list_is_rejected_without_echo(self):
        with patch.dict(os.environ, {"AI_API_KEY": '["gsk_test_one", "unknown_secret"]'}):
            with self.assertRaises(ProviderError) as caught:
                build_provider({"name": "openai/gpt-oss-120b"})
        self.assertNotIn("unknown_secret", str(caught.exception))

    def test_single_openrouter_key_uses_openrouter_for_deepseek(self):
        with patch.dict(os.environ, {"AI_API_KEY": "sk-or-v1-local-test-key"}):
            provider = build_provider({"provider": "auto", "name": "deepseek/deepseek-v4.1-flash"})
        self.assertEqual(provider.name, "openrouter")
        self.assertEqual(provider.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(provider.url, "https://openrouter.ai/api/v1/chat/completions")

    def test_openrouter_list_uses_same_deepseek_model(self):
        keys = ["sk-or-v1-local-one", "sk-or-v1-local-two"]
        with patch.dict(os.environ, {"AI_API_KEY": json.dumps(keys)}):
            provider = build_provider({"name": "deepseek/deepseek-v4.1-flash"})
        self.assertEqual([c.model for c in provider.clients], ["deepseek/deepseek-v4.1-flash"] * 2)
        self.assertTrue(all(c.name == "openrouter" for c in provider.clients))

    def test_openrouter_requires_its_model_id(self):
        with patch.dict(os.environ, {"AI_API_KEY": "sk-or-v1-local-test-key"}):
            with self.assertRaisesRegex(ProviderError, "OpenRouter requires its model ID"):
                build_provider({"name": "deepseek-flash"})

    def test_recognizable_key_is_not_sent_to_other_endpoint(self):
        with patch.dict(os.environ, {"AI_API_KEY": "sk-or-v1-local-test-key"}):
            with self.assertRaisesRegex(ProviderError, "OpenRouter key requires"):
                build_provider({"provider": "deepseek", "name": "deepseek-flash"})

    def test_qwen_endpoint_and_model_are_explicit(self):
        with patch.dict(os.environ, {"AI_API_KEY": "sk-local-qwen-key"}):
            with self.assertRaisesRegex(ProviderError, "region-specific"):
                build_provider({"provider": "qwen", "name": "qwen-plus"})
            provider = build_provider({"provider": "qwen", "name": "qwen-plus",
                                       "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"})
        self.assertEqual(provider.name, "qwen")
        self.assertEqual(provider.url, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertEqual(detect_provider({"name": "qwen-plus", "base_url": provider.url}, "sk-local-qwen-key"), "qwen")

    def test_unknown_qwen_model_requires_model_id(self):
        with patch.dict(os.environ, {"AI_API_KEY": "sk-local-qwen-key"}):
            with self.assertRaisesRegex(ProviderError, "Set.*AI_MODEL"):
                build_provider({"provider": "qwen", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"})


if __name__ == "__main__":
    unittest.main()
