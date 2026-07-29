"""Helpers for removing secrets from structured output."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


SENSITIVE_KEY_PARTS = (
    "token",
    "cookie",
    "secret",
    "password",
    "passwd",
    "session",
    "refresh",
    "authorization",
    "credential",
    "key",
)

SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|cookie|secret|password|passwd|session|refresh_token|authorization|credential|api_key|key)\b"
    r"(\s*[:=]\s*)"
    r"([^\s,;&]+)"
)


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    return "<redacted>"


def redact_text(value: str) -> str:
    return SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        value,
    )


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: redact_value(value) if is_sensitive_key(key) else value
        for key, value in values.items()
    }


def redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: redact_value(item) if is_sensitive_key(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
