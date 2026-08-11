"""Instagram platform tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.links import (
    LinkSafetyPolicy,
    ResolveRequest,
    default_link_resolver_registry,
    sanitize_link_resolution_for_output,
)
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec
from mediagent.platforms.instagram import auth as instagram_auth
from mediagent.platforms.instagram import client as instagram_client
from mediagent.platforms.instagram import links as instagram_links
from mediagent.platforms.instagram import parser as instagram_parser
from mediagent.tools import link_tools


def definitions() -> list[ToolDefinition]:
    saved_properties = {
        "db_path": {"type": "string"}, "session_file": {"type": "string"},
        "cursor": {"type": "string"}, "limit": {"type": "integer"},
        "max_pages": {"type": "integer"}, "full_sync": {"type": "boolean"},
        "stop_on_known": {"type": "boolean"}, "store_cursor": {"type": "boolean"},
        "timeout_seconds": {"type": "number"},
    }
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
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.saved.collect",
                description="Collect normalized Instagram saved posts one sequential page at a time.",
                input_schema={"type": "object", "properties": saved_properties},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.NETWORK, Permission.WRITE_DB),
                dry_run_supported=True,
            ), handler=saved_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="instagram.saved.sync",
                description="Collect, deduplicate, download, and record Instagram saved posts.",
                input_schema={"type": "object", "properties": {
                    **saved_properties, "library_root": {"type": "string"}, "target_dir": {"type": "string"},
                    "include_platform_layer": {"type": "boolean"}, "overwrite": {"type": "boolean"},
                    "retry_failed": {"type": "boolean"}, "repair_missing_files": {"type": "boolean"},
                    "write_sidecar_metadata": {"type": "boolean"}, "attempts": {"type": "integer"},
                }},
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_CREDENTIALS, Permission.NETWORK,
                             Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ), handler=saved_sync,
        ),
    ]


async def saved_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _saved_db_path(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_db_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if (input_data.get("store_cursor") or input_data.get("stop_on_known")) and db_path is None:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH when storing cursors or stopping on known items.",
            category=ErrorCategory.VALIDATION,
        )
    if context.dry_run:
        validation = _validate_saved_dry_run(context, input_data)
        if validation is not None:
            return validation
        return ToolResult.success(
            {
                "dry_run": True,
                "platform": "instagram",
                "would_collect": True,
                "request": {
                    key: input_data[key]
                    for key in ("cursor", "limit", "max_pages", "full_sync")
                    if key in input_data
                },
            }
        )
    result = await _collect_saved_pages(context, input_data, known_db_path=db_path)
    if result.is_success:
        result.data.pop("_runtime_items", None)
        if input_data.get("store_cursor") and result.data["summary"]["stop_reason"] in {
            "end_of_feed",
            "max_pages_reached",
            "single_page_default",
        }:
            if db_path:
                db.initialize_database(db_path)
                db.set_sync_cursor(
                    db_path,
                    platform="instagram",
                    cursor_name="saved",
                    cursor_value=result.data["summary"]["next_cursor"],
                    metadata={"items": result.data["summary"]["items"]},
                )
                result.data["summary"]["cursor_stored"] = True
    return result


async def _collect_saved_pages(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    known_db_path: Path | None,
) -> ToolResult:
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    full_sync = bool(input_data.get("full_sync")) and input_data.get("max_pages") is None
    max_pages = None if full_sync else max(1, int(input_data.get("max_pages", 1)))
    limit = input_data.get("limit")
    cursor = input_data.get("cursor")
    items: list[dict[str, Any]] = []
    raw_posts = pages = resources = 0
    known_items_seen = 0
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    stop_reason = "end_of_feed"
    try:
        while True:
            remaining = None if limit is None else max(0, int(limit) - len(items))
            if remaining == 0:
                stop_reason = "limit_reached"
                break
            raw, next_cursor = instagram_client.get_saved_page(
                env=context.env, cwd=context.cwd, http_client=context.http_client,
                session_file=session_file, cursor=cursor, amount=50,
                timeout=_timeout(input_data),
            )
            pages += 1
            raw_posts += len(raw)
            page_items = instagram_parser.parse_saved_posts(raw)
            page_statuses = {}
            if input_data.get("stop_on_known") and not input_data.get("full_sync") and known_db_path is not None:
                page_statuses = db.get_media_statuses(known_db_path, page_items)
            for item in page_items:
                if item["remote_id"] in seen_ids:
                    continue
                if page_statuses.get(("instagram", item["remote_id"])) in link_tools.TERMINAL_ITEM_STATUSES:
                    known_items_seen += 1
                    stop_reason = "known_item_seen"
                    break
                seen_ids.add(item["remote_id"])
                items.append(item)
                resources += len((item.get("metadata") or {}).get("files", []))
                if limit is not None and len(items) >= int(limit):
                    stop_reason = "limit_reached"
                    break
            cursor = next_cursor
            if stop_reason in {"limit_reached", "known_item_seen"} or not cursor:
                break
            if max_pages is not None and pages >= max_pages:
                stop_reason = "max_pages_reached" if input_data.get("max_pages") is not None else "single_page_default"
                break
            if cursor in seen_cursors:
                stop_reason = "repeated_cursor"
                break
            seen_cursors.add(cursor)
    except instagram_auth.InstagramPlatformError as exc:
        return _instagram_failure(exc.code, str(exc), details=exc.public_details())
    public_items = [link_tools._public_media_item(item) for item in items]
    output_cursor = None if stop_reason in {"limit_reached", "known_item_seen"} else cursor
    return ToolResult.success(
        {
            "platform": "instagram",
            "items": public_items,
            "_runtime_items": items,
            "summary": {
                "pages_fetched": pages,
                "raw_posts": raw_posts,
                "items": len(items),
                "resources": resources,
                "next_cursor": output_cursor,
                "stop_reason": stop_reason,
                "known_items_seen": known_items_seen,
            },
        }
    )


async def saved_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _saved_db_path(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_db_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if db_path is None:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    if context.dry_run:
        validation = _validate_saved_dry_run(context, input_data)
        if validation is not None:
            return validation
        raw_target = input_data.get("library_root") or input_data.get("target_dir")
        if raw_target:
            try:
                ensure_inside(normalize_path(str(raw_target), env=context.env, cwd=context.cwd), context.allowed_write_roots())
            except PathSafetyError as exc:
                return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    else:
        db.initialize_database(db_path)
    collected = (
        await _collect_saved_pages(
            context,
            {**input_data, "store_cursor": False},
            known_db_path=db_path,
        )
        if not context.dry_run
        else None
    )
    if context.dry_run:
        return ToolResult.success(
            {
                "dry_run": True,
                "platform": "instagram",
                "would_sync": True,
                "request": {key: value for key, value in input_data.items() if key != "session_file"},
            }
        )
    assert collected is not None
    if not collected.is_success:
        return collected
    items = list(collected.data.pop("_runtime_items", []))
    known_seen = int(collected.data["summary"].get("known_items_seen", 0))
    for item in items:
        db.upsert_media_item(db_path, item)
    statuses = db.get_media_statuses(db_path, items)
    candidates, skipped = link_tools._sync_candidates(
        items, statuses, db_path=db_path, retry_failed=bool(input_data.get("retry_failed")),
        repair_missing_files=bool(input_data.get("repair_missing_files")),
    )
    summary = {
        "collected": collected.data["summary"]["items"],
        "known": known_seen,
        "queued": len(candidates),
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "repaired": skipped["repair_items"],
        "skipped": skipped["skipped_items"],
        "files": 0,
        "bytes": 0,
        **collected.data["summary"],
    }
    item_results = []
    artifacts = []
    warnings = []
    for item in candidates:
        result = await link_tools._sync_one_link_item(context, db_path, item, input_data)
        item_results.append(result)
        summary[result["status"]] += 1
        summary["files"] += result["files_downloaded"]
        summary["bytes"] += result["bytes_written"]
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])
    run_status = (
        "success"
        if not summary["failed"] and not summary["partial"]
        else ("partial" if summary["downloaded"] else "failed")
    )
    cursor_safe = (
        run_status == "success"
        and not context.dry_run
        and not known_seen
        and collected.data["summary"]["stop_reason"] == "end_of_feed"
    )
    if input_data.get("store_cursor", True) and cursor_safe:
        db.set_sync_cursor(
            db_path,
            platform="instagram",
            cursor_name="saved",
            cursor_value=collected.data["summary"]["next_cursor"],
            metadata={"items": len(items)},
        )
        summary["cursor_stored"] = True
        summary["cursor_reason"] = "stored"
    else:
        summary["cursor_stored"] = False
        summary["cursor_reason"] = "disabled" if not input_data.get("store_cursor", True) else "incomplete_boundary"
    data = {"platform": "instagram", "summary": summary, "items": item_results}
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings)
    return ToolResult.failure(
        "instagram_saved_sync_partial" if run_status == "partial" else "instagram_saved_sync_failed",
        "Instagram saved sync finished with failed items.",
        data=data,
        warnings=warnings,
        category=ErrorCategory.NETWORK,
    )


def _saved_db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    if input_data.get("db_path"):
        path = Path(resolve_placeholders(str(input_data["db_path"]), context.env)).expanduser().resolve()
        ensure_inside(path, context.allowed_write_roots())
        return path
    return context.db_path


def _validate_saved_dry_run(context: ToolContext, input_data: dict[str, Any]) -> ToolResult | None:
    try:
        session_file = _safe_session_file(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_credential_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if session_file is None or not Path(session_file).exists():
        return _instagram_failure("instagram_session_missing", "Instagram saved session is missing.")
    return None


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
