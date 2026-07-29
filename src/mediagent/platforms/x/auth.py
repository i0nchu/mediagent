"""X OAuth 2.0 PKCE helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
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
from mediagent.core.rate_limit import extract_rate_limit
from mediagent.core.redaction import redact_secrets


AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
ME_URL = "https://api.x.com/2/users/me"
DEFAULT_SCOPES = ("tweet.read", "users.read", "bookmark.read", "offline.access")
X_CREDENTIALS_FILE_ENV = "X_CREDENTIALS_FILE"


def build_authorization_start(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list[str] | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(verifier)
    state_value = state or secrets.token_urlsafe(32)
    scope_values = scopes or list(DEFAULT_SCOPES)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scope_values),
        "state": state_value,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return {
        "authorization_url": f"{AUTHORIZE_URL}?{urlencode(params)}",
        "state": state_value,
        "code_verifier": verifier,
        "code_challenge": challenge,
        "scopes": scope_values,
    }


def exchange_code(
    *,
    http_client: Any,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": code,
            "code_verifier": code_verifier,
        },
        timeout=timeout,
    )
    return _json_response(response)


def refresh_token(
    *,
    http_client: Any,
    client_id: str,
    refresh_token_value: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token_value,
        },
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
    credentials = load_credentials(env=env, cwd=cwd)
    token_ref = CredentialRef(CredentialSource.ENV, "X_ACCESS_TOKEN")
    refresh_ref = CredentialRef(CredentialSource.ENV, "X_REFRESH_TOKEN")
    credential_refs = _credential_refs(env)
    access_token = credentials.get("access_token")
    refresh_token_value = credentials.get("refresh_token")
    scopes = _scopes_from_credentials(env, credentials)
    expires_at = _expires_at_from_credentials(env, credentials)
    if not access_token:
        return (
            AuthSession(
                provider="x",
                account_id=None,
                scopes=[],
                expires_at=None,
                refresh_available=bool(refresh_token_value),
                status="missing_credentials",
                credential_refs=credential_refs or [token_ref, refresh_ref],
            ),
            None,
            None,
        )

    client = http_client or UrllibHttpClient()
    response = client.get_json(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    data, rate_limit, status_code = _json_response(response)
    if status_code == 200:
        account_id = data.get("data", {}).get("id")
        missing = sorted(set(required_scopes or []) - set(scopes))
        expired = _is_expired(expires_at)
        status = "expired" if expired else "missing_scopes" if missing else "usable"
        return (
            AuthSession(
                provider="x",
                account_id=account_id,
                scopes=scopes,
                expires_at=expires_at,
                refresh_available=bool(refresh_token_value),
                status=status,
                credential_refs=credential_refs or [token_ref, refresh_ref],
                metadata={"missing_scopes": missing, "expired": expired},
            ),
            rate_limit,
            status_code,
        )
    return (
        AuthSession(
            provider="x",
            account_id=None,
            scopes=scopes,
            expires_at=expires_at,
            refresh_available=bool(refresh_token_value),
            status="invalid",
            credential_refs=credential_refs or [token_ref, refresh_ref],
            metadata={"response": redact_secrets(data), "status_code": status_code},
        ),
        rate_limit,
        status_code,
    )


def token_payload_to_session(
    payload: dict[str, Any],
    *,
    provider: str = "x",
    account_id: str | None = None,
) -> AuthSession:
    expires_in = payload.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int):
        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    scope_value = payload.get("scope", "")
    scopes = scope_value.split() if isinstance(scope_value, str) else []
    return AuthSession(
        provider=provider,
        account_id=account_id,
        scopes=scopes,
        expires_at=expires_at,
        refresh_available=bool(payload.get("refresh_token")),
        status="issued",
        metadata={
            "token_type": payload.get("token_type"),
        },
    )


def load_credentials(*, env: Any, cwd: Any) -> dict[str, Any]:
    credentials: dict[str, Any] = {}
    file_value = env.get(X_CREDENTIALS_FILE_ENV)
    if file_value:
        credentials.update(read_credential_json(file_value, env=env, cwd=cwd))

    env_mapping = {
        "X_ACCESS_TOKEN": "access_token",
        "X_REFRESH_TOKEN": "refresh_token",
        "X_SCOPES": "scope",
        "X_TOKEN_EXPIRES_AT": "expires_at",
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
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "expires_at": session.expires_at,
    }
    existing.update({key: value for key, value in updates.items() if value not in (None, "")})
    scopes = session.scopes
    if scopes:
        existing["scopes"] = scopes
    path = write_credential_json(credential_path, existing, env=env, cwd=cwd)
    return str(path)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _json_response(response: Any) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": response.content.decode("utf-8", errors="replace")}
    return payload, extract_rate_limit(response.headers), response.status_code


def _credential_refs(env: Any) -> list[CredentialRef]:
    refs = [
        CredentialRef(CredentialSource.ENV, "X_ACCESS_TOKEN"),
        CredentialRef(CredentialSource.ENV, "X_REFRESH_TOKEN"),
    ]
    file_value = env.get(X_CREDENTIALS_FILE_ENV)
    if file_value:
        refs.extend(
            [
                CredentialRef(CredentialSource.FILE, file_value, "access_token"),
                CredentialRef(CredentialSource.FILE, file_value, "refresh_token"),
            ]
        )
    return refs


def _scopes_from_credentials(env: Any, credentials: dict[str, Any]) -> list[str]:
    value = credentials.get("scope") or env.get("X_SCOPES", "")
    if not value:
        scopes = credentials.get("scopes", [])
        if isinstance(scopes, list):
            return [str(scope) for scope in scopes if scope]
        return []
    return [scope for scope in value.replace(",", " ").split() if scope]


def _expires_at_from_credentials(env: Any, credentials: dict[str, Any]) -> str | None:
    value = credentials.get("expires_at") or env.get("X_TOKEN_EXPIRES_AT")
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
