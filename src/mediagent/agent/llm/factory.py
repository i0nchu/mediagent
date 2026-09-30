"""Environment-driven construction of Agent Core LLM clients."""

from __future__ import annotations

import os
from collections.abc import Mapping

from mediagent.agent.llm.ollama import OllamaClient
from mediagent.agent.llm.openai_compatible import OpenAICompatibleClient
from mediagent.agent.llm.protocol import LLMClient


def build_llm_client(env: Mapping[str, str] | None = None) -> LLMClient:
    """Build the configured backend while preserving existing defaults."""

    settings = os.environ if env is None else env
    provider = settings.get("MEDIAGENT_LLM_PROVIDER", "ollama").strip().lower()
    if provider == "ollama":
        return OllamaClient(
            base_url=settings.get("MEDIAGENT_OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
            model=settings.get("MEDIAGENT_OLLAMA_MODEL", "qwen3:8b"),
            timeout=float(settings.get("MEDIAGENT_OLLAMA_TIMEOUT_SECONDS", "60")),
            num_predict=int(settings.get("MEDIAGENT_OLLAMA_NUM_PREDICT", "512")),
        )
    if provider == "openai_compatible":
        return OpenAICompatibleClient(
            base_url=settings.get("MEDIAGENT_OPENAI_BASE_URL", "http://127.0.0.1:11435/v1"),
            model=settings.get("MEDIAGENT_OPENAI_MODEL", "qwen3-8b"),
            api_key=settings.get("MEDIAGENT_OPENAI_API_KEY", ""),
            timeout=float(settings.get("MEDIAGENT_OPENAI_TIMEOUT_SECONDS", "60")),
            max_tokens=int(settings.get("MEDIAGENT_OPENAI_MAX_TOKENS", "512")),
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")
