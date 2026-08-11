"""Telegram platform tools."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from mediagent.core import db
from mediagent.core.auth import CredentialRef, resolve_credential
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.links import (
    ALLOWED_MEDIA_MIME_TYPES,
    LinkSafetyPolicy,
    ResolveRequest,
    URLSafetyError,
    clean_mime,
    default_link_resolver_registry,
    extract_external_links_from_messages,
    fetch_limited_follow_redirects,
    header_value,
    int_header,
    resolution_to_media_item,
    sanitize_link_resolution_for_output,
)
from mediagent.core.storage import default_library_root, plan_storage_path, platform_library_env_name, safe_storage_segment
from mediagent.core.sync import TERMINAL_ITEM_STATUSES, item_status_from_file_counts
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.platforms.telegram import auth as telegram_auth
from mediagent.platforms.telegram import client as telegram_client
from mediagent.platforms.telegram import parser as telegram_parser
from mediagent.tools.metadata_tools import metadata_write


CHAT_TYPES = ["saved_messages", "private", "group", "supergroup", "channel"]
MEDIA_TYPES = ["photo", "video", "audio"]


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.inbox.collect_links",
                description="Collect unique external URLs from a configured Telegram inbox.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "chat": _chat_selector_schema(),
                        "chats": {"type": "array", "items": _chat_selector_schema()},
                        "after_message_id": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "full_sync": {"type": "boolean"},
                        "store_cursor": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_CREDENTIALS, Permission.READ_DB, Permission.WRITE_DB, Permission.NETWORK),
                dry_run_supported=True,
                hidden=True,
            ),
            handler=inbox_collect_links,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.inbox.sync_links",
                description="Resolve and download safe external media links from a configured Telegram inbox.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "chat": _chat_selector_schema(),
                        "chats": {"type": "array", "items": _chat_selector_schema()},
                        "after_message_id": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "full_sync": {"type": "boolean"},
                        "limit": {"type": "integer"},
                        "overwrite": {"type": "boolean"},
                        "retry_failed": {"type": "boolean"},
                        "retry_auth_skipped": {"type": "boolean"},
                        "repair_missing_files": {"type": "boolean"},
                        "attempts": {"type": "integer"},
                        "timeout_seconds": {"type": "number"},
                        "max_redirects": {"type": "integer"},
                        "max_html_bytes": {"type": "integer"},
                        "max_media_bytes": {"type": "integer"},
                        "store_cursor": {"type": "boolean"},
                        "write_sidecar_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.NETWORK,
                    Permission.READ_CREDENTIALS,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
                hidden=True,
            ),
            handler=inbox_sync_links,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.auth.login",
                description="Start or complete explicit local Telegram user-session login.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["start", "complete"]},
                        "phone_number": {"type": "string"},
                        "code": {"type": "string"},
                        "phone_code_hash": {"type": "string"},
                        "password_ref": _credential_ref_schema(),
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
                name="telegram.auth.status",
                description="Validate configured Telegram user-session credentials without exposing secrets.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                permissions=(Permission.READ_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=True,
            ),
            handler=auth_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.dialogs.list",
                description="List selectable Telegram dialogs without downloading media.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"},
                        "chat_types": {"type": "array", "items": {"type": "string", "enum": CHAT_TYPES}},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_CREDENTIALS, Permission.NETWORK),
                dry_run_supported=True,
            ),
            handler=dialogs_list,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.messages.collect",
                description="Collect media-bearing messages from explicitly selected Telegram sources.",
                input_schema={
                    "type": "object",
                    "required_any": [["chat", "chats", "message_links"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "chat": _chat_selector_schema(),
                        "chats": {"type": "array", "items": _chat_selector_schema()},
                        "message_ids": {"type": "array", "items": {"type": "integer"}},
                        "message_links": {"type": "array", "items": {"type": "string"}},
                        "after_message_id": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "media_types": {"type": "array", "items": {"type": "string", "enum": MEDIA_TYPES}},
                        "include_protected": {"type": "boolean"},
                        "extract_message_links": {"type": "boolean"},
                        "store_cursor": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_CREDENTIALS, Permission.READ_DB, Permission.WRITE_DB, Permission.NETWORK),
                dry_run_supported=True,
            ),
            handler=messages_collect,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.media.download",
                description="Download one Telegram media object to a safe local path.",
                input_schema={
                    "type": "object",
                    "required": ["target_path"],
                    "required_any": [["download_ref", "media"]],
                    "properties": {
                        "download_ref": {"type": "object"},
                        "media": {"type": "object"},
                        "target_path": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                        "expected_size_bytes": {"type": "integer"},
                        "expected_mime_type": {"type": "string"},
                        "expected_mime_prefix": {"type": "string"},
                        "timeout_seconds": {"type": "number"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_CREDENTIALS, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=media_download,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.messages.sync",
                description="Deterministically collect, deduplicate, download, and record selected Telegram media.",
                input_schema={
                    "type": "object",
                    "required_any": [["chat", "chats", "message_links"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "chat": _chat_selector_schema(),
                        "chats": {"type": "array", "items": _chat_selector_schema()},
                        "message_ids": {"type": "array", "items": {"type": "integer"}},
                        "message_links": {"type": "array", "items": {"type": "string"}},
                        "after_message_id": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "limit": {"type": "integer"},
                        "media_types": {"type": "array", "items": {"type": "string", "enum": MEDIA_TYPES}},
                        "include_protected": {"type": "boolean"},
                        "extract_message_links": {"type": "boolean"},
                        "overwrite": {"type": "boolean"},
                        "retry_failed": {"type": "boolean"},
                        "repair_missing_files": {"type": "boolean"},
                        "timeout_seconds": {"type": "number"},
                        "store_cursor": {"type": "boolean"},
                        "write_sidecar_metadata": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.NETWORK,
                    Permission.READ_CREDENTIALS,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=messages_sync,
        ),
    ]


async def auth_login(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    mode = input_data.get("mode")
    if mode is None:
        mode = "complete" if input_data.get("code") or input_data.get("phone_code_hash") else "start"
    if mode not in {"start", "complete"}:
        return ToolResult.failure(
            "telegram_auth_login_invalid_mode",
            "Telegram login mode must be start or complete.",
            category=ErrorCategory.VALIDATION,
        )
    if "password" in input_data:
        return ToolResult.failure(
            "telegram_auth_inline_password_not_supported",
            "Telegram 2FA passwords must be provided through password_ref.",
            category=ErrorCategory.VALIDATION,
        )
    if context.dry_run and not _has_telegram_fake_client(context):
        config = telegram_auth.load_config(env=context.env, cwd=context.cwd, data_dir=context.data_dir)
        missing_input = []
        if mode == "complete":
            missing_input = [field for field in ("code", "phone_code_hash") if not input_data.get(field)]
        return ToolResult.success(
            {
                "platform": "telegram",
                "mode": mode,
                "would_send_code": mode == "start",
                "would_sign_in": mode == "complete",
                "missing_input": missing_input,
                "phone_number_present": bool(input_data.get("phone_number") or config.phone_number),
                "config": config.safe_metadata(),
            }
        )
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    phone_number = input_data.get("phone_number") or config.phone_number
    if not phone_number:
        return ToolResult.failure(
            "telegram_auth_missing_phone",
            "Provide phone_number or set TELEGRAM_PHONE_NUMBER.",
            data={"platform": "telegram", "config": config.safe_metadata()},
            category=ErrorCategory.VALIDATION,
        )
    if mode == "complete":
        missing = [field for field in ("code", "phone_code_hash") if not input_data.get(field)]
        if missing:
            return ToolResult.failure(
                "telegram_auth_login_missing_code",
                "Provide code and phone_code_hash to complete Telegram login.",
                data={"platform": "telegram", "missing": missing, "config": config.safe_metadata()},
                category=ErrorCategory.VALIDATION,
            )
    if context.dry_run:
        return ToolResult.success(
            {
                "platform": "telegram",
                "mode": mode,
                "would_send_code": mode == "start",
                "would_sign_in": mode == "complete",
                "config": config.safe_metadata(),
            }
        )

    try:
        telegram_auth.prepare_session_parent(config)
        if mode == "start":
            payload = await _telegram_call(
                context,
                config,
                "telegram_auth_login_start",
                phone_number=phone_number,
            )
        else:
            password = _telegram_password(context, input_data)
            payload = await _telegram_call(
                context,
                config,
                "telegram_auth_login_complete",
                phone_number=phone_number,
                code=input_data["code"],
                phone_code_hash=input_data["phone_code_hash"],
                password=password,
            )
        telegram_auth.secure_session_file(config)
    except telegram_client.TelegramClientError as exc:
        return ToolResult.failure(
            "telegram_auth_login_failed",
            "Telegram login failed.",
            details={"exception_type": type(exc).__name__},
            data={"platform": "telegram", "mode": mode, "config": config.safe_metadata()},
            category=ErrorCategory.AUTH,
        )

    if payload.get("status") == "password_required":
        return ToolResult.failure(
            "telegram_auth_password_required",
            "Telegram account requires 2FA password. Provide password_ref to complete login.",
            data={"platform": "telegram", "mode": "complete", "password_required": True, "config": config.safe_metadata()},
            category=ErrorCategory.AUTH,
        )
    if payload.get("usable"):
        session = telegram_auth.session_from_status(payload, config=config).to_dict()
        return ToolResult.success(
            {
                "platform": "telegram",
                "mode": mode,
                "status": payload.get("status"),
                "usable": True,
                "session": session,
                "config": config.safe_metadata(),
            }
        )
    return ToolResult.success(
        {
            "platform": "telegram",
            "mode": mode,
            "status": payload.get("status"),
            "usable": False,
            "phone_code_hash": payload.get("phone_code_hash"),
            "code_type": payload.get("code_type"),
            "config": config.safe_metadata(),
            "next_step": "Run telegram.auth.login again with mode=complete, code, and phone_code_hash.",
        }
    )


async def auth_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run:
        return ToolResult.success({"would_check": True, "platform": "telegram"})
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    try:
        payload = await _telegram_call(context, config, "telegram_auth_status")
    except telegram_client.TelegramClientError as exc:
        return ToolResult.failure(
            "telegram_auth_status_failed",
            str(exc),
            data={"platform": "telegram", "config": config.safe_metadata()},
            category=ErrorCategory.AUTH,
        )
    session = telegram_auth.session_from_status(payload, config=config).to_dict()
    data = {
        "platform": "telegram",
        "usable": bool(payload.get("usable")),
        "status": payload.get("status"),
        "session": session,
        "config": config.safe_metadata(),
    }
    if payload.get("usable"):
        return ToolResult.success(data)
    return ToolResult.failure(
        "telegram_auth_unusable",
        "Configured Telegram session is not usable.",
        data=data,
        category=ErrorCategory.AUTH,
    )


async def dialogs_list(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    if context.dry_run and not _has_telegram_fake_client(context):
        return ToolResult.success(
            {
                "platform": "telegram",
                "would_list": True,
                "limit": input_data.get("limit"),
                "chat_types": input_data.get("chat_types") or [],
            }
        )
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    try:
        payload = await _telegram_call(
            context,
            config,
            "telegram_list_dialogs",
            limit=_positive_int(input_data.get("limit"), default=50),
            chat_types=input_data.get("chat_types"),
        )
    except telegram_client.TelegramClientError as exc:
        return ToolResult.failure(
            "telegram_dialogs_list_failed",
            str(exc),
            category=ErrorCategory.NETWORK,
        )
    dialogs = payload.get("dialogs", []) if isinstance(payload, dict) else []
    return ToolResult.success(
        {
            "platform": "telegram",
            "dialogs": dialogs,
            "summary": {"dialogs": len(dialogs), **(payload.get("summary", {}) if isinstance(payload, dict) else {})},
        }
    )


async def messages_collect(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    return await _messages_collect(context, input_data, allow_cursor_store=not context.dry_run)


async def media_download(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        target_path = normalize_path(input_data["target_path"], env=context.env, cwd=context.cwd)
        ensure_inside(target_path, context.allowed_write_roots())
        partial_path = target_path.with_name(target_path.name + ".partial")
        ensure_inside(partial_path, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    overwrite = input_data.get("overwrite", False)
    if target_path.exists() and not overwrite:
        return ToolResult.failure(
            "target_exists",
            "Target file already exists and overwrite is false.",
            details={"target_path": str(target_path)},
            category=ErrorCategory.VALIDATION,
        )
    try:
        download_ref = _download_ref(input_data)
    except ValueError as exc:
        return ToolResult.failure(
            "telegram_download_missing_ref",
            str(exc),
            category=ErrorCategory.VALIDATION,
        )
    expected_size = input_data.get("expected_size_bytes") or _media_file(input_data).get("size_bytes")
    expected_mime_type = input_data.get("expected_mime_type") or _media_file(input_data).get("mime_type")
    expected_mime_prefix = input_data.get("expected_mime_prefix") or _expected_mime_prefix(_media_file(input_data))

    if context.dry_run:
        return ToolResult.success(
            {
                "platform": "telegram",
                "target_path": str(target_path),
                "partial_path": str(partial_path),
                "would_download": True,
                "download_ref": _safe_download_ref(download_ref),
            }
        )
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    timeout_seconds = float(input_data.get("timeout_seconds", 30.0))
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if partial_path.exists():
            partial_path.unlink()
        payload = await _telegram_call(
            context,
            config,
            "telegram_download_media",
            download_ref=download_ref,
            target_path=str(target_path),
            partial_path=str(partial_path),
            timeout_seconds=timeout_seconds,
        )
    except asyncio.CancelledError:
        _remove_partial(partial_path)
        raise
    except telegram_client.TelegramClientError as exc:
        _remove_partial(partial_path)
        return ToolResult.failure(
            "telegram_download_failed",
            str(exc),
            category=ErrorCategory.NETWORK,
        )
    try:
        mime_type = _payload_mime_type(payload) or expected_mime_type
        _materialize_download_payload(payload, partial_path, context.allowed_write_roots())
    except ValueError as exc:
        _remove_partial(partial_path)
        return ToolResult.failure(
            "telegram_download_invalid_payload",
            str(exc),
            category=ErrorCategory.NETWORK,
        )
    detected_mime = mime_type or mimetypes.guess_type(str(target_path))[0]
    validation_error = _validate_download_file(partial_path, detected_mime, expected_size, expected_mime_prefix)
    if validation_error:
        _remove_partial(partial_path)
        return ToolResult.failure(
            "telegram_download_validation_failed",
            validation_error,
            category=ErrorCategory.NETWORK,
        )
    try:
        checksum, size_bytes = _hash_file(partial_path)
        partial_path.replace(target_path)
    except Exception:
        _remove_partial(partial_path)
        raise
    return ToolResult.success(
        {
            "platform": "telegram",
            "target_path": str(target_path),
            "partial_path": str(partial_path),
            "finalized": True,
            "size_bytes": size_bytes,
            "checksum": f"sha256:{checksum}",
            "mime_type": detected_mime,
        },
        artifacts=[{"type": "file", "path": str(target_path)}],
    )


async def messages_sync(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    try:
        target_dir, include_platform_layer = _sync_target_dir(context, input_data)
        ensure_inside(target_dir, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    if context.dry_run and not _has_telegram_fake_client(context):
        return ToolResult.success(
            {
                "platform": "telegram",
                "target_dir": str(target_dir),
                "would_sync": True,
                "summary": {
                    "collected": 0,
                    "discovered": 0,
                    "queued": 0,
                    "target_dir": str(target_dir),
                },
            }
        )

    collect_input = dict(input_data)
    collect_input["db_path"] = str(db_path)
    collect_input["store_cursor"] = False
    collect_result = await _messages_collect(context, collect_input, allow_cursor_store=False)
    if not collect_result.is_success:
        return collect_result
    collected_items = collect_result.data.get("items", [])
    _apply_message_link_provenance(
        collected_items,
        input_data.get("_message_link_provenance") or [],
    )
    items = _limited_items(collected_items, input_data.get("limit"))

    statuses = db.get_media_statuses(db_path, items)
    items_to_sync, candidate_summary = _sync_candidates(
        items,
        statuses,
        db_path=db_path,
        retry_failed=input_data.get("retry_failed", False),
        repair_missing_files=input_data.get("repair_missing_files", False),
    )
    if context.dry_run:
        return ToolResult.success(
            {
                "platform": "telegram",
                "target_dir": str(target_dir),
                "summary": {
                    "collected": len(collected_items),
                    "discovered": len(items),
                    "queued": len(items_to_sync),
                    "skipped": candidate_summary["skipped_items"],
                    **candidate_summary,
                    "target_dir": str(target_dir),
                    "cursor_stored": False,
                    "cursor_reason": "dry_run",
                },
                "planned_downloads": _planned_downloads(
                    target_dir,
                    items_to_sync,
                    include_platform_layer=include_platform_layer,
                ),
                "source_summaries": collect_result.data.get("source_summaries", []),
                "message_links": collect_result.data.get("message_links", []),
            }
        )

    db.initialize_database(db_path)
    for item in items:
        db.upsert_media_item(db_path, item)
    statuses = db.get_media_statuses(db_path, items)
    items_to_sync, candidate_summary = _sync_candidates(
        items,
        statuses,
        db_path=db_path,
        retry_failed=input_data.get("retry_failed", False),
        repair_missing_files=input_data.get("repair_missing_files", False),
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "collected": len(collected_items),
        "discovered": len(items),
        "queued": len(items_to_sync),
        "skipped": candidate_summary["skipped_items"],
        **candidate_summary,
        "repaired": 0,
        "still_missing_files": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
        "target_dir": str(target_dir),
    }
    item_results: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    warnings: list[str] = []
    for item in items_to_sync:
        result = await _sync_one_telegram_item(
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
        if item.get("_repair"):
            if result["status"] == "downloaded":
                summary["repaired"] += 1
            summary["still_missing_files"] += result["files_failed"]
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])
        if result.get("cancelled"):
            summary["cancelled"] = True
            break

    run_status = "success"
    if summary["failed"] or summary["partial"]:
        run_status = "partial" if summary["downloaded"] else "failed"
    cursor_decision = _telegram_sync_cursor_decision(input_data, run_status=run_status)
    if cursor_decision["should_store"]:
        stored = _store_telegram_sync_cursors(
            db_path,
            input_data=input_data,
            source_summaries=collect_result.data.get("source_summaries", []),
        )
        summary["cursor_stored"] = bool(stored)
        summary["cursors"] = stored
        summary["cursor_reason"] = "stored" if stored else "no_cursor_value"
    else:
        summary["cursor_stored"] = False
        summary["cursor_reason"] = cursor_decision["reason"]
        if cursor_decision["warning"]:
            warnings.append(cursor_decision["warning"])
    _record_telegram_sync_run(db_path, run_status=run_status, summary=summary, error=None)
    data = {
        "platform": "telegram",
        "target_dir": str(target_dir),
        "summary": summary,
        "items": item_results,
        "collect_summary": collect_result.data.get("summary", {}),
        "source_summaries": collect_result.data.get("source_summaries", []),
        "message_links": collect_result.data.get("message_links", []),
    }
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings)
    return ToolResult.failure(
        "telegram_messages_sync_partial" if run_status == "partial" else "telegram_messages_sync_failed",
        "Telegram message sync finished with failed or partially downloaded items.",
        data=data,
        warnings=warnings,
        category=ErrorCategory.NETWORK,
    )


async def inbox_collect_links(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    return await _inbox_collect_links(context, input_data, allow_cursor_store=not context.dry_run)


async def inbox_sync_links(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )

    collect_input = dict(input_data)
    collect_input["db_path"] = str(db_path)
    collect_input["store_cursor"] = False
    collect_result = await _inbox_collect_links(context, collect_input, allow_cursor_store=False)
    if not collect_result.is_success:
        return collect_result

    links = _limited_items(collect_result.data.get("links", []), input_data.get("limit"))
    if input_data.get("retry_auth_skipped") and db_path.exists():
        if context.dry_run:
            auth_links = db.list_auth_skipped_links(
                db_path,
                limit=input_data.get("limit"),
                ingest_platform="telegram",
            )
        else:
            auth_links = db.claim_auth_skipped_links(
                db_path,
                limit=input_data.get("limit"),
                lease_owner=context.run_id,
                ingest_platform="telegram",
            )
        links = _dedupe_link_records(
            links + [{**link, "_auth_retry": True} for link in auth_links]
        )
    telegram_message_links = _limited_items(
        collect_result.data.get("telegram_message_links", []),
        input_data.get("limit"),
    )
    policy = _link_safety_policy(input_data)
    resolver_request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
    )
    resolutions: list[dict[str, Any]] = []
    resolved_items: list[dict[str, Any]] = []
    summary = {
        "links_collected": (
            collect_result.data.get("summary", {}).get("links_found", len(links))
            + collect_result.data.get("summary", {}).get(
                "telegram_message_links_found", len(telegram_message_links)
            )
        ),
        "links_considered": len(links) + len(telegram_message_links),
        "external_links_considered": len(links),
        "telegram_links_considered": len(telegram_message_links),
        "auth_links_retried": sum(1 for link in links if link.get("_auth_retry")),
        "resolved": 0,
        "skipped_links": 0,
        "queued": 0,
        "skipped_items": 0,
        "skipped_healthy": 0,
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
        "repaired": 0,
        "still_missing_files": 0,
        "downloaded": 0,
        "partial": 0,
        "failed": 0,
        "files_downloaded": 0,
        "files_failed": 0,
        "bytes_written": 0,
    }
    warnings: list[str] = []
    for link in links:
        resolution = default_link_resolver_registry().resolve(link["original_url"], request=resolver_request)
        resolutions.append({"link": _safe_link_record(link), "resolution": sanitize_link_resolution_for_output(resolution)})
        if resolution.get("status") != "resolved":
            summary["skipped_links"] += 1
            if not context.dry_run and link.get("id") is not None:
                db.update_link_resolution(
                    db_path,
                    link_id=int(link["id"]),
                    status="skipped",
                    resolution=resolution,
                    skip_reason=resolution.get("skip_reason"),
                )
            continue
        summary["resolved"] += 1
        item = resolution_to_media_item(resolution, ingest_provenance=_link_ingest_provenance(link))
        if item is None:
            summary["skipped_links"] += 1
            continue
        resolved_items.append(item)
        if not context.dry_run and link.get("id") is not None:
            db.update_link_resolution(
                db_path,
                link_id=int(link["id"]),
                status="resolved",
                resolution=resolution,
                skip_reason=None,
            )

    telegram_result: ToolResult | None = None
    telegram_planned_downloads: list[dict[str, Any]] = []
    telegram_item_results: list[dict[str, Any]] = []
    telegram_artifacts: list[dict[str, str]] = []
    telegram_link_results: list[dict[str, Any]] = []
    telegram_summary: dict[str, Any] = {}
    if telegram_message_links:
        telegram_input = dict(input_data)
        for key in ("chat", "chats", "after_message_id", "max_messages", "full_sync"):
            telegram_input.pop(key, None)
        telegram_input["message_links"] = [link["original_url"] for link in telegram_message_links]
        telegram_input["store_cursor"] = False
        telegram_input["_message_link_provenance"] = telegram_message_links
        telegram_result = await messages_sync(context, telegram_input)
        if not telegram_result.is_success and not telegram_result.data:
            return telegram_result
        telegram_summary = telegram_result.data.get("summary", {})
        telegram_link_results = telegram_result.data.get("message_links", [])
        telegram_planned_downloads = telegram_result.data.get("planned_downloads", [])
        telegram_item_results = telegram_result.data.get("items", [])
        telegram_artifacts = list(telegram_result.artifacts)
        warnings.extend(telegram_result.warnings)
        summary["resolved"] += sum(1 for result in telegram_link_results if result.get("status") == "resolved")
        telegram_skipped = sum(1 for result in telegram_link_results if result.get("status") != "resolved")
        summary["skipped_links"] += telegram_skipped
        resolutions.extend(
            {
                "link": _safe_link_record(link),
                "resolution": {
                    "status": result.get("status"),
                    "resolver": "telegram_message_link",
                    "source_url": result.get("url"),
                    "skip_reason": result.get("skip_reason"),
                    "details": {"items": result.get("items", 0)},
                },
            }
            for link, result in zip(telegram_message_links, telegram_link_results, strict=False)
        )

    if context.dry_run:
        statuses = db.get_media_statuses(db_path, resolved_items)
        items_to_sync, candidate_summary = _sync_candidates(
            resolved_items,
            statuses,
            db_path=db_path,
            retry_failed=input_data.get("retry_failed", False),
            repair_missing_files=input_data.get("repair_missing_files", False),
            link_items=True,
        )
        summary.update(candidate_summary)
        _merge_telegram_sync_summary(summary, telegram_summary)
        planned_downloads = []
        for item in items_to_sync:
            planned_downloads.extend(_planned_link_downloads(context, input_data, item))
        planned_downloads.extend(telegram_planned_downloads)
        summary["queued"] += len(items_to_sync)
        return ToolResult.success(
            {
                "platform": "telegram",
                "db_path": str(db_path),
                "summary": {**summary, "cursor_stored": False, "cursor_reason": "dry_run"},
                "links": resolutions,
                "planned_downloads": planned_downloads,
            },
            warnings=warnings,
        )

    db.initialize_database(db_path)
    for item in resolved_items:
        db.upsert_media_item(db_path, item)
    statuses = db.get_media_statuses(db_path, resolved_items)
    items_to_sync, candidate_summary = _sync_candidates(
        resolved_items,
        statuses,
        db_path=db_path,
        retry_failed=input_data.get("retry_failed", False),
        repair_missing_files=input_data.get("repair_missing_files", False),
        link_items=True,
    )
    summary.update(candidate_summary)
    _merge_telegram_sync_summary(summary, telegram_summary)
    summary["queued"] += len(items_to_sync)

    item_results: list[dict[str, Any]] = list(telegram_item_results)
    artifacts: list[dict[str, str]] = list(telegram_artifacts)
    for item in items_to_sync:
        result = await _sync_one_link_item(context, db_path, item, input_data)
        item_results.append(result)
        summary[result["status"]] += 1
        summary["files_downloaded"] += result["files_downloaded"]
        summary["files_failed"] += result["files_failed"]
        summary["bytes_written"] += result["bytes_written"]
        if item.get("_repair"):
            if result["status"] == "downloaded":
                summary["repaired"] += 1
            summary["still_missing_files"] += result["files_failed"]
        artifacts.extend({"type": "file", "path": path} for path in result["artifacts"])
        warnings.extend(result["warnings"])

    run_status = "success"
    if summary["failed"] or summary["partial"]:
        run_status = "partial" if summary["downloaded"] else "failed"
    cursor_decision = _telegram_link_cursor_decision(input_data, run_status=run_status)
    if cursor_decision["should_store"]:
        stored = _store_telegram_link_cursors(
            db_path,
            input_data=input_data,
            source_summaries=collect_result.data.get("source_summaries", []),
        )
        summary["cursor_stored"] = bool(stored)
        summary["cursors"] = stored
        summary["cursor_reason"] = "stored" if stored else "no_cursor_value"
    else:
        summary["cursor_stored"] = False
        summary["cursor_reason"] = cursor_decision["reason"]
        if cursor_decision["warning"]:
            warnings.append(cursor_decision["warning"])
    _record_telegram_link_sync_run(db_path, run_status=run_status, summary=summary, error=None)
    data = {
        "platform": "telegram",
        "db_path": str(db_path),
        "summary": summary,
        "links": resolutions,
        "items": item_results,
        "telegram_message_links": telegram_link_results,
    }
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts, warnings=warnings)
    return ToolResult.failure(
        "telegram_inbox_sync_links_partial" if run_status == "partial" else "telegram_inbox_sync_links_failed",
        "Telegram inbox link sync finished with failed or partially downloaded items.",
        data=data,
        warnings=warnings,
        category=ErrorCategory.NETWORK,
    )


async def _inbox_collect_links(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    allow_cursor_store: bool,
) -> ToolResult:
    db_path = _db_path(context, input_data)
    chats = _inbox_chat_selectors(context, input_data)
    if not chats:
        return ToolResult.failure(
            "missing_telegram_inbox_chat",
            "Provide chat/chats or set MEDIAGENT_TELEGRAM_INBOX_CHAT, MEDIAGENT_TELEGRAM_INBOX_CHAT_ID, or MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME.",
            category=ErrorCategory.VALIDATION,
        )
    if context.dry_run and not _has_telegram_fake_client(context):
        return ToolResult.success(
            {
                "platform": "telegram",
                "would_collect_links": True,
                "chats": [_safe_chat_selector(chat) for chat in chats],
            }
        )
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    if not context.dry_run:
        db.initialize_database(db_path)
    after_by_source = _link_after_by_source(db_path, chats, input_data)
    try:
        payload = await _telegram_call(
            context,
            config,
            "telegram_collect_messages",
            chats=chats,
            after_by_source=after_by_source,
            limit=_message_scan_limit(input_data, allow_full_sync=True),
            message_ids_by_source={},
            message_links=[],
            include_protected=False,
        )
    except telegram_client.TelegramClientError as exc:
        return ToolResult.failure(
            "telegram_inbox_collect_links_failed",
            str(exc),
            category=ErrorCategory.NETWORK,
        )
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    source_summaries = payload.get("source_summaries", []) if isinstance(payload, dict) else []
    discovered = extract_external_links_from_messages(messages)
    telegram_message_links = telegram_parser.extract_message_link_records(messages)
    queued_links: list[dict[str, Any]] = []
    seen: set[str] = set()
    duplicate_count = 0
    for link in discovered:
        if link["normalized_url"] in seen:
            duplicate_count += 1
            continue
        seen.add(link["normalized_url"])
        record = {**link, "collector_run_id": context.run_id}
        if context.dry_run:
            queued_links.append({**record, "id": None, "is_new": None})
            continue
        queued_links.append(db.upsert_link(db_path, record))
    telegram_message_links = [
        {**link, "collector_run_id": context.run_id}
        for link in telegram_message_links
    ]
    stored_cursors: list[dict[str, Any]] = []
    if input_data.get("store_cursor", True) and allow_cursor_store and input_data.get("after_message_id") is None:
        stored_cursors = _store_telegram_link_cursors(
            db_path,
            input_data=input_data,
            source_summaries=source_summaries,
        )
    return ToolResult.success(
        {
            "platform": "telegram",
            "db_path": str(db_path),
            "links": queued_links,
            "telegram_message_links": telegram_message_links,
            "summary": {
                "messages_scanned": len(messages),
                "links_found": len(discovered),
                "links_queued": len(queued_links),
                "telegram_message_links_found": len(telegram_message_links),
                "duplicates_in_run": duplicate_count,
                "cursor_stored": bool(stored_cursors),
            },
            "source_summaries": source_summaries,
            "cursors": stored_cursors,
        }
    )


async def _messages_collect(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    allow_cursor_store: bool,
) -> ToolResult:
    db_path = _db_path(context, input_data)
    chats = _chat_selectors(input_data)
    if context.dry_run and not _has_telegram_fake_client(context):
        return ToolResult.success(
            {
                "platform": "telegram",
                "would_collect": True,
                "chats": [_safe_chat_selector(chat) for chat in chats],
                "message_links": input_data.get("message_links", []),
                "media_types": input_data.get("media_types") or MEDIA_TYPES,
                "extract_message_links": input_data.get("extract_message_links", False),
            }
        )
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    config_result = _validated_config(context)
    if isinstance(config_result, ToolResult):
        return config_result
    config = config_result
    if not context.dry_run:
        db.initialize_database(db_path)
    after_by_source = _after_by_source(db_path, chats, input_data)
    message_ids_by_source = _message_ids_by_source(chats, input_data)
    try:
        payload = await _telegram_call(
            context,
            config,
            "telegram_collect_messages",
            chats=chats,
            after_by_source=after_by_source,
            limit=_message_scan_limit(input_data, allow_full_sync=False),
            message_ids_by_source=message_ids_by_source,
            message_links=input_data.get("message_links") or [],
            include_protected=input_data.get("include_protected", False),
        )
    except telegram_client.TelegramClientError as exc:
        return ToolResult.failure(
            "telegram_messages_collect_failed",
            str(exc),
            category=ErrorCategory.NETWORK,
        )
    messages = payload.get("messages", []) if isinstance(payload, dict) else []
    source_summaries = payload.get("source_summaries", []) if isinstance(payload, dict) else []
    extracted_message_links: list[str] = []
    linked_messages_count = 0
    if input_data.get("extract_message_links"):
        extracted_message_links = _new_message_links(
            telegram_parser.extract_message_links(messages),
            input_data.get("message_links") or [],
        )
        if extracted_message_links:
            try:
                linked_payload = await _telegram_call(
                    context,
                    config,
                    "telegram_collect_messages",
                    chats=[],
                    after_by_source={},
                    limit=_message_scan_limit(input_data, allow_full_sync=False),
                    message_ids_by_source={},
                    message_links=extracted_message_links,
                    include_protected=input_data.get("include_protected", False),
                )
            except telegram_client.TelegramClientError as exc:
                return ToolResult.failure(
                    "telegram_message_link_collect_failed",
                    str(exc),
                    category=ErrorCategory.NETWORK,
                )
            linked_messages = linked_payload.get("messages", []) if isinstance(linked_payload, dict) else []
            linked_messages_count = len(linked_messages)
            messages.extend(linked_messages)
            for source in linked_payload.get("source_summaries", []) if isinstance(linked_payload, dict) else []:
                source["cursor_eligible"] = False
                source_summaries.append(source)
    items, parser_summary = telegram_parser.normalize_messages(
        messages,
        media_types=input_data.get("media_types"),
        include_protected=input_data.get("include_protected", False),
    )
    stored_cursors: list[dict[str, Any]] = []
    if input_data.get("store_cursor", False) and allow_cursor_store and not _has_explicit_message_selection(input_data):
        db.initialize_database(db_path)
        stored_cursors = _store_telegram_sync_cursors(
            db_path,
            input_data=input_data,
            source_summaries=source_summaries,
        )
    return ToolResult.success(
        {
            "platform": "telegram",
            "db_path": str(db_path),
            "items": items,
            "message_links": _message_link_results(
                input_data.get("message_links") or [],
                messages,
                items,
                source_summaries,
                include_protected=input_data.get("include_protected", False),
            ),
            "summary": {
                **parser_summary,
                "sources": len(source_summaries),
                "extracted_message_links": len(extracted_message_links),
                "linked_messages": linked_messages_count,
                "cursor_stored": bool(stored_cursors),
            },
            "source_summaries": source_summaries,
            "cursors": stored_cursors,
        }
    )


async def _sync_one_telegram_item(
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
    write_sidecar_metadata = input_data.get("write_sidecar_metadata", False)

    for file_info in files:
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
        if target_path.exists() and not overwrite:
            if not _path_known_to_item(db_path, item, file_info, target_path):
                _record_failed_file(
                    db_path,
                    item,
                    file_info,
                    target_path,
                    "target_conflict",
                    "Target file already exists but is not known to Mediagent.",
                )
                result["files_failed"] += 1
                result["errors"].append(
                    _file_error(file_info, "target_conflict", "Target file already exists but is not known to Mediagent.")
                )
                continue
            file_record = _existing_file_record(db_path, item, file_info, plan)
            result["files_downloaded"] += 1
            result["bytes_written"] += file_record.get("size_bytes") or 0
            result["artifacts"].append(str(target_path))
            continue
        try:
            download_result = await media_download(
                context,
                {
                    "download_ref": file_info["download_ref"],
                    "target_path": str(target_path),
                    "overwrite": overwrite,
                    "expected_size_bytes": file_info.get("size_bytes"),
                    "expected_mime_type": file_info.get("mime_type"),
                    "expected_mime_prefix": _expected_mime_prefix(file_info),
                    "timeout_seconds": input_data.get("timeout_seconds", 30.0),
                },
            )
        except asyncio.CancelledError:
            code = "telegram_download_cancelled"
            message = "Telegram media download was cancelled."
            _record_failed_file(db_path, item, file_info, target_path, code, message)
            result["files_failed"] += 1
            result["errors"].append(_file_error(file_info, code, message))
            result["warnings"].append("Telegram sync stopped because a media download was cancelled.")
            result["cancelled"] = True
            break
        if download_result.is_success:
            db.upsert_media_file(
                db_path,
                platform=platform,
                remote_id=remote_id,
                remote_url=_file_remote_url(file_info),
                local_path=download_result.data["target_path"],
                mime_type=download_result.data.get("mime_type") or file_info.get("mime_type"),
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
                    overwrite=overwrite,
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


async def _sync_one_link_item(
    context: ToolContext,
    db_path: Path,
    item: dict[str, Any],
    input_data: dict[str, Any],
) -> dict[str, Any]:
    platform = item["platform"]
    remote_id = item["remote_id"]
    files = _link_item_files(item)
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
        result["errors"].append({"code": "missing_files", "message": "Resolved link item has no downloadable files."})
        return result
    db.update_media_item_status(db_path, platform=platform, remote_id=remote_id, status="downloading")
    overwrite = input_data.get("overwrite", False)
    write_sidecar_metadata = input_data.get("write_sidecar_metadata", False)

    for file_info in files:
        try:
            target_dir, include_platform_layer = _link_target_dir_for_item(context, input_data, item)
            ensure_inside(target_dir, context.allowed_write_roots())
            plan = plan_storage_path(
                library_root=target_dir,
                item=item,
                file_info=file_info,
                include_platform_layer=include_platform_layer,
            )
            target_path = plan.final_path
            ensure_inside(target_path, [target_dir])
        except PathSafetyError as exc:
            target_path = context.data_dir / "unsafe" if context.data_dir else Path("unsafe")
            _record_failed_link_file(db_path, item, file_info, target_path, "unsafe_path", str(exc))
            result["files_failed"] += 1
            result["errors"].append(_file_error(file_info, "unsafe_path", str(exc)))
            continue
        if target_path.exists() and not overwrite:
            if not _link_path_known_to_item(db_path, item, file_info, target_path):
                _record_failed_link_file(
                    db_path,
                    item,
                    file_info,
                    target_path,
                    "target_conflict",
                    "Target file already exists but is not known to Mediagent.",
                )
                result["files_failed"] += 1
                result["errors"].append(
                    _file_error(file_info, "target_conflict", "Target file already exists but is not known to Mediagent.")
                )
                continue
            file_record = _existing_link_file_record(db_path, item, file_info, plan)
            result["files_downloaded"] += 1
            result["bytes_written"] += file_record.get("size_bytes") or 0
            result["artifacts"].append(str(target_path))
            continue
        download_result = _download_link_file_safely(
            context,
            input_data,
            url=_link_file_download_url(file_info),
            headers=_link_file_download_headers(file_info),
            target_path=target_path,
            overwrite=overwrite,
            expected_mime_prefix=_expected_mime_prefix(file_info),
        )
        if download_result.is_success:
            db.upsert_media_file(
                db_path,
                platform=platform,
                remote_id=remote_id,
                remote_url=_link_file_remote_url(file_info),
                local_path=download_result.data["target_path"],
                mime_type=download_result.data.get("mime_type") or file_info.get("mime_type"),
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
                    overwrite=overwrite,
                )
                result["warnings"].extend(metadata_result["warnings"])
                result["errors"].extend(metadata_result["errors"])
                result["artifacts"].extend(metadata_result["artifacts"])
            continue
        _record_failed_link_file(
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


def _download_link_file_safely(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    url: str,
    headers: dict[str, str] | None = None,
    target_path: Path,
    overwrite: bool,
    expected_mime_prefix: str | None,
) -> ToolResult:
    partial_path = target_path.with_name(target_path.name + ".partial")
    try:
        ensure_inside(target_path, context.allowed_write_roots())
        ensure_inside(partial_path, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if target_path.exists() and not overwrite:
        return ToolResult.failure(
            "target_exists",
            "Target file already exists and overwrite is false.",
            details={"target_path": str(target_path)},
            category=ErrorCategory.VALIDATION,
        )

    policy = _link_safety_policy(input_data)
    request = ResolveRequest(
        http_client=context.http_client,
        policy=policy,
        env=context.env,
        cwd=context.cwd,
        allowed_write_roots=tuple(context.allowed_write_roots()),
        dry_run=context.dry_run,
    )
    try:
        response, final_url = fetch_limited_follow_redirects(
            url,
            request=request,
            max_bytes=policy.max_media_bytes + 1,
            headers=headers,
        )
    except URLSafetyError as exc:
        return ToolResult.failure(
            "unsafe_url",
            str(exc),
            details={"reason": exc.reason, **exc.details},
            category=ErrorCategory.NETWORK,
        )
    except Exception as exc:
        return ToolResult.failure(
            "download_failed",
            "Download failed during safe link GET.",
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.NETWORK,
        )
    if response.status_code in (401, 403):
        return ToolResult.failure(
            "requires_auth",
            "Download URL requires authentication.",
            details={"status_code": response.status_code},
            category=ErrorCategory.AUTH,
        )
    if not 200 <= response.status_code < 300:
        return ToolResult.failure(
            "download_failed",
            "Download failed during safe link GET.",
            details={"status_code": response.status_code},
            category=ErrorCategory.NETWORK,
        )
    content_length = int_header(response.headers, "content-length")
    if content_length is not None and content_length > policy.max_media_bytes:
        return ToolResult.failure(
            "too_large",
            "Download Content-Length exceeds the configured link media limit.",
            details={"size_bytes": content_length, "max_media_bytes": policy.max_media_bytes},
            category=ErrorCategory.NETWORK,
        )
    if len(response.content) > policy.max_media_bytes:
        return ToolResult.failure(
            "too_large",
            "Download body exceeds the configured link media limit.",
            details={"bytes_read": len(response.content), "max_media_bytes": policy.max_media_bytes},
            category=ErrorCategory.NETWORK,
        )
    mime_type = clean_mime(header_value(response.headers, "content-type"))
    final_url_suffix = Path(urlparse(final_url).path).suffix.lower()
    if mime_type not in ALLOWED_MEDIA_MIME_TYPES and final_url_suffix == ".mov":
        mime_type = "video/quicktime"
    if mime_type not in ALLOWED_MEDIA_MIME_TYPES:
        return ToolResult.failure(
            "unsupported_media_type",
            "Download Content-Type is not an allowed media MIME type.",
            details={"mime_type": mime_type},
            category=ErrorCategory.NETWORK,
        )
    if expected_mime_prefix and not mime_type.startswith(expected_mime_prefix):
        return ToolResult.failure(
            "download_validation_failed",
            f"Content type does not start with {expected_mime_prefix!r}.",
            details={"mime_type": mime_type},
            category=ErrorCategory.NETWORK,
        )
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if partial_path.exists():
            partial_path.unlink()
        partial_path.write_bytes(response.content)
        checksum, size_bytes = _hash_file(partial_path)
        partial_path.replace(target_path)
    except Exception:
        _remove_partial(partial_path)
        raise
    return ToolResult.success(
        {
            "url": url,
            "final_url": final_url,
            "target_path": str(target_path),
            "partial_path": str(partial_path),
            "finalized": True,
            "size_bytes": size_bytes,
            "checksum": f"sha256:{checksum}",
            "mime_type": mime_type,
        },
        artifacts=[{"type": "file", "path": str(target_path)}],
    )


def _validated_config(context: ToolContext) -> telegram_auth.TelegramConfig | ToolResult:
    config = telegram_auth.load_config(env=context.env, cwd=context.cwd, data_dir=context.data_dir)
    missing = telegram_auth.missing_config(config)
    if missing:
        return ToolResult.failure(
            "telegram_auth_missing_config",
            "Telegram configuration is incomplete.",
            data={"platform": "telegram", "missing": missing, "config": config.safe_metadata()},
            category=ErrorCategory.AUTH,
        )
    try:
        telegram_auth.ensure_session_path_allowed(config, context.allowed_write_roots())
    except PathSafetyError as exc:
        return ToolResult.failure(
            "unsafe_credential_path",
            str(exc),
            data={"platform": "telegram", "config": config.safe_metadata()},
            category=ErrorCategory.FILESYSTEM,
        )
    return config


async def _telegram_call(
    context: ToolContext,
    config: telegram_auth.TelegramConfig,
    method_name: str,
    **kwargs: Any,
) -> Any:
    client = telegram_client.client_from_context(context, config)
    method = getattr(client, method_name)
    return await telegram_client.maybe_await(method(config, **kwargs))


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(str(raw_path), context.env)).expanduser().resolve()
    return context.db_path


def _sync_target_dir(context: ToolContext, input_data: dict[str, Any]) -> tuple[Path, bool]:
    raw_path = input_data.get("library_root") or input_data.get("target_dir")
    if raw_path:
        return (
            normalize_path(str(raw_path), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", True),
        )
    platform_root = context.env.get(platform_library_env_name("telegram"))
    if platform_root:
        return (
            normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", False),
        )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        input_data.get("include_platform_layer", True),
    )


def _link_target_dir_for_item(
    context: ToolContext,
    input_data: dict[str, Any],
    item: dict[str, Any],
) -> tuple[Path, bool]:
    raw_path = input_data.get("library_root") or input_data.get("target_dir")
    if raw_path:
        return (
            normalize_path(str(raw_path), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", True),
        )
    platform_root = context.env.get(platform_library_env_name(item.get("platform")))
    if platform_root:
        return (
            normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
            input_data.get("include_platform_layer", False),
        )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        input_data.get("include_platform_layer", True),
    )


def _chat_selectors(input_data: dict[str, Any]) -> list[Any]:
    chats: list[Any] = []
    if input_data.get("chat") is not None:
        chats.append(input_data["chat"])
    for chat in input_data.get("chats") or []:
        if chat is not None:
            chats.append(chat)
    return chats


def _inbox_chat_selectors(context: ToolContext, input_data: dict[str, Any]) -> list[Any]:
    chats = _chat_selectors(input_data)
    if chats:
        return chats
    selector = _default_inbox_chat_selector(context.env)
    return [selector] if selector is not None else []


def _default_inbox_chat_selector(env: Mapping[str, str]) -> dict[str, Any] | str | None:
    generic = (env.get("MEDIAGENT_TELEGRAM_INBOX_CHAT") or "").strip()
    chat_id = (env.get("MEDIAGENT_TELEGRAM_INBOX_CHAT_ID") or "").strip()
    username = (env.get("MEDIAGENT_TELEGRAM_INBOX_CHAT_USERNAME") or "").strip()
    key = (env.get("MEDIAGENT_TELEGRAM_INBOX_KEY") or "").strip()
    if generic and not any((chat_id, username, key)):
        return generic
    selector: dict[str, Any] = {}
    if key:
        selector["key"] = key
    if chat_id:
        selector["id"] = chat_id
    elif username:
        selector["username"] = username
    elif generic:
        selector["id" if generic.lstrip("-").isdigit() else "username"] = generic
    return selector or None


def _message_ids_by_source(chats: list[Any], input_data: dict[str, Any]) -> dict[str, list[int]]:
    message_ids = [int(value) for value in input_data.get("message_ids") or []]
    if not message_ids:
        return {}
    return {telegram_client.source_key_for_chat(chat): message_ids for chat in chats}


def _after_by_source(db_path: Path, chats: list[Any], input_data: dict[str, Any]) -> dict[str, int | None]:
    explicit = input_data.get("after_message_id")
    after: dict[str, int | None] = {}
    for chat in chats:
        source_key = telegram_client.source_key_for_chat(chat)
        if explicit is not None:
            after[source_key] = int(explicit)
            continue
        cursor = db.get_sync_cursor(
            db_path,
            platform="telegram",
            cursor_name=_telegram_cursor_name(source_key, input_data),
        )
        after[source_key] = int(cursor["cursor_value"]) if cursor and cursor.get("cursor_value") else None
    return after


def _link_after_by_source(db_path: Path, chats: list[Any], input_data: dict[str, Any]) -> dict[str, int | None]:
    explicit = input_data.get("after_message_id")
    after: dict[str, int | None] = {}
    for chat in chats:
        source_key = telegram_client.source_key_for_chat(chat)
        if explicit is not None:
            after[source_key] = int(explicit)
            continue
        cursor = db.get_sync_cursor(
            db_path,
            platform="telegram",
            cursor_name=_telegram_link_cursor_name(source_key),
        )
        after[source_key] = int(cursor["cursor_value"]) if cursor and cursor.get("cursor_value") else None
    return after


def _sync_candidates(
    items: list[dict[str, Any]],
    statuses: dict[tuple[str, str], str],
    *,
    db_path: Path,
    retry_failed: bool,
    repair_missing_files: bool,
    link_items: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    candidates = []
    summary = {
        "skipped_items": 0,
        "skipped_healthy": 0,
        "repair_items": 0,
        "repair_files_missing": 0,
        "repair_files_corrupt": 0,
        "repair_files_unhealthy": 0,
    }
    for item in items:
        status = statuses.get((item["platform"], item["remote_id"]))
        if status == "failed" and retry_failed:
            candidates.append(item)
            continue
        if status in TERMINAL_ITEM_STATUSES:
            if status == "downloaded" and repair_missing_files:
                repair = _repair_assessment(db_path, item, link_items=link_items)
                if repair["repairable"]:
                    repair_item = dict(item)
                    repair_item["_repair"] = repair
                    candidates.append(repair_item)
                    summary["repair_items"] += 1
                    summary["repair_files_missing"] += int(repair.get("missing", 0))
                    summary["repair_files_corrupt"] += int(repair.get("corrupt", 0))
                    summary["repair_files_unhealthy"] += int(repair.get("unhealthy", 0))
                    continue
                summary["skipped_healthy"] += 1
            summary["skipped_items"] += 1
            continue
        candidates.append(item)
    return candidates, summary


def _repair_assessment(db_path: Path, item: dict[str, Any], *, link_items: bool) -> dict[str, Any]:
    records = db.list_media_files(db_path, platform=item["platform"], remote_id=item["remote_id"])
    records_by_remote = {str(record.get("remote_url") or ""): record for record in records if record.get("remote_url")}
    missing = 0
    corrupt = 0
    unhealthy = 0
    files: list[dict[str, Any]] = []
    item_files = _link_item_files(item) if link_items else _item_files(item)
    remote_url_for = _link_file_remote_url if link_items else _file_remote_url
    for file_info in item_files:
        remote_url = remote_url_for(file_info)
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
            elif status == "downloaded" and local_path and not Path(str(local_path)).exists():
                reason = "downloaded_file_missing_on_disk"
                missing += 1
            elif health not in {"valid", "unknown"}:
                reason = "unhealthy_file"
                unhealthy += 1
        if reason:
            files.append(
                {
                    "remote_url": remote_url,
                    "part": file_info.get("part"),
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


def _limited_items(items: list[dict[str, Any]], limit: Any) -> list[dict[str, Any]]:
    if limit is None:
        return items
    return items[: max(0, int(limit))]


def _dedupe_link_records(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[Any, dict[str, Any]] = {}
    for link in links:
        key = link.get("id") if link.get("id") is not None else link.get("normalized_url") or link.get("original_url")
        deduped[key] = link
    return list(deduped.values())


def _merge_telegram_sync_summary(summary: dict[str, Any], telegram_summary: dict[str, Any]) -> None:
    for key in (
        "queued",
        "skipped_items",
        "skipped_healthy",
        "repair_items",
        "repair_files_missing",
        "repair_files_corrupt",
        "repair_files_unhealthy",
        "repaired",
        "still_missing_files",
        "downloaded",
        "partial",
        "failed",
        "files_downloaded",
        "files_failed",
        "bytes_written",
    ):
        summary[key] += int(telegram_summary.get(key, 0) or 0)


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


def _planned_link_downloads(
    context: ToolContext,
    input_data: dict[str, Any],
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    planned = []
    for file_info in _link_item_files(item):
        target_dir, include_platform_layer = _link_target_dir_for_item(context, input_data, item)
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


def _item_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    files = (item.get("metadata") or {}).get("files", [])
    return [
        file_info
        for file_info in files
        if isinstance(file_info, dict) and (file_info.get("download_ref") or file_info.get("telegram_uri"))
    ]


def _link_item_files(item: dict[str, Any]) -> list[dict[str, Any]]:
    runtime_files = (item.get("_runtime") or {}).get("files")
    if isinstance(runtime_files, list) and runtime_files:
        return [
            file_info
            for file_info in runtime_files
            if isinstance(file_info, dict) and (file_info.get("url") or file_info.get("remote_url"))
        ]
    files = (item.get("metadata") or {}).get("files", [])
    return [
        file_info
        for file_info in files
        if isinstance(file_info, dict) and (file_info.get("url") or file_info.get("remote_url"))
    ]


def _download_ref(input_data: dict[str, Any]) -> dict[str, Any]:
    if isinstance(input_data.get("download_ref"), dict):
        return _validate_download_ref(input_data["download_ref"])
    file_info = _media_file(input_data)
    ref = file_info.get("download_ref")
    if isinstance(ref, dict):
        return _validate_download_ref(ref)
    raise ValueError("Provide download_ref or media.metadata.files[0].download_ref.")


def _validate_download_ref(ref: dict[str, Any]) -> dict[str, Any]:
    missing = []
    if not (ref.get("chat_username") or ref.get("chat_id")):
        missing.append("chat_username_or_chat_id")
    for field in ("message_id", "media_id"):
        if ref.get(field) in (None, ""):
            missing.append(field)
    if missing:
        raise ValueError(f"Telegram download_ref is missing required fields: {', '.join(missing)}.")
    return ref


def _media_file(input_data: dict[str, Any]) -> dict[str, Any]:
    media = input_data.get("media")
    if isinstance(media, dict):
        files = (media.get("metadata") or {}).get("files", [])
        if files and isinstance(files[0], dict):
            return files[0]
    return {}


def _payload_mime_type(payload: Any) -> str | None:
    if isinstance(payload, dict):
        return payload.get("mime_type") or payload.get("content_type")
    return None


def _materialize_download_payload(payload: Any, partial_path: Path, allowed_roots: list[Path]) -> None:
    if isinstance(payload, bytes):
        partial_path.write_bytes(payload)
        return
    if not isinstance(payload, dict):
        raise ValueError("Telegram download payload must be bytes or a mapping with content bytes or a file path.")
    content = payload.get("content")
    if isinstance(content, bytes):
        partial_path.write_bytes(content)
        return
    raw_path = payload.get("path") or payload.get("partial_path")
    if not raw_path:
        raise ValueError("Telegram download payload must include content bytes or a streamed file path.")
    source_path = Path(str(raw_path)).expanduser().resolve()
    try:
        ensure_inside(source_path, allowed_roots)
    except PathSafetyError as exc:
        raise ValueError(str(exc)) from exc
    if not source_path.exists():
        raise ValueError("Telegram streamed download path does not exist.")
    if source_path != partial_path:
        source_path.replace(partial_path)


def _validate_download_file(
    path: Path,
    mime_type: str | None,
    expected_size: Any,
    expected_mime_prefix: str | None,
) -> str | None:
    size_bytes = path.stat().st_size
    if expected_size is not None and int(expected_size) != size_bytes:
        return "Downloaded byte size does not match expected Telegram media size."
    if expected_mime_prefix and (not mime_type or not mime_type.startswith(expected_mime_prefix)):
        return f"Content type does not start with {expected_mime_prefix!r}."
    return None


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size_bytes += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size_bytes


def _remove_partial(partial_path: Path) -> None:
    try:
        if partial_path.exists():
            partial_path.unlink()
    except FileNotFoundError:
        pass


def _expected_mime_prefix(file_info: dict[str, Any]) -> str | None:
    media_type = file_info.get("media_type")
    if media_type == "photo":
        return "image/"
    if media_type == "video":
        return "video/"
    if media_type == "audio":
        return "audio/"
    return None


def _file_remote_url(file_info: dict[str, Any]) -> str:
    return file_info.get("telegram_uri") or telegram_parser.telegram_media_uri(file_info.get("download_ref") or {})


def _link_file_remote_url(file_info: dict[str, Any]) -> str:
    return str(file_info.get("remote_url") or file_info.get("url") or "")


def _link_file_download_url(file_info: dict[str, Any]) -> str:
    download_context = file_info.get("download_context")
    if isinstance(download_context, dict) and download_context.get("url"):
        return str(download_context["url"])
    return _link_file_remote_url(file_info)


def _link_file_download_headers(file_info: dict[str, Any]) -> dict[str, str] | None:
    headers: dict[str, str] = {}
    download_context = file_info.get("download_context")
    if isinstance(download_context, dict) and isinstance(download_context.get("headers"), dict):
        headers.update({str(key): str(value) for key, value in download_context["headers"].items()})
    if isinstance(file_info.get("runtime_headers"), dict):
        headers.update({str(key): str(value) for key, value in file_info["runtime_headers"].items()})
    return headers or None


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
        remote_url=_file_remote_url(file_info),
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


def _existing_link_file_record(
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
        remote_url=_link_file_remote_url(file_info),
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
                _file_remote_url(file_info),
                _file_remote_url(file_info),
            ),
        ).fetchone()
    return row is not None


def _link_path_known_to_item(
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
              AND mf.remote_url = ?
            """,
            (
                item["platform"],
                item["remote_id"],
                str(target_path),
                _link_file_remote_url(file_info),
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
        remote_url=_file_remote_url(file_info),
        local_path=str(target_path),
        mime_type=None,
        size_bytes=None,
        checksum=None,
        status="failed",
        file_health="unknown",
    )


def _record_failed_link_file(
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
        remote_url=_link_file_remote_url(file_info),
        local_path=str(target_path),
        mime_type=file_info.get("mime_type"),
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
                "remote_id": item.get("remote_id"),
                "source_url": item.get("source_url"),
                "author_id": item.get("author_id"),
                "author_name": item.get("author_name"),
                "media_type": item.get("media_type"),
                "item_metadata": sanitize_link_resolution_for_output(item.get("metadata") or {}),
                "file": sanitize_link_resolution_for_output(file_info),
            },
        },
    )
    if result.is_success:
        return {"artifacts": [artifact["path"] for artifact in result.artifacts], "warnings": [], "errors": []}
    if result.error and result.error.code == "target_exists":
        return {"artifacts": [], "warnings": [f"Metadata already exists: {metadata_path}"], "errors": []}
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
        "telegram_uri": file_info.get("telegram_uri"),
        "kind": file_info.get("kind"),
        "page": file_info.get("page", 0),
    }


def _telegram_sync_cursor_decision(input_data: dict[str, Any], *, run_status: str) -> dict[str, Any]:
    if not input_data.get("store_cursor", True):
        return {"should_store": False, "reason": "disabled", "warning": None}
    if _has_explicit_message_selection(input_data):
        return {"should_store": False, "reason": "explicit_selection", "warning": None}
    if input_data.get("after_message_id") is not None:
        return {"should_store": False, "reason": "explicit_after_message_id", "warning": None}
    if run_status != "success":
        return {
            "should_store": False,
            "reason": "run_not_successful",
            "warning": "Telegram cursors were not advanced because the sync run was not fully successful.",
        }
    return {"should_store": True, "reason": "ready", "warning": None}


def _telegram_link_cursor_decision(input_data: dict[str, Any], *, run_status: str) -> dict[str, Any]:
    if not input_data.get("store_cursor", True):
        return {"should_store": False, "reason": "disabled", "warning": None}
    if input_data.get("after_message_id") is not None:
        return {"should_store": False, "reason": "explicit_after_message_id", "warning": None}
    if run_status != "success":
        return {
            "should_store": False,
            "reason": "run_not_successful",
            "warning": "Telegram link cursors were not advanced because the sync run was not fully successful.",
        }
    return {"should_store": True, "reason": "ready", "warning": None}


def _store_telegram_sync_cursors(
    db_path: Path,
    *,
    input_data: dict[str, Any],
    source_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored = []
    for source in source_summaries:
        source_key = source.get("source_key")
        cursor_value = source.get("next_message_id")
        if source.get("cursor_eligible") is False:
            continue
        if not source_key or cursor_value in (None, ""):
            continue
        stored.append(
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name=_telegram_cursor_name(str(source_key), input_data),
                cursor_value=str(cursor_value),
                metadata={
                    "messages": source.get("messages", 0),
                    "media_types": input_data.get("media_types") or MEDIA_TYPES,
                },
            )
        )
    return stored


def _store_telegram_link_cursors(
    db_path: Path,
    *,
    input_data: dict[str, Any],
    source_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored = []
    for source in source_summaries:
        source_key = source.get("source_key")
        cursor_value = source.get("next_message_id")
        if source.get("cursor_eligible") is False:
            continue
        if not source_key or cursor_value in (None, ""):
            continue
        stored.append(
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name=_telegram_link_cursor_name(str(source_key)),
                cursor_value=str(cursor_value),
                metadata={"messages": source.get("messages", 0), "kind": "external_links"},
            )
        )
    return stored


def _telegram_cursor_name(source_key: str, input_data: dict[str, Any]) -> str:
    parts = ["messages", safe_storage_segment(source_key, max_length=80)]
    media_types = input_data.get("media_types")
    if media_types:
        parts.append("-".join(sorted(str(media_type) for media_type in media_types)))
    return ":".join(parts)


def _telegram_link_cursor_name(source_key: str) -> str:
    return f"links:{safe_storage_segment(source_key, max_length=80)}"


def _record_telegram_sync_run(
    db_path: Path,
    *,
    run_status: str,
    summary: dict[str, Any],
    error: dict[str, Any] | None,
) -> str:
    return db.insert_run(
        db_path,
        run_type="tool",
        name="telegram.messages.sync",
        status=run_status,
        summary=summary,
        error=error,
        dry_run=False,
    )


def _record_telegram_link_sync_run(
    db_path: Path,
    *,
    run_status: str,
    summary: dict[str, Any],
    error: dict[str, Any] | None,
) -> str:
    return db.insert_run(
        db_path,
        run_type="tool",
        name="telegram.inbox.sync_links",
        status=run_status,
        summary=summary,
        error=error,
        dry_run=False,
    )


def _link_safety_policy(input_data: dict[str, Any]) -> LinkSafetyPolicy:
    return LinkSafetyPolicy(
        max_redirects=max(0, int(input_data.get("max_redirects", 3))),
        timeout_seconds=float(input_data.get("timeout_seconds", 30.0)),
        max_html_bytes=max(1, int(input_data.get("max_html_bytes", 1024 * 1024))),
        max_media_bytes=max(1, int(input_data.get("max_media_bytes", 1024 * 1024 * 1024))),
    )


def _link_ingest_provenance(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "platform": link.get("ingest_platform"),
        "chat_id": link.get("source_chat_id"),
        "message_id": link.get("source_message_id"),
        "message_date": link.get("source_message_date"),
        "collector_run_id": link.get("collector_run_id"),
        "link_id": link.get("id"),
    }


def _apply_message_link_provenance(
    items: list[dict[str, Any]],
    link_records: list[dict[str, Any]],
) -> None:
    for item in items:
        matching = [
            _link_ingest_provenance(record)
            for record in link_records
            if _item_matches_message_link(item, record.get("original_url"))
        ]
        if not matching:
            continue
        metadata = dict(item.get("metadata") or {})
        metadata["ingested_from"] = matching[0]
        metadata["source_provenance"] = matching
        item["metadata"] = metadata


def _message_link_results(
    message_links: list[str],
    messages: list[dict[str, Any]],
    items: list[dict[str, Any]],
    source_summaries: list[dict[str, Any]],
    *,
    include_protected: bool,
) -> list[dict[str, Any]]:
    summaries_by_source: dict[str, list[dict[str, Any]]] = {}
    for summary in source_summaries:
        source_key = str(summary.get("source_key") or "")
        summaries_by_source.setdefault(source_key, []).append(summary)
    results = []
    for link in message_links:
        refs = telegram_client.parse_message_links([link])
        ref = refs[0] if refs else None
        matching_items = [item for item in items if _item_matches_message_link(item, link)]
        matching_messages = [message for message in messages if _message_matches_link(message, link)]
        summary = None
        if ref:
            candidates = summaries_by_source.get(str(ref["source_key"])) or []
            summary = next(
                (candidate for candidate in candidates if candidate.get("message_link") == link),
                None,
            )
            if summary is None and candidates:
                summary = candidates.pop(0)
        if matching_items:
            results.append({"url": link, "status": "resolved", "items": len(matching_items)})
            continue
        message = matching_messages[0] if matching_messages else None
        if message and message.get("protected_content") and not include_protected:
            skip_reason = "protected_content"
        elif message and (message.get("unavailable") or message.get("media_unavailable")):
            skip_reason = "inaccessible"
        elif message:
            skip_reason = "no_supported_media"
        else:
            skip_reason = (summary or {}).get("skip_reason") or "inaccessible"
        results.append({"url": link, "status": "skipped", "skip_reason": skip_reason, "items": 0})
    return results


def _item_matches_message_link(item: dict[str, Any], link: Any) -> bool:
    if not isinstance(link, str):
        return False
    if item.get("source_url") == link:
        return True
    refs = telegram_client.parse_message_links([link])
    if not refs:
        return False
    ref = refs[0]
    telegram = (item.get("metadata") or {}).get("telegram") or {}
    message_id_matches = str(telegram.get("message_id")) == str(ref["message_id"])
    chat_matches = str(telegram.get("chat_id")) == str(ref["chat"])
    username_matches = str(telegram.get("chat_username") or "").lstrip("@").lower() == str(ref["chat"]).lower()
    return message_id_matches and (chat_matches or username_matches)


def _message_matches_link(message: dict[str, Any], link: str) -> bool:
    if message.get("source_url") == link:
        return True
    refs = telegram_client.parse_message_links([link])
    if not refs:
        return False
    ref = refs[0]
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    message_id_matches = str(message.get("id") or message.get("message_id")) == str(ref["message_id"])
    chat_matches = str(chat.get("id") or message.get("chat_id")) == str(ref["chat"])
    username_matches = str(chat.get("username") or "").lstrip("@").lower() == str(ref["chat"]).lower()
    return message_id_matches and (chat_matches or username_matches)


def _safe_link_record(link: dict[str, Any]) -> dict[str, Any]:
    return {
        key: link.get(key)
        for key in (
            "id",
            "ingest_platform",
            "original_url",
            "normalized_url",
            "source_chat_id",
            "source_message_id",
            "source_message_date",
            "collector_run_id",
            "status",
            "skip_reason",
            "is_new",
            "previous_status",
        )
    }


def _has_explicit_message_selection(input_data: dict[str, Any]) -> bool:
    return bool(input_data.get("message_ids") or input_data.get("message_links"))


def _has_telegram_fake_client(context: ToolContext) -> bool:
    candidate = context.http_client
    return candidate is not None and any(
        hasattr(candidate, name)
        for name in (
            "telegram_auth_login_start",
            "telegram_auth_login_complete",
            "telegram_auth_status",
            "telegram_list_dialogs",
            "telegram_collect_messages",
            "telegram_download_media",
        )
    )


def _positive_int(value: Any, *, default: int) -> int:
    if value is None:
        return default
    return max(1, int(value))


def _message_scan_limit(input_data: dict[str, Any], *, allow_full_sync: bool) -> int | None:
    if input_data.get("max_messages") is not None:
        return max(1, int(input_data["max_messages"]))
    if allow_full_sync and input_data.get("full_sync"):
        return None
    return 100


def _safe_chat_selector(selector: Any) -> Any:
    if isinstance(selector, dict):
        return {
            key: value
            for key, value in selector.items()
            if key in {"id", "username", "alias", "key", "type"} and value not in (None, "")
        }
    return selector


def _safe_download_ref(download_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in download_ref.items()
        if key in {"chat_id", "chat_username", "message_id", "media_id", "media_index"} and value not in (None, "")
    }


def _telegram_password(context: ToolContext, input_data: dict[str, Any]) -> str | None:
    if input_data.get("password_ref"):
        return resolve_credential(
            CredentialRef.from_dict(input_data["password_ref"]),
            env=context.env,
            cwd=context.cwd,
        )
    return None


def _new_message_links(extracted: list[str], existing: list[str]) -> list[str]:
    existing_set = set(existing)
    return [link for link in extracted if link not in existing_set]


def _chat_selector_schema() -> dict[str, Any]:
    return {
        "type": ["string", "object"],
        "properties": {
            "id": {"type": ["string", "integer"]},
            "chat_id": {"type": ["string", "integer"]},
            "username": {"type": "string"},
            "alias": {"type": "string"},
            "key": {"type": "string"},
            "type": {"type": "string"},
        },
    }


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
