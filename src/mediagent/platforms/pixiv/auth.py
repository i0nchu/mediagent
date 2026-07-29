"""Pixiv App API auth helpers.

Pixiv does not expose a stable public API for this project. This adapter keeps
local OAuth/PKCE setup, explicit refresh-token fallback, and other
Pixiv-specific behavior isolated from core tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

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


LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
DEFAULT_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
DEFAULT_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
PIXIV_CREDENTIALS_FILE_ENV = "PIXIV_CREDENTIALS_FILE"


def build_authorization_start(
    *,
    code_verifier: str | None = None,
) -> dict[str, Any]:
    verifier = code_verifier or secrets.token_urlsafe(32)
    challenge = _pkce_challenge(verifier)
    params = {
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    return {
        "authorization_url": f"{LOGIN_URL}?{urlencode(params)}",
        "code_verifier": verifier,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
    }


def exchange_authorization_code(
    *,
    http_client: Any,
    code: str,
    code_verifier: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    redirect_uri: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id or DEFAULT_CLIENT_ID,
            "client_secret": client_secret or DEFAULT_CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri or REDIRECT_URI,
            "include_policy": "true",
        },
        headers=_auth_headers(),
        timeout=timeout,
    )
    return _json_response(response)


def extract_authorization_code(callback_url: str) -> str | None:
    parsed = urlparse(callback_url)
    query = parse_qs(parsed.query)
    values = query.get("code")
    if not values:
        return None
    return values[0] or None


def refresh_access_token(
    *,
    http_client: Any,
    refresh_token_value: str,
    client_id: str | None = None,
    client_secret: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token_value,
            "client_id": client_id or DEFAULT_CLIENT_ID,
            "client_secret": client_secret or DEFAULT_CLIENT_SECRET,
            "include_policy": "true",
        },
        headers=_auth_headers(),
        timeout=timeout,
    )
    return _json_response(response)


def load_credentials(*, env: Any, cwd: Any) -> dict[str, Any]:
    credentials: dict[str, Any] = {}
    file_value = env.get(PIXIV_CREDENTIALS_FILE_ENV)
    if file_value:
        credentials.update(read_credential_json(file_value, env=env, cwd=cwd))
        credentials["credential_file"] = file_value

    env_mapping = {
        "PIXIV_ACCESS_TOKEN": "access_token",
        "PIXIV_REFRESH_TOKEN": "refresh_token",
        "PIXIV_TOKEN_EXPIRES_AT": "expires_at",
        "PIXIV_USER_ID": "user_id",
        "PIXIV_SCOPES": "scope",
        "PIXIV_CLIENT_ID": "client_id",
        "PIXIV_CLIENT_SECRET": "client_secret",
    }
    for env_name, key in env_mapping.items():
        value = env.get(env_name)
        if value:
            credentials[key] = value
    return credentials


def token_payload_to_session(
    payload: dict[str, Any],
    *,
    provider: str = "pixiv",
) -> AuthSession:
    token_data = _token_data(payload)
    user = token_data.get("user") if isinstance(token_data.get("user"), dict) else {}
    expires_in = token_data.get("expires_in")
    expires_at = None
    if isinstance(expires_in, int):
        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    scope_value = token_data.get("scope", "")
    scopes = scope_value.split() if isinstance(scope_value, str) else []
    account_id = str(user.get("id")) if user.get("id") is not None else None
    return AuthSession(
        provider=provider,
        account_id=account_id,
        scopes=scopes,
        expires_at=expires_at,
        refresh_available=bool(token_data.get("refresh_token")),
        status="issued",
        metadata={
            "account": user.get("account"),
            "name": user.get("name"),
            "token_type": token_data.get("token_type"),
        },
    )


def session_from_credentials(credentials: dict[str, Any], *, status: str) -> AuthSession:
    credential_refs = _credential_refs(credentials)
    scopes = _scopes_from_credentials(credentials)
    account_id = credentials.get("user_id") or _nested_user_value(credentials, "id")
    return AuthSession(
        provider="pixiv",
        account_id=str(account_id) if account_id else None,
        scopes=scopes,
        expires_at=_expires_at_from_credentials(credentials),
        refresh_available=bool(credentials.get("refresh_token")),
        status=status,
        credential_refs=credential_refs,
        metadata={
            "account": _nested_user_value(credentials, "account"),
            "name": _nested_user_value(credentials, "name"),
        },
    )


def write_token_payload(
    credential_path: str,
    payload: dict[str, Any],
    *,
    env: Any,
    cwd: Any,
) -> str:
    existing = read_credential_json(credential_path, env=env, cwd=cwd)
    token_data = _token_data(payload)
    session = token_payload_to_session(payload)
    user = token_data.get("user") if isinstance(token_data.get("user"), dict) else {}
    updates = {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token") or existing.get("refresh_token"),
        "token_type": token_data.get("token_type"),
        "scope": token_data.get("scope"),
        "expires_at": session.expires_at,
        "user_id": str(user.get("id")) if user.get("id") is not None else existing.get("user_id"),
        "user": {
            "id": str(user.get("id")) if user.get("id") is not None else None,
            "account": user.get("account"),
            "name": user.get("name"),
        },
    }
    existing.update({key: value for key, value in updates.items() if value not in (None, "", {})})
    path = write_credential_json(credential_path, existing, env=env, cwd=cwd)
    return str(path)


def is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= datetime.now(UTC)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _auth_headers() -> dict[str, str]:
    return {
        "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; mediagent)",
        "App-OS": "android",
        "App-OS-Version": "11",
        "App-Version": "5.0.234",
    }


def _json_response(response: Any) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": response.content.decode("utf-8", errors="replace")}
    return payload, extract_rate_limit(response.headers), response.status_code


def _token_data(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("response")
    if isinstance(nested, dict):
        return nested
    return payload


def _credential_refs(credentials: dict[str, Any]) -> list[CredentialRef]:
    refs = [
        CredentialRef(CredentialSource.ENV, "PIXIV_ACCESS_TOKEN"),
        CredentialRef(CredentialSource.ENV, "PIXIV_REFRESH_TOKEN"),
    ]
    file_value = credentials.get("credential_file")
    if file_value:
        refs.extend(
            [
                CredentialRef(CredentialSource.FILE, str(file_value), "access_token"),
                CredentialRef(CredentialSource.FILE, str(file_value), "refresh_token"),
            ]
        )
    return refs


def _scopes_from_credentials(credentials: dict[str, Any]) -> list[str]:
    value = credentials.get("scope")
    if not value:
        scopes = credentials.get("scopes", [])
        if isinstance(scopes, list):
            return [str(scope) for scope in scopes if scope]
        return []
    return [scope for scope in str(value).replace(",", " ").split() if scope]


def _expires_at_from_credentials(credentials: dict[str, Any]) -> str | None:
    value = credentials.get("expires_at")
    return str(value) if value else None


def _nested_user_value(credentials: dict[str, Any], key: str) -> Any:
    user = credentials.get("user")
    if isinstance(user, dict):
        return user.get(key)
    return None
