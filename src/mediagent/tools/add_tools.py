"""Unified explicit-input intake for URLs and local media."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.tools import comic_tools, link_tools, local_import_tools, tagging_tools


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="media.add",
                description="Add one explicit URL, local media file, or directory to the managed library.",
                input_schema={
                    "type": "object",
                    "required": ["input"],
                    "properties": {
                        "input": {"type": "string", "minLength": 1},
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                        "repair": {"type": "boolean"},
                        "auto_tag": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_CREDENTIALS,
                    Permission.WRITE_CREDENTIALS,
                    Permission.NETWORK,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=media_add,
        )
    ]


async def media_add(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    raw_input = str(input_data.get("input") or "").strip()
    if not raw_input:
        return ToolResult.failure(
            "invalid_add_input",
            "Provide one URL, local media file, or directory.",
            category=ErrorCategory.VALIDATION,
        )

    if _is_http_url(raw_input):
        provider = comic_tools.comic_link_provider(raw_input)
        delegated_tool = "comic.link.sync" if provider else "link.media.sync"
        delegated_input = _url_input(raw_input, input_data, comic=provider is not None)
        if provider:
            result = await comic_tools.comic_link_sync(context, delegated_input)
            input_kind = "comic_url"
        else:
            result = await link_tools.media_sync(context, delegated_input)
            input_kind = "url"
    else:
        delegated_tool = "media.local.import"
        delegated_input = _local_input(raw_input, input_data)
        result = local_import_tools.import_local_media(context, delegated_input)
        input_kind = "local"

    result.data = {
        **result.data,
        "add": {
            "input_kind": input_kind,
            "delegated_tool": delegated_tool,
        },
    }
    return await tagging_tools.finalize_add_result(context, input_data, result)


def _url_input(raw_input: str, input_data: dict[str, Any], *, comic: bool) -> dict[str, Any]:
    repair = bool(input_data.get("repair", True))
    delegated: dict[str, Any] = {
        "url": raw_input,
        "overwrite": bool(input_data.get("overwrite", False)),
        "retry_failed": repair,
        "repair_missing_files": repair,
    }
    if not comic:
        delegated["write_sidecar_metadata"] = False
    _copy_path_overrides(input_data, delegated)
    return delegated


def _local_input(raw_input: str, input_data: dict[str, Any]) -> dict[str, Any]:
    delegated: dict[str, Any] = {"path": raw_input}
    _copy_path_overrides(input_data, delegated)
    return delegated


def _copy_path_overrides(source: dict[str, Any], target: dict[str, Any]) -> None:
    for key in ("db_path", "library_root"):
        if source.get(key):
            target[key] = source[key]


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
