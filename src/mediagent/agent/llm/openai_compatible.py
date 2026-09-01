"""Minimal OpenAI-compatible LLM client using the standard library."""

from __future__ import annotations

import json
import math
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request
from urllib.parse import urlsplit


_FALLBACK_HTTP_STATUSES = frozenset({404, 405, 501})
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class OpenAICompatibleClient:
    """Generate text through chat completions with a completions fallback."""

    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout: float = 60.0
    max_tokens: int = 512

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MEDIAGENT_OPENAI_BASE_URL must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MEDIAGENT_OPENAI_BASE_URL must not contain credentials, query, or fragment.")
        if not self.model.strip():
            raise ValueError("MEDIAGENT_OPENAI_MODEL must not be empty.")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("MEDIAGENT_OPENAI_TIMEOUT_SECONDS must be greater than zero.")
        if self.max_tokens <= 0:
            raise ValueError("MEDIAGENT_OPENAI_MAX_TOKENS must be greater than zero.")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "api_key", self.api_key.strip())

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        chat_payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        try:
            response = self._post_json("/chat/completions", chat_payload)
        except _EndpointUnavailable:
            completion_prompt = prompt if not system else f"{system}\n\n{prompt}"
            response = self._post_json(
                "/completions",
                {
                    "model": self.model,
                    "prompt": completion_prompt,
                    "temperature": 0,
                    "max_tokens": self.max_tokens,
                    "stream": False,
                },
            )
            return _completion_text(response)
        return _chat_completion_text(response)

    def _post_json(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url + endpoint
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        if self.api_key:
            # urllib copies ordinary headers while following redirects.  Keep
            # credentials on the original request only so a redirected
            # endpoint can never receive the configured API key.
            req.add_unredirected_header("Authorization", f"Bearer {self.api_key}")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            if exc.code in _FALLBACK_HTTP_STATUSES and endpoint == "/chat/completions":
                raise _EndpointUnavailable from exc
            raise RuntimeError(
                f"OpenAI-compatible request failed with HTTP {exc.code}."
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise RuntimeError(
                f"OpenAI-compatible request timed out after {self.timeout:g} seconds."
            ) from exc
        except error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise RuntimeError(
                    f"OpenAI-compatible request timed out after {self.timeout:g} seconds."
                ) from exc
            raise RuntimeError("OpenAI-compatible request could not reach the configured service.") from exc
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("OpenAI-compatible service returned an oversized response.")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenAI-compatible service returned invalid JSON.") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError("OpenAI-compatible service returned an invalid response object.")
        return decoded


class _EndpointUnavailable(RuntimeError):
    pass


def _chat_completion_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI-compatible chat response did not include a completion.")
    choice = choices[0]
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise RuntimeError("OpenAI-compatible chat response did not include completion text.")
    return content


def _completion_text(response: dict[str, Any]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenAI-compatible response did not include a completion.")
    choice = choices[0]
    text = choice.get("text") if isinstance(choice, dict) else None
    if not isinstance(text, str) or not text:
        raise RuntimeError("OpenAI-compatible response did not include completion text.")
    return text
