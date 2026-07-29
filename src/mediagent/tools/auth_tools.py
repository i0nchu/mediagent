"""Generic authentication tools."""

from __future__ import annotations

from typing import Any

from mediagent.core.auth import CredentialRef, resolve_credential, resolve_credential_path
from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.platforms.pixiv import auth as pixiv_auth
from mediagent.platforms.pixiv import client as pixiv_client
from mediagent.platforms.x import auth as x_auth


CREDENTIAL_REF_SCHEMA = {
    "type": "object",
    "required": ["source", "name"],
    "properties": {
        "source": {"type": "string", "enum": ["env", "file"]},
        "name": {"type": "string"},
        "key": {"type": "string"},
    },
}


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="auth.session.status",
                description="Report whether configured credentials are present or usable.",
                input_schema={
                    "type": "object",
                    "required": ["provider"],
                    "properties": {
                        "provider": {"type": "string"},
                        "credential_refs": {
                            "type": "array",
                            "items": CREDENTIAL_REF_SCHEMA,
                        },
                        "required_scopes": {"type": "array", "items": {"type": "string"}},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=session_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="auth.session.refresh",
                description="Refresh a provider auth session through its platform adapter.",
                input_schema={
                    "type": "object",
                    "required": ["provider"],
                    "properties": {
                        "provider": {"type": "string"},
                        "client_id": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                        "refresh_token_ref": CREDENTIAL_REF_SCHEMA,
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=session_refresh,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="auth.session.revoke",
                description="Plan explicit local credential revocation guidance.",
                input_schema={
                    "type": "object",
                    "required": ["provider", "confirm"],
                    "properties": {
                        "provider": {"type": "string"},
                        "confirm": {"type": "boolean"},
                        "credential_refs": {
                            "type": "array",
                            "items": CREDENTIAL_REF_SCHEMA,
                        },
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=session_revoke,
        ),
    ]


async def session_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    provider = input_data["provider"]
    if context.dry_run:
        return ToolResult.success({"provider": provider, "would_check": True})
    if provider == "x":
        env = _env_with_x_credential_refs(context, input_data)
        session, rate_limit, status_code = x_auth.status_from_env(
            env=env,
            cwd=context.cwd,
            http_client=context.http_client,
            required_scopes=input_data.get("required_scopes"),
        )
        if session.status != "usable":
            return ToolResult.failure(
                f"{provider}_auth_{session.status}",
                "Configured auth session is not usable.",
                data={"auth_status": session.to_dict(), "status_code": status_code},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        return ToolResult.success(
            {"session": session.to_dict(), "status_code": status_code},
            rate_limit=rate_limit,
        )
    if provider == "pixiv":
        credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
        credentials = _credentials_with_pixiv_credential_refs(context, input_data, credentials)
        access_token = credentials.get("access_token")
        expires_at = credentials.get("expires_at")
        user_id = credentials.get("user_id")
        if access_token and user_id and not pixiv_auth.is_expired(str(expires_at) if expires_at else None):
            payload, rate_limit, status_code = pixiv_client.get_user_detail(
                http_client=context.http_client,
                access_token=str(access_token),
                user_id=str(user_id),
            )
            if status_code == 200:
                return ToolResult.success(
                    {
                        "provider": provider,
                        "status_code": status_code,
                        "session": pixiv_auth.session_from_credentials(credentials, status="usable").to_dict(),
                    },
                    rate_limit=rate_limit,
                )
            return ToolResult.failure(
                "pixiv_auth_invalid",
                "Configured auth session is not usable.",
                data={
                    "auth_status": pixiv_auth.session_from_credentials(credentials, status="invalid").to_dict(),
                    "status_code": status_code,
                    "response": payload,
                },
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        refresh_token_value = _refresh_token_value(context, input_data, credentials)
        if not refresh_token_value:
            session = pixiv_auth.session_from_credentials(credentials, status="missing_credentials")
            return ToolResult.failure(
                "pixiv_auth_missing_credentials",
                "PIXIV_REFRESH_TOKEN or PIXIV_CREDENTIALS_FILE is required.",
                data={"auth_status": session.to_dict()},
                category=ErrorCategory.AUTH,
            )
        payload, rate_limit, status_code = pixiv_auth.refresh_access_token(
            http_client=context.http_client,
            refresh_token_value=refresh_token_value,
            client_id=credentials.get("client_id") or context.env.get("PIXIV_CLIENT_ID"),
            client_secret=credentials.get("client_secret") or context.env.get("PIXIV_CLIENT_SECRET"),
        )
        if status_code != 200:
            return ToolResult.failure(
                "pixiv_auth_refresh_failed",
                "Pixiv refresh token is not usable.",
                data={"status_code": status_code, "response": payload},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        return ToolResult.success(
            {
                "provider": provider,
                "status_code": status_code,
                "session": pixiv_auth.token_payload_to_session(payload).to_dict(),
                "credentials_written": False,
            },
            rate_limit=rate_limit,
        )

    refs = [CredentialRef.from_dict(item) for item in input_data.get("credential_refs", [])]
    resolved = {
        ref.key or ref.name: bool(resolve_credential(ref, env=context.env, cwd=context.cwd))
        for ref in refs
    }
    missing = [name for name, present in resolved.items() if not present]
    data = {
        "provider": provider,
        "credentials": resolved,
        "missing": missing,
    }
    if missing:
        return ToolResult.failure(
            "auth_missing_credentials",
            "One or more credentials are missing.",
            data=data,
            category=ErrorCategory.AUTH,
        )
    return ToolResult.success(data)


async def session_refresh(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    provider = input_data["provider"]
    try:
        credential_output_path = _safe_credential_output_path(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure(
            "unsafe_credential_path",
            str(exc),
            category=ErrorCategory.FILESYSTEM,
        )
    if context.dry_run:
        return ToolResult.success(
            {
                "provider": provider,
                "would_refresh": True,
                "would_write_credentials": credential_output_path is not None,
            }
        )
    if provider == "pixiv":
        credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
        refresh_token_value = _refresh_token_value(context, input_data, credentials)
        if not refresh_token_value:
            return ToolResult.failure(
                "auth_missing_credentials",
                "PIXIV_REFRESH_TOKEN or refresh_token_ref is required.",
                category=ErrorCategory.AUTH,
            )
        payload, rate_limit, status_code = pixiv_auth.refresh_access_token(
            http_client=context.http_client,
            refresh_token_value=refresh_token_value,
            client_id=credentials.get("client_id") or context.env.get("PIXIV_CLIENT_ID"),
            client_secret=credentials.get("client_secret") or context.env.get("PIXIV_CLIENT_SECRET"),
        )
        if status_code != 200:
            return ToolResult.failure(
                "auth_refresh_failed",
                "Token refresh failed.",
                data={"provider": provider, "status_code": status_code, "response": payload},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        credential_file = _persist_credentials(context, credential_output_path, payload, provider=provider)
        return ToolResult.success(
            {
                "provider": provider,
                "status_code": status_code,
                "session": pixiv_auth.token_payload_to_session(payload).to_dict(),
                "credential_file": credential_file,
                "credentials_written": credential_file is not None,
            },
            rate_limit=rate_limit,
        )
    if provider != "x":
        return ToolResult.failure(
            "auth_refresh_unsupported",
            f"Refresh is not implemented for provider: {provider}",
            category=ErrorCategory.AUTH,
        )
    credentials = x_auth.load_credentials(env=context.env, cwd=context.cwd)
    client_id = input_data.get("client_id") or context.env.get("X_CLIENT_ID") or credentials.get("client_id")
    refresh_token_value = None
    if not refresh_token_value and input_data.get("refresh_token_ref"):
        refresh_token_value = resolve_credential(
            CredentialRef.from_dict(input_data["refresh_token_ref"]),
            env=context.env,
            cwd=context.cwd,
        )
    refresh_token_value = refresh_token_value or credentials.get("refresh_token")
    if not client_id or not refresh_token_value:
        return ToolResult.failure(
            "auth_missing_credentials",
            "X_CLIENT_ID and refresh token are required.",
            category=ErrorCategory.AUTH,
        )
    payload, rate_limit, status_code = x_auth.refresh_token(
        http_client=context.http_client,
        client_id=client_id,
        refresh_token_value=refresh_token_value,
    )
    if status_code != 200:
        return ToolResult.failure(
            "auth_refresh_failed",
            "Token refresh failed.",
            data={"provider": provider, "status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload, provider=provider)
    return ToolResult.success(
        {
            "provider": provider,
            "status_code": status_code,
            "session": x_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def session_revoke(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if not input_data["confirm"]:
        return ToolResult.failure(
            "revoke_not_confirmed",
            "Explicit confirmation is required before revocation guidance is returned.",
            category=ErrorCategory.PERMISSION,
        )
    refs = [CredentialRef.from_dict(item).to_dict() for item in input_data.get("credential_refs", [])]
    return ToolResult.success(
        {
            "provider": input_data["provider"],
            "revoked_remote_session": False,
            "local_action_required": "Remove or rotate the configured credentials.",
            "credential_refs": refs,
        }
    )


def _credential_output_path(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    provider = input_data.get("provider")
    if provider == "pixiv":
        return input_data.get("credential_output_path") or context.env.get(pixiv_auth.PIXIV_CREDENTIALS_FILE_ENV)
    return input_data.get("credential_output_path") or context.env.get(x_auth.X_CREDENTIALS_FILE_ENV)


def _env_with_x_credential_refs(context: ToolContext, input_data: dict[str, Any]) -> dict[str, str]:
    env = dict(context.env)
    for item in input_data.get("credential_refs", []):
        ref = CredentialRef.from_dict(item)
        value = resolve_credential(ref, env=context.env, cwd=context.cwd)
        if not value:
            continue
        key = (ref.key or ref.name).lower()
        if key in ("access_token", "x_access_token"):
            env["X_ACCESS_TOKEN"] = value
        elif key in ("refresh_token", "x_refresh_token"):
            env["X_REFRESH_TOKEN"] = value
        elif key in ("scope", "scopes", "x_scopes"):
            env["X_SCOPES"] = value
        elif key in ("expires_at", "x_token_expires_at"):
            env["X_TOKEN_EXPIRES_AT"] = value
    return env


def _credentials_with_pixiv_credential_refs(
    context: ToolContext,
    input_data: dict[str, Any],
    credentials: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(credentials)
    for item in input_data.get("credential_refs", []):
        ref = CredentialRef.from_dict(item)
        value = resolve_credential(ref, env=context.env, cwd=context.cwd)
        if not value:
            continue
        key = (ref.key or ref.name).lower()
        if key in ("access_token", "pixiv_access_token"):
            merged["access_token"] = value
        elif key in ("refresh_token", "pixiv_refresh_token"):
            merged["refresh_token"] = value
        elif key in ("scope", "scopes", "pixiv_scopes"):
            merged["scope"] = value
        elif key in ("expires_at", "pixiv_token_expires_at"):
            merged["expires_at"] = value
        elif key in ("user_id", "pixiv_user_id"):
            merged["user_id"] = value
        elif key in ("client_id", "pixiv_client_id"):
            merged["client_id"] = value
        elif key in ("client_secret", "pixiv_client_secret"):
            merged["client_secret"] = value
    return merged


def _safe_credential_output_path(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    credential_output_path = _credential_output_path(context, input_data)
    if not credential_output_path:
        return None
    path = resolve_credential_path(credential_output_path, env=context.env, cwd=context.cwd)
    ensure_inside(path, context.allowed_write_roots())
    return str(path)


def _persist_credentials(
    context: ToolContext,
    credential_output_path: str | None,
    payload: dict[str, Any],
    *,
    provider: str,
) -> str | None:
    if not credential_output_path:
        return None
    if provider == "pixiv":
        return pixiv_auth.write_token_payload(
            credential_output_path,
            payload,
            env=context.env,
            cwd=context.cwd,
        )
    return x_auth.write_token_payload(
        credential_output_path,
        payload,
        env=context.env,
        cwd=context.cwd,
    )


def _refresh_token_value(
    context: ToolContext,
    input_data: dict[str, Any],
    credentials: dict[str, Any],
) -> str | None:
    if input_data.get("refresh_token_ref"):
        return resolve_credential(
            CredentialRef.from_dict(input_data["refresh_token_ref"]),
            env=context.env,
            cwd=context.cwd,
        )
    value = credentials.get("refresh_token")
    return str(value) if value else None
