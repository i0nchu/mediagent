"""Rate-limit metadata helpers."""

from __future__ import annotations

from typing import Any


def extract_rate_limit(headers: dict[str, str]) -> dict[str, Any] | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    if not any(key.startswith("x-rate-limit-") for key in normalized):
        return None
    return {
        "limit": _as_int(normalized.get("x-rate-limit-limit")),
        "remaining": _as_int(normalized.get("x-rate-limit-remaining")),
        "reset_epoch": _as_int(normalized.get("x-rate-limit-reset")),
    }


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
