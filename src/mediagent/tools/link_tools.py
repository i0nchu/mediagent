"""Undocumented experimental link resolver tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import resolve_placeholders
from mediagent.core.links import (
    LinkSafetyPolicy,
    ResolveRequest,
    default_link_resolver_registry,
    resolution_to_media_item,
)
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="link.resolve.preview",
                description="Safely resolve one queued or direct URL without downloading.",
                input_schema={
                    "type": "object",
                    "required_any": [["url", "link_id"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "url": {"type": "string"},
                        "link_id": {"type": "integer"},
                        "record": {"type": "boolean"},
                        "timeout_seconds": {"type": "number"},
                        "max_redirects": {"type": "integer"},
                        "max_html_bytes": {"type": "integer"},
                        "max_media_bytes": {"type": "integer"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
                experimental=True,
            ),
            handler=resolve_preview,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="link.resolve.to_media_item",
                description="Convert one safe link resolution into a normalized media item.",
                input_schema={
                    "type": "object",
                    "required_any": [["resolution", "url", "link_id"]],
                    "properties": {
                        "db_path": {"type": "string"},
                        "resolution": {"type": "object"},
                        "url": {"type": "string"},
                        "link_id": {"type": "integer"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
                experimental=True,
            ),
            handler=resolve_to_media_item,
        ),
    ]


async def resolve_preview(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    link = None
    db_path = _db_path(context, input_data)
    if input_data.get("link_id") is not None:
        if not db_path:
            return ToolResult.failure(
                "missing_db_path",
                "Provide db_path or set MEDIAGENT_DB_PATH when using link_id.",
                category=ErrorCategory.VALIDATION,
            )
        link = db.get_link(db_path, link_id=int(input_data["link_id"]))
        if link is None:
            return ToolResult.failure(
                "unknown_link",
                "Link queue record was not found.",
                details={"link_id": input_data["link_id"]},
                category=ErrorCategory.DATABASE,
            )
        raw_url = link["original_url"]
    else:
        raw_url = input_data["url"]

    resolution = _resolve_url(context, raw_url, input_data)
    should_record = bool(input_data.get("record", input_data.get("link_id") is not None))
    if link and db_path and should_record and not context.dry_run:
        status = "resolved" if resolution.get("status") == "resolved" else "skipped"
        link = db.update_link_resolution(
            db_path,
            link_id=int(link["id"]),
            status=status,
            resolution=resolution,
            skip_reason=resolution.get("skip_reason"),
        )
    return ToolResult.success(
        {
            "resolution": resolution,
            "link": link,
            "recorded": bool(link and should_record and not context.dry_run),
        },
        warnings=resolution.get("warnings") or [],
    )


async def resolve_to_media_item(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolution = input_data.get("resolution")
    link = None
    db_path = _db_path(context, input_data)
    if not isinstance(resolution, dict):
        if input_data.get("link_id") is not None:
            if not db_path:
                return ToolResult.failure(
                    "missing_db_path",
                    "Provide db_path or set MEDIAGENT_DB_PATH when using link_id.",
                    category=ErrorCategory.VALIDATION,
                )
            link = db.get_link(db_path, link_id=int(input_data["link_id"]))
            if link is None:
                return ToolResult.failure(
                    "unknown_link",
                    "Link queue record was not found.",
                    details={"link_id": input_data["link_id"]},
                    category=ErrorCategory.DATABASE,
                )
            resolution = link.get("resolution") or {}
            if resolution.get("status") not in {"resolved", "skipped"}:
                resolution = _resolve_url(context, link["original_url"], input_data)
        else:
            resolution = _resolve_url(context, input_data["url"], input_data)

    provenance = _ingest_provenance(link)
    item = resolution_to_media_item(resolution, ingest_provenance=provenance)
    if item is None:
        return ToolResult.success(
            {
                "converted": False,
                "skip_reason": resolution.get("skip_reason"),
                "resolution": resolution,
                "link": link,
            }
        )
    return ToolResult.success(
        {
            "converted": True,
            "item": item,
            "resolution": resolution,
            "link": link,
        }
    )


def _resolve_url(context: ToolContext, raw_url: str, input_data: dict[str, Any]) -> dict[str, Any]:
    policy = LinkSafetyPolicy(
        max_redirects=max(0, int(input_data.get("max_redirects", 3))),
        timeout_seconds=float(input_data.get("timeout_seconds", 30.0)),
        max_html_bytes=max(1, int(input_data.get("max_html_bytes", 1024 * 1024))),
        max_media_bytes=max(1, int(input_data.get("max_media_bytes", 1024 * 1024 * 1024))),
    )
    request = ResolveRequest(http_client=context.http_client, policy=policy)
    return default_link_resolver_registry().resolve(raw_url, request=request)


def _ingest_provenance(link: dict[str, Any] | None) -> dict[str, Any]:
    if not link:
        return {}
    return {
        "platform": link.get("ingest_platform"),
        "chat_id": link.get("source_chat_id"),
        "message_id": link.get("source_message_id"),
        "message_date": link.get("source_message_date"),
        "collector_run_id": link.get("collector_run_id"),
        "link_id": link.get("id"),
    }


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(raw_path, context.env)).expanduser().resolve()
    return context.db_path
