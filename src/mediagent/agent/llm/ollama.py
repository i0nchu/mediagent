"""Minimal Ollama client using the standard library."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request


@dataclass(frozen=True)
class OllamaClient:
    base_url: str
    model: str
    timeout: float = 60.0
    num_predict: int = 512

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": self.num_predict,
            },
        }
        if system:
            payload["system"] = system
        url = self.base_url.rstrip("/") + "/api/generate"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (TimeoutError, error.URLError) as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        return str(data.get("response") or "")
