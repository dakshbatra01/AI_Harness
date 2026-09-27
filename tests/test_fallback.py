"""Credential fallback never changes the selected model or exposes key values."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from harness.provider import build_provider
from harness.provider.base import ModelTurn, ProviderError


class FallbackTests(unittest.TestCase):
    def test_routes_same_model_and_rotates_after_rate_limit(self):
        keys = ["gsk_test_one", "sk-or-v1-test_two", "gsk_test_three"]
        with patch.dict(os.environ, {"AI_API_KEY": json.dumps(keys)}):
            provider = build_provider({"name": "openai/gpt-oss-120b", "provider": "auto"})
        self.assertEqual([c.model for c in provider.clients], ["openai/gpt-oss-120b"] * 3)
        self.assertEqual([c.name for c in provider.clients], ["groq", "openai_compatible", "groq"])
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


if __name__ == "__main__":
    unittest.main()
