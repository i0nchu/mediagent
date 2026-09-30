"""Shared interface implemented by Agent Core LLM backends."""

from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        """Return one complete generated response."""

        ...
