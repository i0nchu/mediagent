"""Helpers for removing secrets from structured output."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = frozenset(
    {
    "token",
    "tokens",
    "cookie",
    "cookies",
    "secret",
    "secrets",
    "password",
    "passwd",
    "session",
    "refresh",
    "authorization",
    "authorization_code",
    "proxy_authorization",
    "credential",
    "credentials",
    "api_key",
    "access_key",
    "secret_key",
    "private_key",
    "key",
    }
)
SENSITIVE_KEY_SUFFIXES = (
    "_token",
    "_cookie",
    "_cookies",
    "_secret",
    "_password",
    "_passwd",
    "_authorization",
    "_credential",
    "_credentials",
    "_api_key",
)

SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|cookie|secret|password|passwd|session|refresh_token|authorization|credential|api_key|key)\b"
    r"(\s*[:=]\s*)"
    r"([^\s,;&]+)"
)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
QUOTED_SECRET_FIELD_PATTERN = re.compile(
    r"(?i)(?P<prefix>['\"]?(?:authorization|proxy-authorization|cookie|set-cookie)['\"]?\s*[:=]\s*)"
    r"(?P<quote>['\"])(?P<value>.*?)(?P=quote)"
)
AUTHORIZATION_PATTERN = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\b\s*[:=]\s*"
    r"(?:(?:bearer|basic)\s+[^\s,;}]+|[^\s,;}]+)"
)
COOKIE_PATTERN = re.compile(r"(?i)\b(cookie|set-cookie)\b\s*[:=]\s*[^,\r\n}]+")
STANDALONE_AUTH_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]{6,}")
SENSITIVE_QUERY_PARTS = (
    "token",
    "signature",
    "credential",
    "authorization",
    "password",
    "passwd",
    "secret",
    "cookie",
    "session",
    "api_key",
    "apikey",
    "access_key",
    "auth",
    "code",
)


def is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in SENSITIVE_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def redact_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    return "<redacted>"


def redact_text(value: str) -> str:
    text = URL_PATTERN.sub(lambda match: _redact_url(match.group(0)), value)
    text = QUOTED_SECRET_FIELD_PATTERN.sub(_redact_quoted_secret_field, text)
    text = AUTHORIZATION_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = COOKIE_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = STANDALONE_AUTH_PATTERN.sub(lambda match: f"{match.group(1)} <redacted>", text)
    return SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )


def _redact_quoted_secret_field(match: re.Match[str]) -> str:
    quote = match.group("quote")
    return f"{match.group('prefix')}{quote}<redacted>{quote}"


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        if not hostname:
            return "<redacted-url>"
        netloc = hostname
        if ":" in hostname and not hostname.startswith("["):
            netloc = f"[{hostname}]"
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        query = urlencode(
            [
                (key, "redacted" if _is_sensitive_query_key(key) else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ],
            doseq=True,
        )
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, query, parsed.fragment))
    except (TypeError, ValueError):
        return "<redacted-url>"


def _is_sensitive_query_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_QUERY_PARTS)


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
