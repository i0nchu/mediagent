from __future__ import annotations

import io
import json
import socket
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, request
from unittest.mock import patch

from mediagent import cli
from mediagent.agent.llm import OllamaClient, OpenAICompatibleClient


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        content = json.dumps(self.payload).encode("utf-8")
        return content if size < 0 else content[:size]


class LLMClientTests(unittest.TestCase):
    def test_ollama_provider_build_behavior_is_unchanged(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MEDIAGENT_LLM_PROVIDER": "ollama",
                "MEDIAGENT_OLLAMA_BASE_URL": "http://127.0.0.1:11434",
                "MEDIAGENT_OLLAMA_MODEL": "existing-model",
                "MEDIAGENT_OLLAMA_TIMEOUT_SECONDS": "17",
                "MEDIAGENT_OLLAMA_NUM_PREDICT": "321",
            },
            clear=False,
        ):
            client = cli.build_llm_client()

        self.assertIsInstance(client, OllamaClient)
        self.assertEqual(client.base_url, "http://127.0.0.1:11434")
        self.assertEqual(client.model, "existing-model")
        self.assertEqual(client.timeout, 17.0)
        self.assertEqual(client.num_predict, 321)

    def test_ollama_generate_still_uses_native_generate_endpoint(self) -> None:
        client = OllamaClient(
            base_url="http://127.0.0.1:11434",
            model="existing-model",
            timeout=17,
            num_predict=321,
        )
        with patch(
            "mediagent.agent.llm.ollama.request.urlopen",
            return_value=FakeResponse({"response": "ollama-result"}),
        ) as urlopen:
            result = client.generate("user prompt", system="system prompt")

        self.assertEqual(result, "ollama-result")
        request_value = urlopen.call_args.args[0]
        payload = json.loads(request_value.data.decode("utf-8"))
        self.assertEqual(request_value.full_url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(payload["model"], "existing-model")
        self.assertEqual(payload["prompt"], "user prompt")
        self.assertEqual(payload["system"], "system prompt")
        self.assertEqual(payload["options"]["num_predict"], 321)

    def test_openai_compatible_provider_builds_from_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "MEDIAGENT_LLM_PROVIDER": "openai_compatible",
                "MEDIAGENT_OPENAI_BASE_URL": "http://127.0.0.1:11435/v1/",
                "MEDIAGENT_OPENAI_MODEL": "qwen3-8b",
                "MEDIAGENT_OPENAI_API_KEY": "local-secret",
                "MEDIAGENT_OPENAI_TIMEOUT_SECONDS": "60",
                "MEDIAGENT_OPENAI_MAX_TOKENS": "512",
            },
            clear=False,
        ):
            client = cli.build_llm_client()

        self.assertIsInstance(client, OpenAICompatibleClient)
        self.assertEqual(client.base_url, "http://127.0.0.1:11435/v1")
        self.assertEqual(client.model, "qwen3-8b")
        self.assertEqual(client.api_key, "local-secret")
        self.assertEqual(client.timeout, 60.0)
        self.assertEqual(client.max_tokens, 512)
        self.assertNotIn("local-secret", repr(client))

    def test_agent_command_loads_openai_compatible_settings_from_env_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "MEDIAGENT_LLM_PROVIDER=openai_compatible\n"
                "MEDIAGENT_OPENAI_BASE_URL=http://127.0.0.1:11435/v1\n"
                "MEDIAGENT_OPENAI_MODEL=qwen3-8b\n"
                "MEDIAGENT_OPENAI_API_KEY=local\n"
                "MEDIAGENT_OPENAI_TIMEOUT_SECONDS=60\n"
                "MEDIAGENT_OPENAI_MAX_TOKENS=512\n",
                encoding="utf-8",
            )
            with (
                patch.dict(
                    "os.environ",
                    {"MEDIAGENT_ENV_FILE": str(env_file)},
                    clear=True,
                ),
                patch("mediagent.cli.handle_agent_skills_list", return_value=0),
            ):
                result = cli.run(["agent", "skills", "list"])
                client = cli.build_llm_client()

        self.assertEqual(result, 0)
        self.assertIsInstance(client, OpenAICompatibleClient)
        self.assertEqual(client.base_url, "http://127.0.0.1:11435/v1")
        self.assertEqual(client.model, "qwen3-8b")

    def test_unsupported_provider_is_rejected(self) -> None:
        with patch.dict("os.environ", {"MEDIAGENT_LLM_PROVIDER": "unknown"}, clear=False):
            with self.assertRaisesRegex(ValueError, "Unsupported LLM provider: unknown"):
                cli.build_llm_client()

    def test_chat_completion_uses_expected_interface_and_headers(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
            api_key="local-secret",
            timeout=12,
            max_tokens=345,
        )
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            return_value=FakeResponse({"choices": [{"message": {"content": "result"}}]}),
        ) as urlopen:
            result = client.generate("user prompt", system="system prompt")

        self.assertEqual(result, "result")
        request_value = urlopen.call_args.args[0]
        payload = json.loads(request_value.data.decode("utf-8"))
        self.assertEqual(request_value.full_url, "http://127.0.0.1:11435/v1/chat/completions")
        self.assertEqual(request_value.get_header("Authorization"), "Bearer local-secret")
        self.assertEqual(payload["model"], "qwen3-8b")
        self.assertEqual(payload["max_tokens"], 345)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)

    def test_missing_chat_endpoint_falls_back_to_completions(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
        )
        missing = error.HTTPError(
            "http://127.0.0.1:11435/v1/chat/completions",
            404,
            "not found",
            {},
            io.BytesIO(b"not found"),
        )
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            side_effect=[missing, FakeResponse({"choices": [{"text": "fallback"}]})],
        ) as urlopen:
            result = client.generate("user prompt", system="system prompt")

        self.assertEqual(result, "fallback")
        self.assertEqual(urlopen.call_count, 2)
        fallback_request = urlopen.call_args.args[0]
        fallback_payload = json.loads(fallback_request.data.decode("utf-8"))
        self.assertEqual(fallback_request.full_url, "http://127.0.0.1:11435/v1/completions")
        self.assertEqual(fallback_payload["prompt"], "system prompt\n\nuser prompt")

    def test_http_error_is_clear_and_does_not_leak_api_key(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
            api_key="super-secret",
        )
        failure = error.HTTPError(
            "http://127.0.0.1:11435/v1/chat/completions",
            500,
            "server error",
            {},
            io.BytesIO(b'{"error":{"message":"bad"}}'),
        )
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            side_effect=failure,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500") as raised:
                client.generate("prompt")

        self.assertNotIn("super-secret", str(raised.exception))

    def test_api_key_is_not_forwarded_to_redirected_endpoint(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
            api_key="super-secret",
        )
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            return_value=FakeResponse({"choices": [{"message": {"content": "result"}}]}),
        ) as urlopen:
            client.generate("prompt")

        original = urlopen.call_args.args[0]
        redirected = request.HTTPRedirectHandler().redirect_request(
            original,
            None,
            302,
            "Found",
            {},
            "https://different.example/v1/chat/completions",
        )
        self.assertEqual(original.get_header("Authorization"), "Bearer super-secret")
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_timeout_has_bounded_actionable_error(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
            timeout=7,
        )
        for timeout_error in (TimeoutError(), socket.timeout()):
            with self.subTest(exception=type(timeout_error).__name__):
                with patch(
                    "mediagent.agent.llm.openai_compatible.request.urlopen",
                    side_effect=timeout_error,
                ):
                    with self.assertRaisesRegex(RuntimeError, "timed out after 7 seconds"):
                        client.generate("prompt")
        wrapped_timeout = error.URLError(socket.timeout())
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            side_effect=wrapped_timeout,
        ):
            with self.assertRaisesRegex(RuntimeError, "timed out after 7 seconds"):
                client.generate("prompt")

    def test_invalid_response_is_reported(self) -> None:
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:11435/v1",
            model="qwen3-8b",
        )
        with patch(
            "mediagent.agent.llm.openai_compatible.request.urlopen",
            return_value=FakeResponse({"choices": []}),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not include a completion"):
                client.generate("prompt")


if __name__ == "__main__":
    unittest.main()
