"""Pixiv platform tools."""

from __future__ import annotations

import hashlib
import mimetypes
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.auth import CredentialRef, resolve_credential, resolve_credential_path
from mediagent.core.comics import CBZ_STORAGE_LAYOUT, comic_archive_relative_path
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.links import LinkSafetyPolicy, ResolveRequest, default_link_resolver_registry, sanitize_link_resolution_for_output
from mediagent.core.storage import default_library_root, plan_storage_path, platform_library_env_name
from mediagent.core.sync import (
    TERMINAL_ITEM_STATUSES,
    item_status_from_file_counts,
)
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.tools.download_tools import download_http
from mediagent.tools.metadata_tools import metadata_write
from mediagent.platforms.pixiv import auth as pixiv_auth
from mediagent.platforms.pixiv import client as pixiv_client
from mediagent.platforms.pixiv import links as pixiv_links
from mediagent.platforms.pixiv import parser as pixiv_parser
from mediagent.tools import pixiv_library_tools


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.auth.login",
                description="Start or complete explicit local Pixiv OAuth PKCE login.",
                input_schema={
                    "type": "object",
                    "required_with": [
                        {"field": "code", "required": ["code_verifier"]},
                        {"field": "callback_url", "required": ["code_verifier"]},
                    ],
                    "properties": {
                        "code": {"type": "string"},
                        "callback_url": {"type": "string"},
                        "code_verifier": {"type": "string"},
                        "client_id": {"type": "string"},
                        "client_secret": {"type": "string"},
                        "redirect_uri": {"type": "string"},
                        "credential_output_path": {"type": "string"},
                        "open_browser": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_login,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.auth.status",
                description="Validate configured Pixiv credentials without exposing secrets.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.auth.refresh",
                description="Refresh Pixiv App API access credentials.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "credential_output_path": {"type": "string"},
                        "refresh_token_ref": _credential_ref_schema(),
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
                name="pixiv.link.resolve",
                description="Resolve one Pixiv artwork URL or id into downloadable media candidates.",
                input_schema={
                    "type": "object",
                    "required_any": [["url", "illust_id"]],
                    "properties": {
                        "url": {"type": "string"},
                        "illust_id": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                        "include_ugoira_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.WRITE_CREDENTIALS),
                dry_run_supported=True,
            ),
            handler=link_resolve,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.bookmarks.collect",
                description="Collect bookmarked Pixiv illustrations and manga for the configured account.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "user_id": {"type": "string"},
                        "restrict": {"type": "string", "enum": ["public", "private"]},
                        "max_bookmark_id": {"type": "string"},
                        "tag": {"type": "string"},
                        "store_cursor": {"type": "boolean"},
                        "include_ugoira_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.NETWORK,
                    Permission.READ_CREDENTIALS,
                    Permission.WRITE_CREDENTIALS,
                    Permission.WRITE_DB,
                ),
                dry_run_supported=True,
            ),
            handler=bookmarks_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.bookmarks.sync",
                description="Deterministically collect, deduplicate, download, and record Pixiv bookmarks.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "user_id": {"type": "string"},
                        "restrict": {"type": "string", "enum": ["public", "private"]},
                        "max_bookmark_id": {"type": "string"},
                        "tag": {"type": "string"},
                        "limit": {"type": "integer"},
                        "max_pages": {"type": "integer"},
                        "full_sync": {"type": "boolean"},
                        "stop_on_known": {"type": "boolean"},
                        "media_types": {"type": "array", "items": {"type": "string", "enum": ["photo", "video", "audio"]}},
                        "overwrite": {"type": "boolean"},
                        "retry_failed": {"type": "boolean"},
                        "repair_missing_files": {"type": "boolean"},
                        "package_comics": {"type": "boolean"},
                        "attempts": {"type": "integer"},
                        "timeout_seconds": {"type": "number"},
                        "include_ugoira_metadata": {"type": "boolean"},
                        "store_cursor": {"type": "boolean"},
                        "write_sidecar_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.NETWORK,
                    Permission.READ_CREDENTIALS,
                    Permission.WRITE_CREDENTIALS,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=bookmarks_sync,
        ),
    ]


async def auth_login(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if input_data.get("code") or input_data.get("callback_url"):
        return await _auth_login_exchange(context, input_data)
    if input_data.get("code_verifier"):
        return ToolResult.failure(
            "pixiv_auth_login_missing_code",
            "Provide code or callback_url together with code_verifier to exchange Pixiv credentials.",
            category=ErrorCategory.VALIDATION,
        )

    start = pixiv_auth.build_authorization_start()
    browser_opened = False
    if input_data.get("open_browser") and not context.dry_run:
        browser_opened = webbrowser.open(start["authorization_url"])
    return ToolResult.success(
        {
            "mode": "start",
            "authorization_url": start["authorization_url"],
            "code_verifier": start["code_verifier"],
            "redirect_uri": start["redirect_uri"],
            "browser_opened": browser_opened,
            "manual_code_instructions": (
                "Open the authorization URL, complete Pixiv login, then run "
                "pixiv.auth.login again with the full callback_url or only the code query "
                "parameter plus code_verifier."
            ),
        },
        warnings=[
            "Pixiv OAuth is an unofficial adapter path and may change.",
            "The authorization code is short-lived; exchange it immediately.",
        ],
    )


async def _auth_login_exchange(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    submitted_code = _authorization_code_value(input_data)
    if not submitted_code:
        return ToolResult.failure(
            "pixiv_auth_login_missing_code",
            "Pixiv callback URL does not contain a code query parameter.",
            category=ErrorCategory.VALIDATION,
        )
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
                "mode": "exchange",
                "would_exchange": True,
                "would_write_credentials": credential_output_path is not None,
            }
        )
    credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
    payload, rate_limit, status_code = pixiv_auth.exchange_authorization_code(
        http_client=context.http_client,
        code=submitted_code,
        code_verifier=input_data["code_verifier"],
        client_id=input_data.get("client_id") or credentials.get("client_id") or context.env.get("PIXIV_CLIENT_ID"),
        client_secret=(
            input_data.get("client_secret")
            or credentials.get("client_secret")
            or context.env.get("PIXIV_CLIENT_SECRET")
        ),
        redirect_uri=input_data.get("redirect_uri"),
    )
    if status_code != 200:
        return ToolResult.failure(
            "pixiv_auth_login_exchange_failed",
            "Pixiv authorization-code exchange failed.",
            data={
                "status_code": status_code,
                "response": _redact_authorization_code_payload(payload, submitted_code),
            },
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "mode": "exchange",
            "status_code": status_code,
            "session": pixiv_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_check": True, "platform": "pixiv"})
    credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
    refresh_token_value = credentials.get("refresh_token")
    access_token = credentials.get("access_token")
    expires_at = credentials.get("expires_at")
    user_id = credentials.get("user_id")
    if access_token and user_id and not pixiv_auth.is_expired(str(expires_at) if expires_at else None):
        payload, rate_limit, status_code = pixiv_client.get_user_detail(
            http_client=context.http_client,
            access_token=access_token,
            user_id=str(user_id),
        )
        if status_code == 200:
            return ToolResult.success(
                {
                    "status_code": status_code,
                    "session": pixiv_auth.session_from_credentials(credentials, status="usable").to_dict(),
                },
                rate_limit=rate_limit,
            )
        return ToolResult.failure(
            "pixiv_auth_invalid",
            "Pixiv access token is not usable.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    if not refresh_token_value:
        session = pixiv_auth.session_from_credentials(credentials, status="missing_credentials")
        return ToolResult.failure(
            "pixiv_auth_missing_credentials",
            "PIXIV_REFRESH_TOKEN or PIXIV_CREDENTIALS_FILE is required.",
            data={"auth_status": session.to_dict()},
            category=ErrorCategory.AUTH,
        )
    payload, rate_limit, status_code = _refresh_payload(context, str(refresh_token_value), credentials)
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
            "status_code": status_code,
            "session": pixiv_auth.token_payload_to_session(payload).to_dict(),
            "credentials_written": False,
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
    credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
    refresh_token_value = _refresh_token_value(context, input_data, credentials)
    if not refresh_token_value:
        return ToolResult.failure(
            "pixiv_auth_missing_credentials",
            "PIXIV_REFRESH_TOKEN or refresh_token_ref is required.",
            category=ErrorCategory.AUTH,
        )
    payload, rate_limit, status_code = _refresh_payload(context, refresh_token_value, credentials)
    if status_code != 200:
        return ToolResult.failure(
            "pixiv_auth_refresh_failed",
            "Pixiv token refresh failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    credential_file = _persist_credentials(context, credential_output_path, payload)
    return ToolResult.success(
        {
            "status_code": status_code,
            "session": pixiv_auth.token_payload_to_session(payload).to_dict(),
            "credential_file": credential_file,
            "credentials_written": credential_file is not None,
        },
        rate_limit=rate_limit,
    )


async def link_resolve(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    raw_url = _pixiv_link_input(input_data)
    if raw_url is None:
        return ToolResult.failure(
            "pixiv_artwork_unsupported_url",
            "Provide a Pixiv artwork URL or illust_id.",
            category=ErrorCategory.VALIDATION,
        )
    if pixiv_links.pixiv_artwork_id(raw_url) is None:
        return ToolResult.failure(
            "pixiv_artwork_unsupported_url",
            "Pixiv URL is not a supported artwork URL.",
            data={
                "details": pixiv_links.pixiv_error_details(
                    "pixiv_artwork_unsupported_url",
                    {"reason": "missing_artwork_id"},
                )
            },
            category=ErrorCategory.VALIDATION,
        )
    policy = LinkSafetyPolicy(timeout_seconds=float(input_data.get("timeout_seconds", 30.0)))
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
        platform_options={"pixiv": {"include_ugoira_metadata": input_data.get("include_ugoira_metadata", True)}},
    )
    resolution = default_link_resolver_registry().resolve(raw_url, request=request)
    public_resolution = sanitize_link_resolution_for_output(resolution)
    if resolution.get("status") == "resolved" and resolution.get("resolver") == "pixiv_artwork_link":
        return ToolResult.success({"resolution": public_resolution})
    if resolution.get("status") == "resolved":
        return ToolResult.failure(
            "pixiv_artwork_unsupported_url",
            "Pixiv URL resolved outside the Pixiv resolver boundary.",
            data={"resolution": public_resolution},
            category=ErrorCategory.VALIDATION,
        )
    details = resolution.get("details") if isinstance(resolution.get("details"), dict) else {}
    code = str(details.get("error_code") or resolution.get("skip_reason") or "pixiv_artwork_resolve_failed")
    return _pixiv_link_failure(code, data={"resolution": public_resolution}, details=details)


async def bookmarks_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_collect": True, "platform": "pixiv"})
    return await _bookmarks_collect(context, input_data, allow_side_effects=True)


async def _bookmarks_collect(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    allow_side_effects: bool,
) -> ToolResult:
    try:
        credential_output_path = _safe_credential_output_path(context, input_data)
    except PathSafetyError as exc:
        return ToolResult.failure(
            "unsafe_credential_path",
            str(exc),
            category=ErrorCategory.FILESYSTEM,
        )
    credentials = pixiv_auth.load_credentials(env=context.env, cwd=context.cwd)
    token_result = _ensure_access_token(
        context,
        credentials,
        credential_output_path if allow_side_effects else None,
    )
    if not token_result["ok"]:
        return token_result["result"]
    access_token = token_result["access_token"]
    user_id = input_data.get("user_id") or token_result.get("user_id") or credentials.get("user_id")
    if not user_id:
        return ToolResult.failure(
            "pixiv_missing_user_id",
            "Pixiv user ID is missing. Run pixiv.auth.refresh with a credential output path first.",
            category=ErrorCategory.AUTH,
        )
    payload, rate_limit, status_code = pixiv_client.get_user_bookmarks(
        http_client=context.http_client,
        access_token=access_token,
        user_id=str(user_id),
        restrict=input_data.get("restrict", "public"),
        max_bookmark_id=input_data.get("max_bookmark_id"),
        tag=input_data.get("tag"),
    )
    if status_code == 429:
        return ToolResult.failure(
            "pixiv_rate_limited",
            "Pixiv bookmarks endpoint is rate limited.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.RATE_LIMIT,
            rate_limit=rate_limit,
        )
    if status_code in (401, 403):
        return ToolResult.failure(
            "pixiv_auth_failed",
            "Pixiv credentials cannot access bookmarks.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.AUTH,
            rate_limit=rate_limit,
        )
    if status_code != 200:
        return ToolResult.failure(
            "pixiv_bookmarks_collect_failed",
            "Pixiv bookmarks collection failed.",
            data={"status_code": status_code, "response": payload},
            category=ErrorCategory.NETWORK,
            rate_limit=rate_limit,
        )

    ugoira_metadata = {}
    if input_data.get("include_ugoira_metadata", True):
        ugoira_metadata = _collect_ugoira_metadata(context, access_token, payload)
    items, next_cursor = pixiv_parser.parse_bookmarks(payload, ugoira_metadata_by_id=ugoira_metadata)

    db_path = input_data.get("db_path")
    if allow_side_effects and input_data.get("store_cursor", True):
        resolved_db_path = context.db_path
        if db_path:
            resolved_db_path = Path(db_path).expanduser().resolve()
        if resolved_db_path:
            db.initialize_database(resolved_db_path)
            db.set_sync_cursor(
                resolved_db_path,
                platform="pixiv",
                cursor_name=f"bookmarks:{input_data.get('restrict', 'public')}",
                cursor_value=next_cursor,
                metadata={"items": len(items), "user_id": str(user_id), "tag": input_data.get("tag")},
            )
    return ToolResult.success(
        {
            "platform": "pixiv",
            "user_id": str(user_id),
            "items": items,
            "summary": {
                "items": len(items),
                "next_max_bookmark_id": next_cursor,
                "restrict": input_data.get("restrict", "public"),
            },
        },
        rate_limit=rate_limit,
    )


async def bookmarks_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _sync_db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    try:
        target_dir, include_platform_layer = _sync_target_dir(context, input_data)
        ensure_inside(target_dir, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    collect_result, collected_items, pagination = await _collect_sync_pages(context, db_path, input_data)
    if not collect_result.is_success:
        return collect_result

    filtered_items = _filter_media_types(collected_items, input_data.get("media_types"))
    items = _limited_items(filtered_items, input_data.get("limit"))
    if context.dry_run:
        statuses = db.get_media_statuses(db_path, items)
        items_to_sync, skipped, unavailable, repair_summary = _sync_candidates(
            items,
            statuses,
            db_path=db_path,
            retry_failed=input_data.get("retry_failed", False),
            repair_missing_files=input_data.get("repair_missing_files", False),
        )
        planned_downloads = _planned_downloads(
            target_dir,
            items_to_sync,
            include_platform_layer=include_platform_layer,
        )
        planned_packages = _planned_comic_packages(
            target_dir,
            [item for item in items if item.get("source_availability") != "unavailable"],
            include_platform_layer=include_platform_layer,
        ) if input_data.get("package_comics") else []
        cursor_decision = _pixiv_sync_cursor_decision(
            input_data,
            available_items=filtered_items,
            items=items,
            run_status="dry_run",
            max_pages_reached=pagination["max_pages_reached"],
            known_item_seen=pagination["known_item_seen"],
        )
        return ToolResult.success(
            {
                "dry_run": True,
                "platform": "pixiv",
                "target_dir": str(target_dir),
                "summary": {
                    "collected": len(collected_items),
                    "pages_scanned": pagination["pages_scanned"],
                    "max_pages_reached": pagination["max_pages_reached"],
                    "stop_on_known": pagination["stop_on_known"],
                    "known_item_seen": pagination["known_item_seen"],
                    "known_items_seen": pagination["known_items_seen"],
                    "collection_stop_reason": pagination["stop_reason"],
                    "media_type_filtered": len(filtered_items),
                    "discovered": len(items),
                    "queued": len(items_to_sync),
                    "skipped": skipped,
                    "unavailable": unavailable,
                    **repair_summary,
                    "planned_files": len(planned_downloads),
                    "planned_comic_packages": len(planned_packages),
                    "downloaded": 0,
                    "partial": 0,
                    "failed": 0,
                    "bytes_written": 0,
                    "cursor_stored": False,
                    "cursor_reason": cursor_decision["reason"],
                },
                "planned_downloads": planned_downloads,
                "planned_packages": planned_packages,
            }
        )

    db.initialize_database(db_path)
    for item in items:
        db.upsert_media_item(db_path, item)
    statuses = db.get_media_statuses(db_path, items)
    items_to_sync, skipped, unavailable, repair_summary = _sync_candidates(
        items,
        statuses,
        db_path=db_path,
        retry_failed=input_data.get("retry_failed", False),
        repair_missing_files=input_data.get("repair_missing_files", False),
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "collected": len(collected_items),
        "pages_scanned": pagination["pages_scanned"],
        "max_pages_reached": pagination["max_pages_reached"],
        "stop_on_known": pagination["stop_on_known"],
        "known_item_seen": pagination["known_item_seen"],
        "known_items_seen": pagination["known_items_seen"],
        "collection_stop_reason": pagination["stop_reason"],
        "media_type_filtered": len(filtered_items),
        "discovered": len(items),
        "queued": len(items_to_sync),
        "skipped": skipped,
        "unavailable": unavailable,
        **repair_summary,
        "repaired": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "comic_packages": 0,
        "comic_packages_existing": 0,
        "comic_packages_migrated": 0,
        "comic_packages_failed": 0,
        "target_dir": str(target_dir),
    }
    item_results = []
    artifacts = []
    warnings = []
    comic_package_results = []

    for item in items_to_sync:
        result = await _sync_one_pixiv_item(
            context,
            db_path,
            target_dir,
            item,
            input_data,
            include_platform_layer=include_platform_layer,
        )
        item_results.append(result)
        summary[result["status"]] += 1
        summary["files_downloaded"] += result["files_downloaded"]
        summary["files_failed"] += result["files_failed"]
        summary["bytes_written"] += result["bytes_written"]
        if item.get("_repair") and result["status"] == "downloaded":
            summary["repaired"] += 1
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])

    if input_data.get("package_comics"):
        current_statuses = db.get_media_statuses(db_path, items)
        for item in items:
            if not _is_comic_item(item) or item.get("source_availability") == "unavailable":
                continue
            if current_statuses.get((item["platform"], item["remote_id"])) != "downloaded":
                continue
            package = pixiv_library_tools.package_one_comic(
                db_path=db_path,
                library_root=target_dir,
                remote_id=str(item["remote_id"]),
                include_platform_layer=include_platform_layer,
                overwrite=bool(input_data.get("overwrite")),
            )
            comic_package_results.append(package)
            if package["status"] == "packaged":
                summary["comic_packages"] += 1
                artifacts.append({"type": "file", "path": package["target_path"]})
            elif package["status"] == "existing":
                summary["comic_packages_existing"] += 1
            elif package["status"] == "legacy_migrated":
                summary["comic_packages_migrated"] += 1
            else:
                summary["comic_packages_failed"] += 1
                warnings.append(
                    f"Pixiv comic {item['remote_id']} downloaded, but CBZ packaging did not complete: "
                    f"{package.get('reason') or package['status']}"
                )

    run_status = "success"
    if summary["failed"] or summary["partial"] or summary["comic_packages_failed"]:
        run_status = "partial" if summary["downloaded"] else "failed"
    cursor_decision = _pixiv_sync_cursor_decision(
        input_data,
        available_items=filtered_items,
        items=items,
        run_status=run_status,
        max_pages_reached=pagination["max_pages_reached"],
        known_item_seen=pagination["known_item_seen"],
    )
    if cursor_decision["should_store"]:
        cursor = _store_pixiv_sync_cursor(
            db_path,
            collect_result=collect_result,
            input_data=input_data,
            items=items,
        )
        summary["cursor_stored"] = True
        summary["cursor_value"] = cursor["cursor_value"]
        summary["cursor_reason"] = "stored"
    else:
        summary["cursor_stored"] = False
        summary["cursor_value"] = collect_result.data.get("summary", {}).get("next_max_bookmark_id")
        summary["cursor_reason"] = cursor_decision["reason"]
        if cursor_decision["warning"]:
            warnings.append(cursor_decision["warning"])
    _record_pixiv_sync_run(db_path, run_status=run_status, summary=summary, error=None)

    data = {
        "platform": "pixiv",
        "target_dir": str(target_dir),
        "summary": summary,
        "items": item_results,
        "comic_packages": comic_package_results,
    }
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings, rate_limit=collect_result.rate_limit)
    return ToolResult.failure(
        "pixiv_bookmarks_sync_partial" if run_status == "partial" else "pixiv_bookmarks_sync_failed",
        "Pixiv bookmark sync finished with failed or partially downloaded items.",
        data=data,
        warnings=warnings,
        category=ErrorCategory.NETWORK,
        rate_limit=collect_result.rate_limit,
    )


def _sync_db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(str(raw_path), context.env)).expanduser().resolve()
    return context.db_path


def _pixiv_link_input(input_data: dict[str, Any]) -> str | None:
    if input_data.get("url"):
        return str(input_data["url"])
    if input_data.get("illust_id"):
        illust_id = str(input_data["illust_id"]).strip()
        if illust_id.isdigit():
            return pixiv_links.pixiv_canonical_artwork_url(illust_id)
    return None


def _pixiv_link_failure(
    code: str,
    *,
    data: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> ToolResult:
    if code == "pixiv_rate_limited":
        category = ErrorCategory.RATE_LIMIT
    elif code in {"pixiv_auth_missing_credentials", "pixiv_auth_refresh_failed", "pixiv_auth_failed"}:
        category = ErrorCategory.AUTH
    elif code in {"unsafe_credential_path"}:
        category = ErrorCategory.FILESYSTEM
    elif code in {"pixiv_artwork_unsupported_url"}:
        category = ErrorCategory.VALIDATION
    else:
        category = ErrorCategory.NETWORK
    return ToolResult.failure(
        code,
        "Pixiv artwork link could not be resolved.",
        data=data,
        details=pixiv_links.pixiv_error_details(code, details or {}),
        category=category,
    )


def _sync_target_dir(context: ToolContext, input_data: dict[str, Any]) -> tuple[Path, bool]:
    raw_path = input_data.get("library_root") or input_data.get("target_dir")
    if raw_path:
        return (
            normalize_path(str(raw_path), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", True),
        )
    platform_root = context.env.get(platform_library_env_name("pixiv"))
    if platform_root:
        return (
            normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", False),
        )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        input_data.get("include_platform_layer", True),
    )


async def _collect_sync_pages(
    context: ToolContext,
    db_path: Path,
    input_data: dict[str, Any],
) -> tuple[ToolResult, list[dict[str, Any]], dict[str, Any]]:
    full_sync = bool(input_data.get("full_sync")) and input_data.get("max_pages") is None
    max_pages_limited = input_data.get("max_pages") is not None
    single_page_default = not full_sync and input_data.get("max_pages") is None
    max_pages = max(1, int(input_data.get("max_pages", 1))) if not full_sync else None
    stop_on_known = bool(input_data.get("stop_on_known", False))
    cursor = input_data.get("max_bookmark_id")
    collected_items: list[dict[str, Any]] = []
    last_result: ToolResult | None = None
    pages_scanned = 0
    max_pages_reached = False
    known_items_seen = 0
    stop_reason = "end_of_feed"
    seen_cursors: set[str] = set()
    page_number = 0
    while True:
        page_number += 1
        collect_input = {
            "db_path": str(db_path),
            "restrict": input_data.get("restrict", "public"),
            "store_cursor": False,
            "include_ugoira_metadata": input_data.get("include_ugoira_metadata", True),
        }
        for field in ("user_id", "tag"):
            if input_data.get(field) is not None:
                collect_input[field] = input_data[field]
        if cursor:
            collect_input["max_bookmark_id"] = cursor
        result = await _bookmarks_collect(
            context,
            collect_input,
            allow_side_effects=not context.dry_run,
        )
        if not result.is_success:
            return result, collected_items, {
                "pages_scanned": pages_scanned,
                "max_pages_reached": max_pages_reached,
            }
        last_result = result
        pages_scanned = page_number
        page_items = result.data.get("items", [])
        collected_items.extend(page_items)
        if stop_on_known:
            page_known = _known_items_in_page(
                db_path,
                page_items,
                media_types=input_data.get("media_types"),
                retry_failed=input_data.get("retry_failed", False),
            )
            known_items_seen += page_known
            if page_known:
                stop_reason = "known_item_seen"
                break
        cursor = result.data.get("summary", {}).get("next_max_bookmark_id")
        if not cursor:
            break
        if max_pages is not None and page_number == max_pages:
            if max_pages_limited:
                max_pages_reached = True
                stop_reason = "max_pages_reached"
            if single_page_default:
                stop_reason = "single_page_default"
            break
        cursor_key = str(cursor)
        if cursor_key in seen_cursors:
            stop_reason = "repeated_cursor"
            break
        seen_cursors.add(cursor_key)
    assert last_result is not None
    return last_result, collected_items, {
        "pages_scanned": pages_scanned,
        "max_pages_reached": max_pages_reached,
        "stop_on_known": stop_on_known,
        "known_item_seen": known_items_seen > 0,
        "known_items_seen": known_items_seen,
        "stop_reason": stop_reason,
    }


def _known_items_in_page(
    db_path: Path,
    page_items: list[dict[str, Any]],
    *,
    media_types: Any,
    retry_failed: bool,
) -> int:
    filtered = _filter_media_types(page_items, media_types)
    if not filtered:
        return 0
    statuses = db.get_media_statuses(db_path, filtered)
    return sum(1 for item in filtered if _is_known_stop_status(statuses.get((item["platform"], item["remote_id"])), retry_failed=retry_failed))


def _is_known_stop_status(status: str | None, *, retry_failed: bool) -> bool:
    if status is None:
        return False
    if status == "failed" and retry_failed:
        return False
    return status in TERMINAL_ITEM_STATUSES


def _filter_media_types(items: list[dict[str, Any]], media_types: Any) -> list[dict[str, Any]]:
    if not media_types:
        return items
    allowed = {str(media_type) for media_type in media_types}
    return [item for item in items if item.get("media_type") in allowed]


def _limited_items(items: list[dict[str, Any]], limit: Any) -> list[dict[str, Any]]:
    if limit is None:
        return items
    return items[: max(0, int(limit))]


def _sync_candidates(
    items: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str],
    *,
    db_path: Path,
    retry_failed: bool,
    repair_missing_files: bool,
) -> tuple[list[dict[str, Any]], int, int, dict[str, int]]:
    candidates = []
    skipped = 0
    unavailable = 0
    repair_summary = {
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
    }
    for item in items:
        if item.get("source_availability") == "unavailable":
            skipped += 1
            unavailable += 1
            continue
        status = statuses.get((item["platform"], item["remote_id"]))
        if status == "failed" and retry_failed:
            candidates.append(item)
            continue
        if status in TERMINAL_ITEM_STATUSES:
            if status == "downloaded" and repair_missing_files:
                repair = _repair_assessment(db_path, item)
                if repair["repairable"]:
                    repair_item = dict(item)
                    repair_item["_repair"] = repair
                    candidates.append(repair_item)
                    repair_summary["repair_items"] += 1
                    repair_summary["repair_files_missing"] += int(repair["missing"])
                    repair_summary["repair_files_corrupt"] += int(repair["corrupt"])
                    repair_summary["repair_files_unhealthy"] += int(repair["unhealthy"])
                    continue
            skipped += 1
            continue
        candidates.append(item)
    return candidates, skipped, unavailable, repair_summary


def _repair_assessment(db_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    records = db.list_media_files(db_path, platform=item["platform"], remote_id=item["remote_id"])
    records_by_remote = {str(record.get("remote_url") or ""): record for record in records if record.get("remote_url")}
    missing = 0
    corrupt = 0
    unhealthy = 0
    files: list[dict[str, Any]] = []
    for file_info in _item_files(item):
        remote_url = str(file_info.get("url") or "")
        record = records_by_remote.get(remote_url)
        reason = None
        health = None
        status = None
        local_path = None
        if record is None:
            reason = "missing_record"
            missing += 1
        else:
            health = str(record.get("file_health") or "unknown")
            status = str(record.get("status") or "")
            local_path = record.get("local_path")
            if health == "corrupt":
                reason = "corrupt_file"
                corrupt += 1
            elif health == "missing":
                reason = "missing_file"
                missing += 1
            elif status and status != "downloaded":
                reason = "unhealthy_status"
                unhealthy += 1
            elif status == "downloaded" and local_path and (
                _path_is_in_trash(Path(str(local_path))) or not Path(str(local_path)).exists()
            ):
                reason = "downloaded_file_missing_on_disk"
                missing += 1
            elif health not in {"valid", "unknown"}:
                reason = "unhealthy_file"
                unhealthy += 1
        if reason:
            files.append(
                {
                    "remote_url": remote_url,
                    "page": file_info.get("page"),
                    "reason": reason,
                    "status": status,
                    "file_health": health,
                    "local_path": local_path,
                }
            )
    return {
        "repairable": bool(files),
        "missing": missing,
        "corrupt": corrupt,
        "unhealthy": unhealthy,
        "files": files,
    }


def _repair_file_requires_overwrite(item: dict[str, Any], file_info: dict[str, Any]) -> bool:
    repair = item.get("_repair") if isinstance(item.get("_repair"), dict) else {}
    remote_url = str(file_info.get("url") or "")
    return any(
        entry.get("remote_url") == remote_url
        and entry.get("reason") in {"corrupt_file", "unhealthy_status", "unhealthy_file"}
        for entry in repair.get("files", [])
        if isinstance(entry, dict)
    )


def _path_is_in_trash(path: Path) -> bool:
    return any(part.lower() == ".trash" for part in path.parts)


def _pixiv_sync_cursor_decision(
    input_data: dict[str, Any],
    *,
    available_items: list[dict[str, Any]],
    items: list[dict[str, Any]],
    run_status: str,
    max_pages_reached: bool = False,
    known_item_seen: bool = False,
) -> dict[str, Any]:
    if not input_data.get("store_cursor", True):
        return {"should_store": False, "reason": "disabled", "warning": None}
    if run_status == "dry_run":
        return {"should_store": False, "reason": "dry_run", "warning": None}
    if input_data.get("stop_on_known") and known_item_seen:
        return {
            "should_store": False,
            "reason": "known_item_seen",
            "warning": None,
        }
    if len(items) < len(available_items):
        return {
            "should_store": False,
            "reason": "limit_truncated",
            "warning": "Pixiv cursor was not advanced because limit truncated the selected bookmark items.",
        }
    if max_pages_reached:
        return {
            "should_store": False,
            "reason": "max_pages_reached",
            "warning": "Pixiv cursor was not advanced because max_pages stopped before the bookmark pagination ended.",
        }
    if run_status != "success":
        return {
            "should_store": False,
            "reason": "run_not_successful",
            "warning": "Pixiv cursor was not advanced because the sync run was not fully successful.",
        }
    return {"should_store": True, "reason": "ready", "warning": None}


def _store_pixiv_sync_cursor(
    db_path: Path,
    *,
    collect_result: ToolResult,
    input_data: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    collect_summary = collect_result.data.get("summary", {})
    return db.set_sync_cursor(
        db_path,
        platform="pixiv",
        cursor_name=_pixiv_sync_cursor_name(input_data),
        cursor_value=collect_summary.get("next_max_bookmark_id"),
        metadata={
            "items": len(items),
            "user_id": collect_result.data.get("user_id"),
            "tag": input_data.get("tag"),
        },
    )


def _pixiv_sync_cursor_name(input_data: dict[str, Any]) -> str:
    parts = ["bookmarks", str(input_data.get("restrict", "public"))]
    media_types = input_data.get("media_types")
    if media_types:
        parts.append("-".join(sorted(str(media_type) for media_type in media_types)))
    return ":".join(parts)


def _planned_downloads(
    target_dir: Path,
    items: list[dict[str, Any]],
    *,
    include_platform_layer: bool,
) -> list[dict[str, Any]]:
    planned = []
    for item in items:
        for file_info in _item_files(item):
            plan = plan_storage_path(
                library_root=target_dir,
                item=item,
                file_info=file_info,
                include_platform_layer=include_platform_layer,
            )
            planned.append(
                {
                    "platform": item["platform"],
                    "remote_id": item["remote_id"],
                    "kind": file_info.get("kind"),
                    "page": file_info.get("page", 0),
                    "relative_path": plan.relative_path.as_posix(),
                    "target_path": str(plan.final_path),
                    "partial_path": str(plan.partial_path),
                    "layout": plan.layout,
                    "date_source": plan.date_source,
                }
            )
    return planned


def _planned_comic_packages(
    target_dir: Path,
    items: list[dict[str, Any]],
    *,
    include_platform_layer: bool,
) -> list[dict[str, Any]]:
    packages = []
    for item in items:
        if not _is_comic_item(item):
            continue
        relative_path = comic_archive_relative_path(
            item=item,
            include_platform_layer=include_platform_layer,
        )
        packages.append(
            {
                "platform": item["platform"],
                "remote_id": item["remote_id"],
                "page_count": len(_item_files(item)),
                "relative_path": relative_path.as_posix(),
                "target_path": str((target_dir / relative_path).resolve()),
                "layout": CBZ_STORAGE_LAYOUT,
            }
        )
    return packages


def _is_comic_item(item: dict[str, Any]) -> bool:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    pixiv_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
    work_type = metadata.get("work_type") or pixiv_metadata.get("work_type")
    if work_type:
        return str(work_type) == "comic"
    return pixiv_parser.pixiv_work_type(
        metadata.get("pixiv_type") or pixiv_metadata.get("pixiv_type")
    ) == "comic"


async def _sync_one_pixiv_item(
    context: ToolContext,
    db_path: Path,
    target_dir: Path,
    item: dict[str, Any],
    input_data: dict[str, Any],
    *,
    include_platform_layer: bool,
) -> dict[str, Any]:
    platform = item["platform"]
    remote_id = item["remote_id"]
    files = _item_files(item)
    result = {
        "platform": platform,
        "remote_id": remote_id,
        "status": "queued",
        "files_total": len(files),
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "artifacts": [],
        "warnings": [],
        "errors": [],
    }
    if not files:
        db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status="failed")
        result["status"] = "failed"
        result["errors"].append({"code": "missing_files", "message": "Media item has no downloadable files."})
        return result

    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status="downloading")
    overwrite = input_data.get("overwrite", False)
    attempts = max(1, int(input_data.get("attempts", 3)))
    timeout_seconds = float(input_data.get("timeout_seconds", 30.0))
    write_sidecar_metadata = input_data.get("write_sidecar_metadata", False)

    for file_info in files:
        overwrite_file = overwrite or _repair_file_requires_overwrite(item, file_info)
        try:
            plan = plan_storage_path(
                library_root=target_dir,
                item=item,
                file_info=file_info,
                include_platform_layer=include_platform_layer,
            )
        except PathSafetyError as exc:
            target_path = target_dir / "unsafe"
            _record_failed_file(db_path, item, file_info, target_path, "unsafe_path", str(exc))
            result["files_failed"] += 1
            result["errors"].append(_file_error(file_info, "unsafe_path", str(exc)))
            continue
        target_path = plan.final_path
        try:
            ensure_inside(target_path, [target_dir])
        except PathSafetyError as exc:
            _record_failed_file(db_path, item, file_info, target_path, "unsafe_path", str(exc))
            result["files_failed"] += 1
            result["errors"].append(_file_error(file_info, "unsafe_path", str(exc)))
            continue

        if target_path.exists() and not overwrite_file:
            if not _path_known_to_item(db_path, item, file_info, target_path):
                _record_failed_file(db_path, item, file_info, target_path, "target_conflict", "Target file already exists but is not known to Mediagent.")
                result["files_failed"] += 1
                result["errors"].append(
                    _file_error(
                        file_info,
                        "target_conflict",
                        "Target file already exists but is not known to Mediagent.",
                    )
                )
                continue
            file_record = _existing_file_record(db_path, item, file_info, plan)
            result["files_downloaded"] += 1
            result["bytes_written"] += file_record.get("size_bytes") or 0
            result["artifacts"].append(str(target_path))
            if write_sidecar_metadata:
                metadata_result = await _write_sidecar_metadata(
                    context,
                    item=item,
                    file_info=file_info,
                    target_path=target_path,
                    overwrite=False,
                )
                result["warnings"].extend(metadata_result["warnings"])
                result["errors"].extend(metadata_result["errors"])
                result["artifacts"].extend(metadata_result["artifacts"])
            continue

        download_result = await download_http(
            context,
            {
                "url": file_info["url"],
                "target_path": str(target_path),
                "overwrite": overwrite_file,
                "attempts": attempts,
                "timeout_seconds": timeout_seconds,
                "expected_mime_prefix": _expected_mime_prefix(file_info),
                "headers": {"Referer": "https://www.pixiv.net/"},
                "use_partial": True,
            },
        )
        if download_result.is_success:
            db.upsert_media_file(
                db_path,
                platform=platform,
                remote_id=remote_id,
                remote_url=file_info["url"],
                local_path=download_result.data["target_path"],
                mime_type=download_result.data.get("mime_type"),
                size_bytes=download_result.data.get("size_bytes"),
                checksum=download_result.data.get("checksum"),
                status="downloaded",
                library_relative_path=plan.relative_path.as_posix(),
                storage_layout=plan.layout,
                file_health="valid",
                source_timestamp=plan.source_timestamp,
                verified_at=datetime.now(UTC).isoformat(),
            )
            result["files_downloaded"] += 1
            result["bytes_written"] += download_result.data.get("size_bytes") or 0
            result["artifacts"].append(download_result.data["target_path"])
            if write_sidecar_metadata:
                metadata_result = await _write_sidecar_metadata(
                    context,
                    item=item,
                    file_info=file_info,
                    target_path=target_path,
                    overwrite=overwrite_file,
                )
                result["warnings"].extend(metadata_result["warnings"])
                result["errors"].extend(metadata_result["errors"])
                result["artifacts"].extend(metadata_result["artifacts"])
            continue

        _record_failed_file(
            db_path,
            item,
            file_info,
            target_path,
            download_result.error.code if download_result.error else "download_failed",
            download_result.error.message if download_result.error else "Download failed.",
        )
        result["files_failed"] += 1
        result["errors"].append(
            _file_error(
                file_info,
                download_result.error.code if download_result.error else "download_failed",
                download_result.error.message if download_result.error else "Download failed.",
            )
        )

    result["status"] = item_status_from_file_counts(
        total=result["files_total"],
        downloaded=result["files_downloaded"],
        failed=result["files_failed"],
    )
    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status=result["status"])
    return result


def _item_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    files = (item.get("metadata") or {}).get("files", [])
    return [file_info for file_info in files if isinstance(file_info, dict) and file_info.get("url")]


def _existing_file_record(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    target_path = plan.final_path
    content = target_path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    mime_type = mimetypes.guess_type(str(target_path))[0]
    return db.upsert_media_file(
        db_path,
        platform=item["platform"],
        remote_id=item["remote_id"],
        remote_url=file_info.get("url"),
        local_path=str(target_path),
        mime_type=mime_type,
        size_bytes=len(content),
        checksum=f"sha256:{checksum}",
        status="downloaded",
        library_relative_path=plan.relative_path.as_posix(),
        storage_layout=plan.layout,
        file_health="valid",
        source_timestamp=plan.source_timestamp,
        verified_at=datetime.now(UTC).isoformat(),
    )


def _path_known_to_item(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
) -> bool:
    if not db_path.exists():
        return False
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT mf.id
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            WHERE mi.platform = ?
              AND mi.remote_id = ?
              AND mf.local_path = ?
              AND (mf.remote_url = ? OR ? IS NULL)
            """,
            (
                item["platform"],
                item["remote_id"],
                str(target_path),
                file_info.get("url"),
                file_info.get("url"),
            ),
        ).fetchone()
    return row is not None


def _record_failed_file(
    db_path: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
    code: str,
    message: str,
) -> None:
    db.upsert_media_file(
        db_path,
        platform=item["platform"],
        remote_id=item["remote_id"],
        remote_url=file_info.get("url"),
        local_path=str(target_path),
        mime_type=None,
        size_bytes=None,
        checksum=None,
        status="failed",
        file_health="unknown",
    )


async def _write_sidecar_metadata(
    context: ToolContext,
    *,
    item: dict[str, Any],
    file_info: dict[str, Any],
    target_path: Path,
    overwrite: bool,
) -> dict[str, list[Any]]:
    metadata_path = target_path.with_suffix(".json")
    result = await metadata_write(
        context,
        {
            "target_path": str(metadata_path),
            "overwrite": overwrite,
            "metadata": {
                "platform": item["platform"],
                "remote_id": item["remote_id"],
                "source_url": item.get("source_url"),
                "author_id": item.get("author_id"),
                "author_name": item.get("author_name"),
                "media_type": item.get("media_type"),
                "item_metadata": item.get("metadata") or {},
                "file": file_info,
            },
        },
    )
    if result.is_success:
        return {
            "artifacts": [artifact["path"] for artifact in result.artifacts],
            "warnings": [],
            "errors": [],
        }
    if result.error and result.error.code == "target_exists":
        return {
            "artifacts": [],
            "warnings": [f"Metadata already exists: {metadata_path}"],
            "errors": [],
        }
    return {
        "artifacts": [],
        "warnings": [],
        "errors": [
            {
                "code": result.error.code if result.error else "metadata_write_failed",
                "message": result.error.message if result.error else "Metadata write failed.",
                "target_path": str(metadata_path),
            }
        ],
    }


def _file_error(file_info: dict[str, Any], code: str, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "url": file_info.get("url"),
        "kind": file_info.get("kind"),
        "page": file_info.get("page", 0),
    }


def _expected_mime_prefix(file_info: dict[str, Any]) -> str | None:
    if file_info.get("kind") == "image":
        return "image/"
    return None


def _record_pixiv_sync_run(
    db_path: Path,
    *,
    run_status: str,
    summary: dict[str, Any],
    error: dict[str, Any] | None,
) -> str:
    return db.insert_run(
        db_path,
        run_type="tool",
        name="pixiv.bookmarks.sync",
        status=run_status,
        summary=summary,
        error=error,
        dry_run=False,
    )


def _refresh_payload(
    context: ToolContext,
    refresh_token_value: str,
    credentials: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    return pixiv_auth.refresh_access_token(
        http_client=context.http_client,
        refresh_token_value=refresh_token_value,
        client_id=credentials.get("client_id") or context.env.get("PIXIV_CLIENT_ID"),
        client_secret=credentials.get("client_secret") or context.env.get("PIXIV_CLIENT_SECRET"),
    )


def _ensure_access_token(
    context: ToolContext,
    credentials: dict[str, Any],
    credential_output_path: str | None,
) -> dict[str, Any]:
    access_token = credentials.get("access_token")
    expires_at = str(credentials.get("expires_at")) if credentials.get("expires_at") else None
    if access_token and not pixiv_auth.is_expired(expires_at):
        return {
            "ok": True,
            "access_token": str(access_token),
            "user_id": credentials.get("user_id"),
        }
    refresh_token_value = credentials.get("refresh_token")
    if not refresh_token_value:
        return {
            "ok": False,
            "result": ToolResult.failure(
                "pixiv_auth_missing_credentials",
                "Pixiv access token is missing or expired, and no refresh token is configured.",
                category=ErrorCategory.AUTH,
            ),
        }
    payload, rate_limit, status_code = _refresh_payload(context, str(refresh_token_value), credentials)
    if status_code != 200:
        return {
            "ok": False,
            "result": ToolResult.failure(
                "pixiv_auth_refresh_failed",
                "Pixiv token refresh failed before bookmark collection.",
                data={"status_code": status_code, "response": payload},
                category=ErrorCategory.AUTH,
                rate_limit=rate_limit,
            ),
        }
    credential_file = _persist_credentials(context, credential_output_path, payload)
    token_data = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    user = token_data.get("user") if isinstance(token_data.get("user"), dict) else {}
    return {
        "ok": True,
        "access_token": str(token_data["access_token"]),
        "user_id": user.get("id"),
        "credential_file": credential_file,
    }


def _collect_ugoira_metadata(
    context: ToolContext,
    access_token: str,
    bookmarks_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    metadata = {}
    for illust in bookmarks_payload.get("illusts", []):
        if not isinstance(illust, dict) or illust.get("type") != "ugoira":
            continue
        illust_id = str(illust.get("id"))
        payload, _, status_code = pixiv_client.get_ugoira_metadata(
            http_client=context.http_client,
            access_token=access_token,
            illust_id=illust_id,
        )
        if status_code == 200:
            metadata[illust_id] = payload
    return metadata


def _credential_output_path(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    return input_data.get("credential_output_path") or context.env.get(pixiv_auth.PIXIV_CREDENTIALS_FILE_ENV)


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
    return pixiv_auth.write_token_payload(
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


def _credential_ref_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["source", "name"],
        "properties": {
            "source": {"type": "string", "enum": ["env", "file"]},
            "name": {"type": "string"},
            "key": {"type": "string"},
        },
    }


def _redact_authorization_code_payload(value: Any, submitted_code: str) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if str(key).lower() == "code":
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_authorization_code_payload(item, submitted_code)
        return redacted
    if isinstance(value, list):
        return [_redact_authorization_code_payload(item, submitted_code) for item in value]
    if isinstance(value, str) and submitted_code:
        return value.replace(submitted_code, "<redacted>")
    return value


def _authorization_code_value(input_data: dict[str, Any]) -> str | None:
    value = input_data.get("code")
    if value:
        return str(value)
    callback_url = input_data.get("callback_url")
    if callback_url:
        return pixiv_auth.extract_authorization_code(str(callback_url))
    return None
