"""Tool boundary for Asset tags and search."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from mediagent.core import asset_tags
from mediagent.core.filesystem import PathSafetyError, ensure_inside, resolve_placeholders
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="library.asset.tags.update",
                description="Add or remove equal-priority tags on one Asset.",
                input_schema={
                    "type": "object",
                    "required": ["asset_id"],
                    "properties": {
                        "asset_id": {"type": "string", "minLength": 1},
                        "add": {"type": "array", "items": {"type": "string"}},
                        "remove": {"type": "array", "items": {"type": "string"}},
                        "db_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=False,
            ),
            handler=update_asset_tags,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="library.asset.search",
                description="Search Asset tags, metadata, sources, and filenames.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "terms": {"type": "array", "items": {"type": "string"}},
                        "include_inactive": {"type": "boolean"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                        "db_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB,),
                dry_run_supported=False,
            ),
            handler=search_assets,
        ),
    ]


def update_asset_tags(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _db_path(context, input_data, write=True)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        result = asset_tags.update_tags(
            resolved,
            asset_id=str(input_data["asset_id"]),
            add=input_data.get("add") or (),
            remove=input_data.get("remove") or (),
        )
        return ToolResult.success(result)
    except ValueError as exc:
        message = str(exc)
        return ToolResult.failure(
            "asset_not_found" if message.startswith("Unknown Asset:") else "invalid_asset_tags",
            message,
            category=ErrorCategory.VALIDATION,
        )
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return ToolResult.failure(
            "asset_tags_update_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.DATABASE,
        )


def search_assets(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _db_path(context, input_data, write=False)
    if isinstance(resolved, ToolResult):
        return resolved
    try:
        return ToolResult.success(
            asset_tags.search(
                resolved,
                terms=input_data.get("terms") or (),
                include_inactive=bool(input_data.get("include_inactive", False)),
                limit=int(input_data.get("limit", 50)),
            )
        )
    except ValueError as exc:
        return ToolResult.failure("invalid_asset_search", str(exc), category=ErrorCategory.VALIDATION)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return ToolResult.failure(
            "asset_search_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.DATABASE,
        )


def _db_path(
    context: ToolContext,
    input_data: dict[str, Any],
    *,
    write: bool,
) -> Path | ToolResult:
    raw = input_data.get("db_path")
    if raw:
        path = Path(resolve_placeholders(str(raw), context.env)).expanduser().resolve()
    elif context.db_path is not None:
        path = context.db_path
    else:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    if not path.is_file():
        return ToolResult.failure("missing_db", "Database does not exist.", category=ErrorCategory.DATABASE)
    if write:
        try:
            ensure_inside(path, context.allowed_write_roots())
        except PathSafetyError as exc:
            return ToolResult.failure("unsafe_db_path", str(exc), category=ErrorCategory.FILESYSTEM)
    return path
