"""Media database tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import resolve_placeholders
from mediagent.core.redaction import redact_secrets
from mediagent.core.sync import MEDIA_ITEM_STATUSES
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


MEDIA_ITEM_SCHEMA = {
    "type": "object",
    "required": ["platform", "remote_id", "media_type"],
    "properties": {
        "platform": {"type": "string"},
        "remote_id": {"type": "string"},
        "source_url": {"type": "string"},
        "author_id": {"type": "string"},
        "author_name": {"type": "string"},
        "media_type": {"type": "string", "enum": ["photo", "video", "audio"]},
        "status": {"type": "string"},
        "source_availability": {
            "type": "string",
            "enum": ["available", "deleted", "restricted", "unavailable", "unknown"],
        },
        "metadata": {"type": "object"},
    },
}


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="media.item.upsert",
                description="Upsert normalized media items into SQLite.",
                input_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "items": {"type": "array", "items": MEDIA_ITEM_SCHEMA},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=item_upsert,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="media.item.filter_new",
                description="Filter discovered media items to items that are not completed or skipped.",
                input_schema={
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "items": {"type": "array", "items": MEDIA_ITEM_SCHEMA},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB,),
                dry_run_supported=True,
            ),
            handler=item_filter_new,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="media.item.set_status",
                description="Set a known media item status intentionally.",
                input_schema={
                    "type": "object",
                    "required": ["platform", "remote_id", "status"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "platform": {"type": "string"},
                        "remote_id": {"type": "string"},
                        "status": {"type": "string", "enum": list(MEDIA_ITEM_STATUSES)},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=item_set_status,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="media.file.upsert",
                description="Upsert a local media file record for a known media item.",
                input_schema={
                    "type": "object",
                    "required": ["platform", "remote_id", "status"],
                    "required_any": [["remote_url", "local_path"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "platform": {"type": "string"},
                        "remote_id": {"type": "string"},
                        "remote_url": {"type": "string"},
                        "local_path": {"type": "string"},
                        "mime_type": {"type": "string"},
                        "size_bytes": {"type": "integer"},
                        "checksum": {"type": "string"},
                        "status": {"type": "string"},
                        "library_relative_path": {"type": "string"},
                        "storage_layout": {"type": "string"},
                        "file_health": {"type": "string"},
                        "source_timestamp": {"type": "string"},
                        "verified_at": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=file_upsert,
        ),
    ]


async def item_upsert(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )

    sanitized_items = [redact_secrets(item) for item in input_data["items"]]
    if context.dry_run:
        return ToolResult.success(
            {
                "db_path": str(db_path),
                "would_upsert": len(sanitized_items),
                "items": sanitized_items,
            }
        )

    db.initialize_database(db_path)
    results = [db.upsert_media_item(db_path, item) for item in sanitized_items]
    return ToolResult.success(
        {
            "db_path": str(db_path),
            "upserted": len(results),
            "items": results,
        }
    )


async def item_filter_new(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )

    items = [redact_secrets(item) for item in input_data["items"]]
    statuses = db.get_media_statuses(db_path, items)
    new_items: list[dict[str, Any]] = []
    summary = {
        "new": 0,
        "known": 0,
        "downloaded": 0,
        "failed": 0,
        "skipped": 0,
    }
    for item in items:
        status = statuses.get((item["platform"], item["remote_id"]))
        if not status:
            summary["new"] += 1
            new_items.append(item)
            continue
        if status in ("downloaded", "skipped", "failed"):
            summary[status] += 1
        else:
            summary["known"] += 1
            new_items.append(item)

    return ToolResult.success(
        {
            "db_path": str(db_path),
            "items": new_items,
            "summary": summary,
        }
    )


async def item_set_status(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    status = input_data["status"]
    record = {
        "platform": input_data["platform"],
        "remote_id": input_data["remote_id"],
        "status": status,
    }
    if context.dry_run:
        return ToolResult.success({"db_path": str(db_path), "would_update": record})
    db.initialize_database(db_path)
    try:
        result = db.update_media_item_status(db_path, **record)
    except ValueError as exc:
        return ToolResult.failure(
            "unknown_media_item",
            str(exc),
            category=ErrorCategory.DATABASE,
        )
    return ToolResult.success({"db_path": str(db_path), "item": result})


async def file_upsert(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    record = {
        "platform": input_data["platform"],
        "remote_id": input_data["remote_id"],
        "remote_url": input_data.get("remote_url"),
        "local_path": input_data.get("local_path"),
        "mime_type": input_data.get("mime_type"),
        "size_bytes": input_data.get("size_bytes"),
        "checksum": input_data.get("checksum"),
        "status": input_data["status"],
        "library_relative_path": input_data.get("library_relative_path"),
        "storage_layout": input_data.get("storage_layout"),
        "file_health": input_data.get("file_health"),
        "source_timestamp": input_data.get("source_timestamp"),
        "verified_at": input_data.get("verified_at"),
    }
    if context.dry_run:
        return ToolResult.success({"db_path": str(db_path), "would_upsert": record})
    db.initialize_database(db_path)
    try:
        result = db.upsert_media_file(db_path, **record)
    except ValueError as exc:
        return ToolResult.failure(
            "unknown_media_item",
            str(exc),
            category=ErrorCategory.DATABASE,
        )
    return ToolResult.success({"db_path": str(db_path), "file": result})


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(raw_path, context.env)).expanduser().resolve()
    return context.db_path
