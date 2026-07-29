"""Reddit platform tools."""

from __future__ import annotations

from pathlib import Path
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
from mediagent.platforms.reddit import auth as reddit_auth
from mediagent.platforms.reddit import client as reddit_client
from mediagent.platforms.reddit import parser as reddit_parser


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
                name="reddit.auth.start",
                description="Generate a Reddit OAuth authorization URL for saved-media access.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "client_id": {"type": "string"},
                        "redirect_uri": {"type": "string"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                        "state": {"type": "string"},
                        "duration": {"type": "string", "enum": ["temporary", "permanent"]},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV,),
                dry_run_supported=True,
            ),
            handler=auth_start,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="reddit.auth.exchange",
                description="Exchange a Reddit OAuth authorization code for tokens.",
                input_schema={
                    "type": "object",
                    "required": ["code"],
                    "properties": {
                        "client_id": {"type": "string"},
                        "client_secret": {"type": "string"},
                        "redirect_uri": {"type": "string"},
                        "user_agent": {"type": "string"},
                        "code": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.NETWORK, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_exchange,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="reddit.auth.refresh",
                description="Refresh Reddit OAuth tokens.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "client_id": {"type": "string"},
                        "client_secret": {"type": "string"},
                        "user_agent": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                        "refresh_token_ref": CREDENTIAL_REF_SCHEMA,
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_refresh,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="reddit.auth.status",
                description="Validate Reddit token presence, scopes, and authenticated user.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "required_scopes": {"type": "array", "items": {"type": "string"}},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.NETWORK, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="reddit.saved.collect",
                description="Collect media-bearing Reddit saved items without downloading files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "username": {"type": "string"},
                        "after": {"type": "string"},
                        "limit": {"type": "integer"},
                        "store_cursor": {"type": "boolean"},
                        "media_types": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["photo", "video", "audio"]},
                        },
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_DB),
                dry_run_supported=True,
            ),
            handler=saved_collect,
        ),
    ]


async def auth_start(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    config = reddit_auth.load_config(env=context.env)
    client_id = input_data.get("client_id") or config.client_id
    redirect_uri = input_data.get("redirect_uri") or config.redirect_uri
    if not client_id or not redirect_uri:
        return ToolResult.failure(
            "reddit_auth_missing_config",
            "REDDIT_CLIENT_ID and REDDIT_REDIRECT_URI are required.",
            category=ErrorCategory.VALIDATION,
        )
    result = reddit_auth.build_authorization_start(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=input_data.get("scopes"),
        state=input_data.get("state"),
        duration=input_data.get("duration", "permanent"),
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
    config = reddit_auth.load_config(env=context.env)
    client_id = input_data.get("client_id") or config.client_id
    client_secret = input_data.get("client_secret") or config.client_secret
    redirect_uri = input_data.get("redirect_uri") or config.redirect_uri
    user_agent = input_data.get("user_agent") or config.user_agent
    missing = _missing_config(
        {
            "REDDIT_CLIENT_ID": client_id,
            "REDDIT_REDIRECT_URI": redirect_uri,
            "REDDIT_USER_AGENT": user_agent,
        }
    )
    if missing:
        return _missing_config_result(missing)
    user_agent_error = _invalid_user_agent_result(user_agent)
    if user_agent_error:
        return user_agent_error
    if context.dry_run:
        return ToolResult.success(
            {
                "would_exchange": True,
                "would_write_credentials": credential_output_path is not None,
            }
        )
    payload, rate_limit, status_code = reddit_auth.exchange_code(
        http_client=context.http_client,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        user_agent=user_agent,
        code=input_data["code"],
    )
    if status_code != 200:
        return ToolResult.failure(
            "reddit_auth_exchange_failed",
            "Reddit token exchange failed.",
            data={
                "status_code": status_code,
                "response": _sanitize_auth_payload(payload, secrets=[input_data["code"], client_secret]),
            },
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "status_code": status_code,
            "session": reddit_auth.token_payload_to_session(payload).to_dict(),
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
    config = reddit_auth.load_config(env=context.env)
    credentials = reddit_auth.load_credentials(env=context.env, cwd=context.cwd)
    client_id = input_data.get("client_id") or config.client_id
    client_secret = input_data.get("client_secret") or config.client_secret
    user_agent = input_data.get("user_agent") or config.user_agent
    refresh_token_value = _refresh_token_value(context, input_data, credentials)
    missing = _missing_config(
        {
            "REDDIT_CLIENT_ID": client_id,
            "REDDIT_USER_AGENT": user_agent,
            "refresh_token": refresh_token_value,
        }
    )
    if missing:
        return _missing_config_result(missing, code="reddit_auth_missing_credentials")
    user_agent_error = _invalid_user_agent_result(user_agent)
    if user_agent_error:
        return user_agent_error
    payload, rate_limit, status_code = reddit_auth.refresh_token(
        http_client=context.http_client,
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        refresh_token_value=refresh_token_value,
    )
    if status_code != 200:
        return ToolResult.failure(
            "reddit_auth_refresh_failed",
            "Reddit token refresh failed.",
            data={
                "status_code": status_code,
                "response": _sanitize_auth_payload(payload, secrets=[refresh_token_value, client_secret]),
            },
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "status_code": status_code,
            "session": reddit_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_check": True, "provider": "reddit"})
    config = reddit_auth.load_config(env=context.env)
    if config.user_agent:
        user_agent_error = _invalid_user_agent_result(config.user_agent)
        if user_agent_error:
            return user_agent_error
    session, rate_limit, status_code = reddit_auth.status_from_env(
        env=context.env,
        cwd=context.cwd,
        http_client=context.http_client,
        required_scopes=input_data.get("required_scopes"),
    )
    if session.status != "usable":
        return ToolResult.failure(
            f"reddit_auth_{session.status}",
            "Reddit auth session is not usable.",
            data={"status_code": status_code, "auth_status": session.to_dict()},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    return ToolResult.success(
        {"status_code": status_code, "session": session.to_dict()},
        rate_limit=rate_limit,
    )


async def saved_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success(
            {
                "would_collect": True,
                "platform": "reddit",
                "username": input_data.get("username") or "me",
                "limit": _limit(input_data.get("limit", 100)),
                "store_cursor": input_data.get("store_cursor", True),
            }
        )
    try:
        cursor_db_path = _resolved_db_path(context, input_data) if input_data.get("store_cursor", True) else None
    except PathSafetyError as exc:
        return ToolResult.failure(
            "unsafe_db_path",
            str(exc),
            category=ErrorCategory.FILESYSTEM,
        )
    config = reddit_auth.load_config(env=context.env)
    credentials = reddit_auth.load_credentials(env=context.env, cwd=context.cwd)
    access_token = credentials.get("access_token")
    if not config.user_agent or not access_token:
        missing = _missing_config(
            {
                "REDDIT_USER_AGENT": config.user_agent,
                "access_token": access_token,
            }
        )
        return _missing_config_result(missing, code="reddit_auth_missing_credentials")
    user_agent_error = _invalid_user_agent_result(config.user_agent)
    if user_agent_error:
        return user_agent_error

    username = input_data.get("username") or credentials.get("username") or context.env.get("REDDIT_USERNAME")
    rate_limit = None
    if not username or username == "me":
        payload, me_rate_limit, status_code = reddit_client.get_me(
            http_client=context.http_client,
            access_token=access_token,
            user_agent=config.user_agent,
        )
        rate_limit = me_rate_limit
        if status_code != 200:
            return ToolResult.failure(
                "reddit_me_failed",
                "Could not resolve authenticated Reddit user.",
                data={"status_code": status_code, "response": payload},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            )
        username = payload.get("name")
    if not username:
        return ToolResult.failure(
            "reddit_missing_username",
            "Authenticated Reddit username is missing.",
            category=ErrorCategory.AUTH,
        )

    payload, saved_rate_limit, status_code = reddit_client.get_saved(
        http_client=context.http_client,
        access_token=access_token,
        user_agent=config.user_agent,
        username=username,
        after=input_data.get("after"),
        limit=_limit(input_data.get("limit", 100)),
    )
    rate_limit = saved_rate_limit or rate_limit
    if status_code == 429:
        return ToolResult.failure(
            "reddit_rate_limited",
            "Reddit saved endpoint is rate limited.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.RATE_LIMIT,
            rate_limit=rate_limit,
        )
    if status_code != 200:
        return ToolResult.failure(
            "reddit_saved_collect_failed",
            "Reddit saved collection failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.NETWORK,
            rate_limit=rate_limit,
        )

    items, summary = reddit_parser.parse_saved_listing(
        payload,
        media_types=input_data.get("media_types"),
    )
    cursor_stored = None
    if cursor_db_path:
        db.initialize_database(cursor_db_path)
        cursor_stored = db.set_sync_cursor(
            cursor_db_path,
            platform="reddit",
            cursor_name=f"saved:{username}",
            cursor_value=summary.get("next_after"),
            metadata={
                "items": summary.get("items"),
                "entries": summary.get("entries"),
                "media_types": input_data.get("media_types") or ["photo", "video", "audio"],
            },
        )

    return ToolResult.success(
        {
            "platform": "reddit",
            "username": username,
            "items": items,
            "summary": {
                **summary,
                "cursor_stored": bool(cursor_stored),
            },
        },
        rate_limit=rate_limit,
    )


def _credential_output_path(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    return input_data.get("credential_output_path") or context.env.get(reddit_auth.REDDIT_CREDENTIALS_FILE_ENV)


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
    return reddit_auth.write_token_payload(
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
    return credentials.get("refresh_token")


def _resolved_db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    db_path = input_data.get("db_path")
    if db_path:
        path = Path(db_path).expanduser().resolve()
        ensure_inside(path, context.allowed_write_roots())
        return path
    return context.db_path


def _limit(value: Any) -> int:
    try:
        return max(1, min(100, int(value)))
    except (TypeError, ValueError):
        return 100


def _missing_config(values: dict[str, Any]) -> list[str]:
    return [name for name, value in values.items() if not value]


def _missing_config_result(
    missing: list[str],
    *,
    code: str = "reddit_auth_missing_config",
) -> ToolResult:
    return ToolResult.failure(
        code,
        "Required Reddit auth configuration is missing.",
        data={"missing": missing},
        category=ErrorCategory.AUTH if code.endswith("credentials") else ErrorCategory.VALIDATION,
    )


def _invalid_user_agent_result(user_agent: str) -> ToolResult | None:
    value = user_agent.strip()
    generic_values = {
        "mediagent",
        "mediagent/0.1.0",
        "python-requests",
        "curl",
        "mozilla/5.0",
    }
    if len(value) >= 12 and value.lower() not in generic_values:
        return None
    return ToolResult.failure(
        "reddit_invalid_user_agent",
        "REDDIT_USER_AGENT must be unique and descriptive.",
        category=ErrorCategory.VALIDATION,
    )


def _sanitize_auth_payload(value: Any, *, secrets: list[str | None] | None = None) -> Any:
    secret_values = [secret for secret in (secrets or []) if secret]
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if _is_sensitive_auth_key(str(key))
            else _sanitize_auth_payload(item, secrets=secret_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_auth_payload(item, secrets=secret_values) for item in value]
    if isinstance(value, str):
        sanitized = value
        for secret in secret_values:
            sanitized = sanitized.replace(secret, "<redacted>")
        return sanitized
    return value


def _is_sensitive_auth_key(key: str) -> bool:
    return key.lower() in {
        "code",
        "authorization_code",
        "access_token",
        "refresh_token",
        "client_secret",
    }
