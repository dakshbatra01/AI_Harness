"""Provider adapters over real HTTP against a local mock server (no network, no API key):
payload shape, auth headers, retries on 429/5xx, adaptive dropping of rejected params, and a full
harness run driven through `make run`-equivalent code paths (build_provider from env)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness.config import load_config
from harness.provider import build_provider
from harness.provider.anthropic import AnthropicProvider
from harness.provider.openai_compat import OpenAICompatProvider


class Mock:
    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.script: list = []  # items: (status, body_dict)

    def start(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                mock.requests.append(body)
                mock.headers.append({k.lower(): v for k, v in self.headers.items()})
                status, resp = mock.script.pop(0) if mock.script else (500, {"error": "script exhausted"})
                data = json.dumps(resp).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 429:
                    self.send_header("retry-after", "0")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def oai_msg(content=None, tool_calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


class OpenAIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.mock = Mock()
        self.base = self.mock.start() + "/v1"

    def tearDown(self):
        self.mock.stop()

    def test_retry_and_param_adaptation(self):
        self.mock.script = [
            (429, {"error": {"message": "rate limited"}}),
            (400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported with this model."}}),
            (200, oai_msg(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}])),
        ]
        p = OpenAICompatProvider("my-model", {"base_url": self.base, "max_retries": 2, "temperature": 0}, "KEY", flavor="openai_compatible")
        turn = p.chat("sys", [{"role": "user", "content": "hi"}], [{"name": "read_file", "description": "d", "parameters": {"type": "object", "properties": {}}}])
        self.assertEqual(turn.tool_calls[0].args, {"path": "a.py"})
        self.assertNotIn("temperature", self.mock.requests[-1])
        self.assertEqual(self.mock.headers[-1]["authorization"], "Bearer KEY")
        self.assertEqual(self.mock.requests[-1]["messages"][0], {"role": "system", "content": "sys"})

    def test_rate_limit_with_billing_link_is_retried(self):
        msg = ("Rate limit reached on input tokens per minute (ITPM). Please try again in 4.98s. Need more tokens? "
               "Upgrade to Dev Tier today at https://console.groq.com/settings/billing")
        self.mock.script = [(429, {"error": {"message": msg}}), (200, oai_msg(content="ok"))]
        p = OpenAICompatProvider("m", {"base_url": self.base, "max_retries": 2}, "K", flavor="openai_compatible")
        self.assertEqual(p.chat("s", [{"role": "user", "content": "u"}], None).text, "ok")

    def test_real_quota_error_is_not_retried(self):
        from harness.provider.base import ProviderError
        self.mock.script = [(429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}),
                            (200, oai_msg(content="never"))]
        p = OpenAICompatProvider("m", {"base_url": self.base, "max_retries": 2}, "K", flavor="openai_compatible")
        with self.assertRaises(ProviderError):
            p.chat("s", [{"role": "user", "content": "u"}], None)

    def test_output_limit_shrinks_max_tokens(self):
        msg = "Request too large on output tokens per minute (OTPM): Limit 1000, Requested 1514. reduce max_tokens"
        self.mock.script = [(413, {"error": {"message": msg}}), (200, oai_msg(content="ok"))]
        p = OpenAICompatProvider("m", {"base_url": self.base, "max_output_tokens": 1500}, "K", flavor="groq")
        self.assertEqual(p.chat("s", [{"role": "user", "content": "u"}], None).text, "ok")
        sent = self.mock.requests[-1].get("max_tokens") or self.mock.requests[-1].get("max_completion_tokens")
        self.assertEqual(sent, 900)

    def test_function_tag_generation_is_salvaged(self):
        gen = "<tool_call>\n<function=run_tests>\n<parameter=tests>\n[\"tests/test_slug.py\"]\n</parameter>\n</function>"
        body = {"error": {"code": "tool_use_failed", "message": "Failed to parse tool call", "failed_generation": gen}}
        self.mock.script = [(400, body)]
        p = OpenAICompatProvider("m", {"base_url": self.base}, "K", flavor="groq")
        tools = [{"name": "run_tests", "description": "d", "parameters": {"type": "object", "properties": {}}}]
        turn = p.chat("s", [{"role": "user", "content": "u"}], tools)
        self.assertEqual((turn.tool_calls[0].name, turn.tool_calls[0].args), ("run_tests", {"tests": ["tests/test_slug.py"]}))
        self.assertGreater(turn.input_tokens, 0)

    def test_reasoning_content_is_replayed_with_tool_calls(self):
        """DeepSeek thinking mode + tools: earlier reasoning_content must be sent back (else HTTP 400)."""
        from harness.config import load_config
        from harness.controller.controller import Controller
        from harness.telemetry import Telemetry
        from tests.fixtures.fixture_repo import make_fixture_repo

        first = oai_msg(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "README.md"}'}}])
        first["choices"][0]["message"]["reasoning_content"] = "I should look at the README first."
        first["usage"]["prompt_cache_hit_tokens"] = 64
        self.mock.script = [(200, first), (200, oai_msg(content="Nothing to change."))]
        with tempfile.TemporaryDirectory() as td:
            repo, issue = make_fixture_repo(Path(td) / "repo")
            cfg = load_config(None, {"run": {"runs_dir": str(Path(td) / "runs")}, "budget": {"max_attempts": 1, "max_model_calls": 2},
                                     "model": {"base_url": "https://api.deepseek.com"}})
            p = OpenAICompatProvider("deepseek-flash", {"base_url": self.base}, "K", flavor="deepseek")
            Controller(cfg, repo, issue, Path(td) / "runs" / "x", p, Telemetry(None, verbose=False)).run()
        second = self.mock.requests[1]["messages"]
        assistant = [m for m in second if m["role"] == "assistant"][0]
        self.assertEqual(assistant.get("reasoning_content"), "I should look at the README first.")

    def test_deepseek_is_detected(self):
        from harness.provider import detect_provider
        self.assertEqual(detect_provider({"name": "deepseek-flash"}, "sk-x"), "deepseek")
        self.assertEqual(detect_provider({"name": "x", "base_url": "https://api.deepseek.com"}, "sk-x"), "deepseek")

    def test_malformed_arguments_reported(self):
        self.mock.script = [(200, oai_msg(tool_calls=[{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{bad"}}]))]
        p = OpenAICompatProvider("m", {"base_url": self.base}, "K", flavor="openai_compatible")
        turn = p.chat("s", [{"role": "user", "content": "u"}], None)
        self.assertIsNotNone(turn.tool_calls[0].parse_error)

    def test_full_run_through_http_text_fallback(self):
        """Endpoint rejects tools -> harness switches to the text protocol and still verifies the fix."""
        from harness.controller.controller import Controller
        from harness.telemetry import Telemetry
        from tests.fixtures.fixture_repo import SCRIPTED_SOLUTION, make_fixture_repo

        self.mock.script = [(400, {"error": {"message": "tools are not supported by this model"}})]
        self.mock.script += [(200, oai_msg(content=t)) for t in SCRIPTED_SOLUTION]
        with tempfile.TemporaryDirectory() as td:
            repo, issue = make_fixture_repo(Path(td) / "repo")
            os.environ["AI_API_KEY"] = "sk-local-test-key-123456"
            try:
                cfg = load_config(None, {"model": {"name": "local-model", "base_url": self.base, "max_retries": 0},
                                         "run": {"runs_dir": str(Path(td) / "runs")}})
                provider = build_provider(cfg.model)
                tel = Telemetry(cfg.runs_dir() / "r", verbose=False)
                res = Controller(cfg, repo, issue, cfg.runs_dir() / "r", provider, tel).run()
                tel.close()
            finally:
                os.environ.pop("AI_API_KEY", None)
            self.assertEqual(res.status, "VERIFIED", res.summary)
            self.assertEqual(res.metrics["tool_mode"], "text")
            self.assertIn("tools", self.mock.requests[0])
            self.assertNotIn("tools", self.mock.requests[1])


class AnthropicHTTPTests(unittest.TestCase):
    def setUp(self):
        self.mock = Mock()
        self.base = self.mock.start()

    def tearDown(self):
        self.mock.stop()

    def test_payload_caching_and_raw_blocks(self):
        raw = [{"type": "thinking", "thinking": "", "signature": "sig"},
               {"type": "text", "text": "reading"},
               {"type": "tool_use", "id": "tu1", "name": "read_file", "input": {"path": "a.py"}}]
        self.mock.script = [(200, {"content": raw, "stop_reason": "tool_use",
                                   "usage": {"input_tokens": 50, "output_tokens": 5, "cache_read_input_tokens": 40}})]
        p = AnthropicProvider("claude-opus-5-5", {"base_url": self.base, "max_output_tokens": 1000, "effort": "high"}, "KEY")
        tools = [{"name": "read_file", "description": "d", "parameters": {"type": "object", "properties": {}}}]
        turn = p.chat("sys", [{"role": "user", "content": "hi"}], tools)
        req = self.mock.requests[0]
        self.assertNotIn("temperature", req)
        self.assertEqual(req["output_config"], {"effort": "high"})
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(req["tools"][0]["input_schema"], {"type": "object", "properties": {}})
        self.assertEqual(self.mock.headers[0]["x-api-key"], "KEY")
        self.assertEqual(turn.tool_calls[0].args, {"path": "a.py"})
        self.assertEqual(turn.raw, raw)
        self.assertEqual(turn.cached_tokens, 40)
        # Echo back: the raw assistant content (incl. thinking) must be replayed verbatim.
        wire = p._convert([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "reading", "_raw": raw,
                            "tool_calls": [{"id": "tu1", "name": "read_file", "args": {"path": "a.py"}}]},
                           {"role": "tool", "tool_call_id": "tu1", "content": "file"}])
        self.assertEqual(wire[1]["content"], raw)

    def test_rejected_effort_is_dropped(self):
        self.mock.script = [
            (400, {"error": {"message": "output_config.effort: not supported for this model"}}),
            (200, {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
        p = AnthropicProvider("claude-sonnet-5", {"base_url": self.base, "effort": "high"}, "KEY")
        turn = p.chat("s", [{"role": "user", "content": "u"}], None)
        self.assertEqual(turn.text, "ok")
        self.assertNotIn("output_config", self.mock.requests[-1])


if __name__ == "__main__":
    unittest.main()
