"""X platform tools."""

from __future__ import annotations

from typing import Any

from mediagent.core import db
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
from mediagent.platforms.x import auth as x_auth
from mediagent.platforms.x import client as x_client
from mediagent.platforms.x import parser as x_parser


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="x.auth.start",
                description="Generate an X OAuth 2.0 PKCE authorization URL.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "client_id": {"type": "string"},
                        "redirect_uri": {"type": "string"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                        "state": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(),
                dry_run_supported=True,
            ),
            handler=auth_start,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="x.auth.exchange",
                description="Exchange an X OAuth authorization code for tokens.",
                input_schema={
                    "type": "object",
                    "required": ["code", "code_verifier"],
                    "properties": {
                        "client_id": {"type": "string"},
                        "redirect_uri": {"type": "string"},
                        "code": {"type": "string"},
                        "code_verifier": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_exchange,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="x.auth.refresh",
                description="Refresh X OAuth tokens.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "client_id": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                        "refresh_token_ref": {
                            "type": "object",
                            "required": ["source", "name"],
                            "properties": {
                                "source": {"type": "string", "enum": ["env", "file"]},
                                "name": {"type": "string"},
                                "key": {"type": "string"},
                            },
                        },
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_refresh,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="x.auth.status",
                description="Validate X token presence, scopes, and authenticated user.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "required_scopes": {"type": "array", "items": {"type": "string"}},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="x.bookmarks.collect",
                description="Collect media-bearing X bookmarks for the authenticated user.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "user_id": {"type": "string"},
                        "pagination_token": {"type": "string"},
                        "max_results": {"type": "integer"},
                        "store_cursor": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_DB),
                dry_run_supported=True,
            ),
            handler=bookmarks_collect,
        ),
    ]


async def auth_start(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    client_id = input_data.get("client_id") or context.env.get("X_CLIENT_ID")
    redirect_uri = input_data.get("redirect_uri") or context.env.get("X_REDIRECT_URI")
    if not client_id or not redirect_uri:
        return ToolResult.failure(
            "auth_missing_config",
            "X_CLIENT_ID and X_REDIRECT_URI are required.",
            category=ErrorCategory.VALIDATION,
        )
    result = x_auth.build_authorization_start(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=input_data.get("scopes"),
        state=input_data.get("state"),
    )
    return ToolResult.success(result)


async def auth_exchange(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        credential_output_path = _safe_credential_output_path(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure(
            "unsafe_credential_path",
            str(exc),
            category=ErrorCategory.FILESYSTEM,
        )
    client_id = input_data.get("client_id") or context.env.get("X_CLIENT_ID")
    redirect_uri = input_data.get("redirect_uri") or context.env.get("X_REDIRECT_URI")
    if not client_id or not redirect_uri:
        return ToolResult.failure(
            "auth_missing_config",
            "X_CLIENT_ID and X_REDIRECT_URI are required.",
            category=ErrorCategory.VALIDATION,
        )
    if context.dry_run:
        return ToolResult.success(
            {
                "would_exchange": True,
                "would_write_credentials": credential_output_path is not None,
            }
        )
    payload, rate_limit, status_code = x_auth.exchange_code(
        http_client=context.http_client,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code=input_data["code"],
        code_verifier=input_data["code_verifier"],
    )
    if status_code != 200:
        return ToolResult.failure(
            "x_auth_exchange_failed",
            "X token exchange failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "status_code": status_code,
            "session": x_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def auth_refresh(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
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
                "would_refresh": True,
                "would_write_credentials": credential_output_path is not None,
            }
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
    if not refresh_token_value:
        refresh_token_value = credentials.get("refresh_token")
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
            "x_auth_refresh_failed",
            "X token refresh failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "status_code": status_code,
            "session": x_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_check": True})
    session, rate_limit, status_code = x_auth.status_from_env(
        env=context.env,
        cwd=context.cwd,
        http_client=context.http_client,
        required_scopes=input_data.get("required_scopes"),
    )
    if session.status != "usable":
        return ToolResult.failure(
            f"x_auth_{session.status}",
            "X auth session is not usable.",
            data={"status_code": status_code, "auth_status": session.to_dict()},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    return ToolResult.success(
        {"status_code": status_code, "session": session.to_dict()},
        rate_limit=rate_limit,
    )


async def bookmarks_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_collect": True, "platform": "x"})
    credentials = x_auth.load_credentials(env=context.env, cwd=context.cwd)
    access_token = credentials.get("access_token")
    if not access_token:
        return ToolResult.failure(
            "auth_missing_credentials",
            "X access token is required.",
            category=ErrorCategory.AUTH,
        )
    user_id = input_data.get("user_id")
    rate_limit = None
    if not user_id:
        me, rate_limit, status_code = x_client.get_me(
            http_client=context.http_client,
            access_token=access_token,
        )
        if status_code != 200:
            return ToolResult.failure(
                "x_me_failed",
                "Could not resolve authenticated X user.",
                data={"status_code": status_code, "response": me},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        user_id = me.get("data", {}).get("id")
    if not user_id:
        return ToolResult.failure(
            "x_missing_user_id",
            "Authenticated X user ID is missing.",
            category=ErrorCategory.AUTH,
        )
    payload, bookmarks_rate_limit, status_code = x_client.get_bookmarks(
        http_client=context.http_client,
        access_token=access_token,
        user_id=user_id,
        pagination_token=input_data.get("pagination_token"),
        max_results=input_data.get("max_results", 100),
    )
    rate_limit = bookmarks_rate_limit or rate_limit
    if status_code == 429:
        return ToolResult.failure(
            "x_rate_limited",
            "X bookmarks endpoint is rate limited.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.RATE_LIMIT,
            rate_limit=rate_limit,
        )
    if status_code != 200:
        return ToolResult.failure(
            "x_bookmarks_collect_failed",
            "X bookmarks collection failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.NETWORK,
            rate_limit=rate_limit,
        )
    items = x_parser.parse_bookmarks(payload)
    meta = payload.get("meta", {})
    next_token = meta.get("next_token")
    db_path = input_data.get("db_path")
    if input_data.get("store_cursor", True):
        resolved_db_path = context.db_path
        if db_path:
            from pathlib import Path

            resolved_db_path = Path(db_path).expanduser().resolve()
        if resolved_db_path:
            db.initialize_database(resolved_db_path)
            db.set_sync_cursor(
                resolved_db_path,
                platform="x",
                cursor_name="bookmarks",
                cursor_value=next_token,
                metadata={"result_count": meta.get("result_count"), "newest_id": meta.get("newest_id")},
            )
    return ToolResult.success(
        {
            "platform": "x",
            "user_id": user_id,
            "items": items,
            "summary": {
                "items": len(items),
                "result_count": meta.get("result_count", len(payload.get("data", []))),
                "next_token": next_token,
            },
        },
        rate_limit=rate_limit,
    )


def _credential_output_path(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    return input_data.get("credential_output_path") or context.env.get(x_auth.X_CREDENTIALS_FILE_ENV)


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
) -> str | None:
    if not credential_output_path:
        return None
    return x_auth.write_token_payload(
        credential_output_path,
        payload,
        env=context.env,
        cwd=context.cwd,
    )
