"""Instagram platform tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.links import (
    LinkSafetyPolicy,
    ResolveRequest,
    default_link_resolver_registry,
    sanitize_link_resolution_for_output,
)
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec
from mediagent.platforms.instagram import auth as instagram_auth
from mediagent.platforms.instagram import links as instagram_links


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.auth.status",
                description="Validate a configured Instagram saved session without exposing secrets.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=True,
            ),
            handler=auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.auth.login",
                description="Create or replace an Instagram saved session from explicit local credentials.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "username": {"type": "string"},
                        "password": {"type": "string"},
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_CREDENTIALS,
                    Permission.WRITE_CREDENTIALS,
                    Permission.NETWORK,
                ),
                dry_run_supported=True,
            ),
            handler=auth_login,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.auth.ensure_session",
                description="Reuse or repair an Instagram saved session with bounded login attempts.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                        "cooldown_seconds": {"type": "integer"},
                        "force_login": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_CREDENTIALS,
                    Permission.WRITE_CREDENTIALS,
                    Permission.NETWORK,
                ),
                dry_run_supported=True,
            ),
            handler=auth_ensure_session,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.link.resolve",
                description="Resolve one Instagram post or Reel URL into downloadable media candidates.",
                input_schema={
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {"type": "string"},
                        "session_file": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=True,
            ),
            handler=link_resolve,
        ),
    ]


async def auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if context.dry_run:
        return ToolResult.success({"would_check": True, "provider": "instagram"})
    try:
        status = instagram_auth.session_status(
            env=context.env,
            cwd=context.cwd,
            http_client=context.http_client,
            session_file=session_file,
            timeout=_timeout(input_data),
        )
    except instagram_auth.InstagramPlatformError as exc:
        return _instagram_failure(exc.code, str(exc), details=exc.public_details())
    if status.get("status") != "usable":
        error = status.get("error") if isinstance(status.get("error"), dict) else {}
        code = str(error.get("error_code") or "instagram_session_invalid")
        return _instagram_failure(
            code,
            "Instagram auth session is not usable.",
            data={"session": status},
            details=error,
        )
    return ToolResult.success({"session": status})


async def auth_login(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if context.dry_run:
        config = instagram_auth.load_config(env=context.env, cwd=context.cwd)
        return ToolResult.success(
            {
                "would_login": True,
                "would_write_credentials": session_file is not None,
                "account_present": bool(input_data.get("username") or config.account),
                "secret_present": bool(input_data.get("password") or config.secret),
            }
        )
    try:
        session = instagram_auth.login(
            env=context.env,
            cwd=context.cwd,
            http_client=context.http_client,
            username=input_data.get("username"),
            password=input_data.get("password"),
            session_file=session_file,
            timeout=_timeout(input_data),
        )
    except instagram_auth.InstagramPlatformError as exc:
        _write_attempt_meta(context, session_file=session_file, status="failed", error_code=exc.code, login_attempted=True)
        return _instagram_failure(exc.code, str(exc), details=exc.public_details())
    _write_attempt_meta(context, session_file=session_file, status="usable", error_code=None, login_attempted=True)
    return ToolResult.success({"session": session, "credentials_written": bool(session_file)})


async def auth_ensure_session(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if context.dry_run:
        return ToolResult.success(
            {
                "would_check": True,
                "would_login_if_needed": True,
                "cooldown_seconds": _cooldown_seconds(input_data),
            }
        )
    status = instagram_auth.session_status(
        env=context.env,
        cwd=context.cwd,
        http_client=context.http_client,
        session_file=session_file,
        timeout=_timeout(input_data),
    )
    if status.get("status") == "usable":
        _write_attempt_meta(context, session_file=session_file, status="usable", error_code=None)
        return ToolResult.success({"session": status, "login_attempted": False})
    error = status.get("error") if isinstance(status.get("error"), dict) else {}
    code = str(error.get("error_code") or "instagram_session_invalid")
    if code in instagram_auth.USER_ACTION_REQUIRED_CODES:
        _write_attempt_meta(context, session_file=session_file, status=str(status.get("status")), error_code=code)
        return _instagram_failure(
            code,
            "Instagram session requires user action.",
            data={"session": status, "login_attempted": False},
            details=error,
        )
    path = instagram_auth.session_file_path(env=context.env, cwd=context.cwd, session_file=session_file)
    metadata = instagram_auth.read_session_meta(path) if path else {}
    can_login, next_attempt_at = instagram_auth.should_attempt_login(
        metadata=metadata,
        cooldown_seconds=_cooldown_seconds(input_data),
        force=bool(input_data.get("force_login", False)),
    )
    if not can_login:
        details = {**error, "next_attempt_at": next_attempt_at}
        return _instagram_failure(
            code,
            "Instagram login cooldown is active.",
            data={"session": status, "login_attempted": False},
            details=details,
            category=ErrorCategory.RATE_LIMIT if code in instagram_auth.RETRYABLE_CODES else ErrorCategory.AUTH,
        )
    try:
        session = instagram_auth.login(
            env=context.env,
            cwd=context.cwd,
            http_client=context.http_client,
            session_file=session_file,
            timeout=_timeout(input_data),
        )
    except instagram_auth.InstagramPlatformError as exc:
        _write_attempt_meta(context, session_file=session_file, status="failed", error_code=exc.code, login_attempted=True)
        return _instagram_failure(
            exc.code,
            str(exc),
            data={"previous_session": status, "login_attempted": True},
            details=exc.public_details(),
        )
    _write_attempt_meta(context, session_file=session_file, status="usable", error_code=None, login_attempted=True)
    return ToolResult.success({"session": session, "login_attempted": True})


async def link_resolve(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    url = str(input_data.get("url") or "")
    if instagram_links.instagram_shortcode(url) is None or instagram_links.instagram_post_kind(url) is None:
        return _instagram_failure(
            "instagram_media_unsupported",
            "Instagram URL is not a supported post, reel, or tv URL.",
            details=instagram_auth.instagram_error_details(
                "instagram_media_unsupported",
                {"reason": "unsupported_instagram_url"},
            ),
        )
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    policy = LinkSafetyPolicy(timeout_seconds=_timeout(input_data))
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
        platform_options={"instagram": {"session_file": session_file}},
    )
    resolution = default_link_resolver_registry().resolve(url, request=request)
    public_resolution = sanitize_link_resolution_for_output(resolution)
    if resolution.get("status") == "resolved" and resolution.get("resolver") == "instagram_media_link":
        return ToolResult.success({"resolution": public_resolution})
    if resolution.get("status") == "resolved":
        return _instagram_failure(
            "instagram_media_unsupported",
            "Instagram URL resolved outside the Instagram resolver boundary.",
            data={"resolution": public_resolution},
            details=instagram_auth.instagram_error_details(
                "instagram_media_unsupported",
                {"reason": "non_instagram_resolver", "resolver": resolution.get("resolver")},
            ),
        )
    details = resolution.get("details") if isinstance(resolution.get("details"), dict) else {}
    code = str(details.get("error_code") or resolution.get("skip_reason") or "instagram_resolve_failed")
    return _instagram_failure(
        code,
        "Instagram link could not be resolved.",
        data={"resolution": public_resolution},
        details=details,
    )


def _safe_session_file(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    path = instagram_auth.session_file_path(
        env=context.env,
        cwd=context.cwd,
        session_file=input_data.get("session_file"),
    )
    if path is None:
        return None
    ensure_inside(path, context.allowed_write_roots())
    return str(path)


def _write_attempt_meta(
    context: ToolContext,
    *,
    session_file: str | None,
    status: str,
    error_code: str | None,
    login_attempted: bool = False,
) -> None:
    if not session_file:
        return
    path = Path(session_file)
    try:
        ensure_inside(path, context.allowed_write_roots())
    except PathSafetyError:
        return
    now = datetime.now(UTC).isoformat()
    metadata = instagram_auth.read_session_meta(path)
    metadata.update(
        {
            "last_checked_at": now,
            "last_status": status,
            "last_error_code": error_code,
        }
    )
    if login_attempted or status != "usable":
        metadata["last_login_attempt_at"] = now
    instagram_auth.write_session_meta(path, metadata)


def _instagram_failure(
    code: str,
    message: str,
    *,
    data: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    category: ErrorCategory | None = None,
) -> ToolResult:
    return ToolResult.failure(
        code,
        message,
        data=data,
        details=details or instagram_auth.instagram_error_details(code),
        category=category or _category_for_code(code),
    )


def _category_for_code(code: str) -> ErrorCategory:
    if code in {"instagram_rate_limited", "instagram_temporarily_blocked"}:
        return ErrorCategory.RATE_LIMIT
    if code.startswith("instagram_session") or code in {
        "instagram_login_required",
        "instagram_checkpoint_required",
        "instagram_two_factor_required",
    }:
        return ErrorCategory.AUTH
    if code in {"instagram_media_not_found", "instagram_media_private", "instagram_media_unsupported"}:
        return ErrorCategory.NETWORK
    return ErrorCategory.RUNTIME


def _timeout(input_data: dict[str, Any]) -> float:
    return float(input_data.get("timeout_seconds", 30.0))


def _cooldown_seconds(input_data: dict[str, Any]) -> int:
    return max(0, int(input_data.get("cooldown_seconds", instagram_auth.DEFAULT_LOGIN_COOLDOWN_SECONDS)))
