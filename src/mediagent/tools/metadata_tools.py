"""Metadata writing tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.redaction import redact_secrets
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="metadata.write",
                description="Write normalized JSON metadata next to downloaded files.",
                input_schema={
                    "type": "object",
                    "required": ["metadata"],
                    "required_any": [["target_path", "target_dir"]],
                    "required_with": [{"field": "target_dir", "required": ["filename"]}],
                    "properties": {
                        "target_path": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "filename": {"type": "string"},
                        "metadata": {"type": "object"},
                        "overwrite": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_FILES,),
                dry_run_supported=True,
            ),
            handler=metadata_write,
        )
    ]


async def metadata_write(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        target_path = _target_path(context, input_data)
        ensure_inside(target_path, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    overwrite = input_data.get("overwrite", False)
    metadata = redact_secrets(input_data["metadata"])
    rendered = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    if target_path.exists() and not overwrite:
        return ToolResult.failure(
            "target_exists",
            "Metadata file already exists and overwrite is false.",
            details={"target_path": str(target_path)},
            category=ErrorCategory.VALIDATION,
        )

    if context.dry_run:
        return ToolResult.success(
            {
                "target_path": str(target_path),
                "would_write": True,
                "size_bytes": len(rendered.encode("utf-8")),
            }
        )

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(rendered, encoding="utf-8")
    return ToolResult.success(
        {
            "target_path": str(target_path),
            "size_bytes": len(rendered.encode("utf-8")),
        },
        artifacts=[{"type": "file", "path": str(target_path)}],
    )


def _target_path(context: ToolContext, input_data: dict[str, Any]) -> Path:
    if input_data.get("target_path"):
        return normalize_path(input_data["target_path"], env=context.env, cwd=context.cwd)
    if not input_data.get("target_dir") or not input_data.get("filename"):
        raise ValueError("Provide target_path or both target_dir and filename.")
    target_dir = normalize_path(input_data["target_dir"], env=context.env, cwd=context.cwd)
    return (target_dir / input_data["filename"]).resolve()
