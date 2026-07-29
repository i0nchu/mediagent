"""Reddit OAuth helpers."""

from __future__ import annotations

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from mediagent.core.auth import (
    AuthSession,
    CredentialRef,
    CredentialSource,
    read_credential_json,
    write_credential_json,
)
from mediagent.core.http import UrllibHttpClient
from mediagent.core.redaction import redact_secrets


AUTHORIZE_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE_URL = "https://oauth.reddit.com"
ME_URL = f"{OAUTH_BASE_URL}/api/v1/me"
DEFAULT_SCOPES = ("identity", "history")
REDDIT_CREDENTIALS_FILE_ENV = "REDDIT_CREDENTIALS_FILE"


@dataclass(frozen=True)
class RedditConfig:
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    user_agent: str | None
    credentials_file: str | None

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "client_id_present": bool(self.client_id),
            "client_secret_present": bool(self.client_secret),
            "redirect_uri_present": bool(self.redirect_uri),
            "user_agent_present": bool(self.user_agent),
            "credentials_file": self.credentials_file,
        }


def load_config(*, env: Any) -> RedditConfig:
    return RedditConfig(
        client_id=env.get("REDDIT_CLIENT_ID") or None,
        client_secret=env.get("REDDIT_CLIENT_SECRET") or None,
        redirect_uri=env.get("REDDIT_REDIRECT_URI") or None,
        user_agent=env.get("REDDIT_USER_AGENT") or None,
        credentials_file=env.get(REDDIT_CREDENTIALS_FILE_ENV) or None,
    )


def build_authorization_start(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str | None = None,
    duration: str = "permanent",
) -> dict[str, Any]:
    state_value = state or secrets.token_urlsafe(32)
    scope_values = scopes or list(DEFAULT_SCOPES)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "state": state_value,
        "redirect_uri": redirect_uri,
        "duration": duration,
        "scope": " ".join(scope_values),
    }
    return {
        "authorization_url": f"{AUTHORIZE_URL}?{urlencode(params)}",
        "state": state_value,
        "scopes": scope_values,
        "duration": duration,
    }


def exchange_code(
    *,
    http_client: Any,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    user_agent: str,
    code: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        headers=_client_headers(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        ),
        timeout=timeout,
    )
    return _json_response(response)


def refresh_token(
    *,
    http_client: Any,
    client_id: str,
    client_secret: str | None,
    user_agent: str,
    refresh_token_value: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
        },
        headers=_client_headers(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
        ),
        timeout=timeout,
    )
    return _json_response(response)


def status_from_env(
    *,
    env: Any,
    cwd: Any,
    http_client: Any,
    required_scopes: list[str] | None = None,
    timeout: float = 30.0,
) -> tuple[AuthSession, dict[str, Any] | None, int | None]:
    config = load_config(env=env)
    credentials = load_credentials(env=env, cwd=cwd)
    access_token = credentials.get("access_token")
    refresh_token_value = credentials.get("refresh_token")
    scopes = _scopes_from_credentials(env, credentials)
    expires_at = _expires_at_from_credentials(env, credentials)
    credential_refs = _credential_refs(env)

    if not config.user_agent:
        return (
            AuthSession(
                provider="reddit",
                account_id=None,
                scopes=scopes,
                expires_at=expires_at,
                refresh_available=bool(refresh_token_value),
                status="missing_config",
                credential_refs=credential_refs,
                metadata={"missing": ["REDDIT_USER_AGENT"]},
            ),
            None,
            None,
        )

    if not access_token:
        return (
            AuthSession(
                provider="reddit",
                account_id=None,
                scopes=scopes,
                expires_at=expires_at,
                refresh_available=bool(refresh_token_value),
                status="missing_credentials",
                credential_refs=credential_refs,
            ),
            None,
            None,
        )

    client = http_client or UrllibHttpClient()
    response = client.get_json(
        ME_URL,
        headers=_bearer_headers(access_token=access_token, user_agent=config.user_agent),
        timeout=timeout,
    )
    data, rate_limit, status_code = _json_response(response)
    if status_code == 200:
        username = data.get("name")
        missing = sorted(set(required_scopes or []) - set(scopes))
        expired = _is_expired(expires_at)
        status = "expired" if expired else "missing_scopes" if missing else "usable"
        return (
            AuthSession(
                provider="reddit",
                account_id=username or data.get("id"),
                scopes=scopes,
                expires_at=expires_at,
                refresh_available=bool(refresh_token_value),
                status=status,
                credential_refs=credential_refs,
                metadata={
                    "missing_scopes": missing,
                    "expired": expired,
                    "username": username,
                    "id": data.get("id"),
                },
            ),
            rate_limit,
            status_code,
        )
    return (
        AuthSession(
            provider="reddit",
            account_id=None,
            scopes=scopes,
            expires_at=expires_at,
            refresh_available=bool(refresh_token_value),
            status="invalid",
            credential_refs=credential_refs,
            metadata={"response": redact_secrets(data), "status_code": status_code},
        ),
        rate_limit,
        status_code,
    )


def token_payload_to_session(
    payload: dict[str, Any],
    *,
    account_id: str | None = None,
) -> AuthSession:
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int):
        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    scopes = _scopes_from_payload(payload)
    return AuthSession(
        provider="reddit",
        account_id=account_id,
        scopes=scopes,
        expires_at=expires_at,
        refresh_available=bool(payload.get("refresh_token")),
        status="issued",
        metadata={"token_type": payload.get("token_type")},
    )


def load_credentials(*, env: Any, cwd: Any) -> dict[str, Any]:
    credentials: dict[str, Any] = {}
    file_value = env.get(REDDIT_CREDENTIALS_FILE_ENV)
    if file_value:
        credentials.update(read_credential_json(file_value, env=env, cwd=cwd))

    env_mapping = {
        "REDDIT_ACCESS_TOKEN": "access_token",
        "REDDIT_REFRESH_TOKEN": "refresh_token",
        "REDDIT_SCOPES": "scope",
        "REDDIT_TOKEN_EXPIRES_AT": "expires_at",
        "REDDIT_USERNAME": "username",
    }
    for env_name, key in env_mapping.items():
        value = env.get(env_name)
        if value:
            credentials[key] = value
    return credentials


def write_token_payload(
    credential_path: str,
    payload: dict[str, Any],
    *,
    env: Any,
    cwd: Any,
) -> str:
    existing = read_credential_json(credential_path, env=env, cwd=cwd)
    session = token_payload_to_session(payload)
    updates = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token") or existing.get("refresh_token"),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "expires_at": session.expires_at,
    }
    existing.update({key: value for key, value in updates.items() if value not in (None, "")})
    if session.scopes:
        existing["scopes"] = session.scopes
    path = write_credential_json(credential_path, existing, env=env, cwd=cwd)
    return str(path)


def bearer_headers(*, access_token: str, user_agent: str) -> dict[str, str]:
    return _bearer_headers(access_token=access_token, user_agent=user_agent)


def _client_headers(
    *,
    client_id: str,
    client_secret: str | None,
    user_agent: str,
) -> dict[str, str]:
    auth_value = f"{client_id}:{client_secret or ''}".encode("utf-8")
    encoded = base64.b64encode(auth_value).decode("ascii")
    return {
        "Authorization": f"Basic {encoded}",
        "User-Agent": user_agent,
    }


def _bearer_headers(*, access_token: str, user_agent: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": user_agent,
    }


def _json_response(response: Any) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": response.content.decode("utf-8", errors="replace")}
    return payload, _extract_reddit_rate_limit(response.headers), response.status_code


def _credential_refs(env: Any) -> list[CredentialRef]:
    refs = [
        CredentialRef(CredentialSource.ENV, "REDDIT_ACCESS_TOKEN"),
        CredentialRef(CredentialSource.ENV, "REDDIT_REFRESH_TOKEN"),
    ]
    file_value = env.get(REDDIT_CREDENTIALS_FILE_ENV)
    if file_value:
        refs.extend(
            [
                CredentialRef(CredentialSource.FILE, file_value, "access_token"),
                CredentialRef(CredentialSource.FILE, file_value, "refresh_token"),
            ]
        )
    return refs


def _scopes_from_credentials(env: Any, credentials: dict[str, Any]) -> list[str]:
    value = credentials.get("scope") or env.get("REDDIT_SCOPES", "")
    if value:
        return [scope for scope in str(value).replace(",", " ").split() if scope]
    scopes = credentials.get("scopes", [])
    if isinstance(scopes, list):
        return [str(scope) for scope in scopes if scope]
    return []


def _scopes_from_payload(payload: dict[str, Any]) -> list[str]:
    scope_value = payload.get("scope", "")
    if isinstance(scope_value, str):
        return [scope for scope in scope_value.replace(",", " ").split() if scope]
    scopes = payload.get("scopes", [])
    if isinstance(scopes, list):
        return [str(scope) for scope in scopes if scope]
    return []


def _expires_at_from_credentials(env: Any, credentials: dict[str, Any]) -> str | None:
    value = credentials.get("expires_at") or env.get("REDDIT_TOKEN_EXPIRES_AT")
    return str(value) if value else None


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= datetime.now(UTC)


def _extract_reddit_rate_limit(headers: dict[str, str]) -> dict[str, Any] | None:
    normalized = {key.lower(): value for key, value in headers.items()}
    if not any(
        key in normalized
        for key in ("x-ratelimit-used", "x-ratelimit-remaining", "x-ratelimit-reset")
    ):
        return None
    return {
        "used": _float_header(normalized.get("x-ratelimit-used")),
        "remaining": _float_header(normalized.get("x-ratelimit-remaining")),
        "reset_seconds": _float_header(normalized.get("x-ratelimit-reset")),
    }


def _float_header(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None

