"""Tool boundary for copy-only local media intake."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mediagent.core import local_import
from mediagent.core.filesystem import PathSafetyError, ensure_inside, resolve_placeholders
from mediagent.core.operational_logging import ProgressLogger
from mediagent.core.storage import default_library_root
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
                name="media.local.import",
                description="Copy an explicit local media file or directory into the managed library.",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string", "minLength": 1},
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_FILES, Permission.WRITE_FILES, Permission.WRITE_DB),
                dry_run_supported=True,
            ),
            handler=import_local_media,
        )
    ]


def import_local_media(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
        library_root = _library_root(context, input_data)
        input_path = _input_path(context, str(input_data["path"]))
        ensure_inside(db_path, context.allowed_write_roots())
        ensure_inside(library_root, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_local_import_path", str(exc), category=ErrorCategory.FILESYSTEM)

    progress = ProgressLogger(context.operation_log)
    try:
        result = local_import.import_input(
            db_path=db_path,
            library_root=library_root,
            input_path=input_path,
            configured_library_roots=_configured_library_roots(context, library_root),
            data_dir=context.data_dir,
            dry_run=context.dry_run,
            progress=lambda completed, pending, failed: progress.report(
                completed=completed,
                pending=pending,
                failed=failed,
            ),
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "local_input_not_found",
            "The local input path does not exist.",
            category=ErrorCategory.FILESYSTEM,
        )
    except (OSError, ValueError) as exc:
        return ToolResult.failure(
            "local_import_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.FILESYSTEM,
        )

    summary = result["summary"]
    data = {key: value for key, value in result.items() if key != "artifacts"}
    artifacts = [{"type": "file", "path": path} for path in result["artifacts"]]
    warnings: list[str] = []
    if summary["unsupported"]:
        warnings.append(f"Skipped {summary['unsupported']} unsupported local file(s).")
    if summary["symlinks_skipped"]:
        warnings.append(f"Skipped {summary['symlinks_skipped']} symbolic link(s).")
    if summary["excluded"]:
        warnings.append(f"Excluded {summary['excluded']} configured data or trash path(s).")
    if summary["blocked"]:
        warnings.append(f"Blocked {summary['blocked']} explicitly removed content item(s).")
    if summary["failed"]:
        return ToolResult.failure(
            "local_import_partial" if _completed_count(summary) else "local_import_failed",
            "Local media import completed with one or more failed files.",
            data=data,
            warnings=warnings,
            category=ErrorCategory.FILESYSTEM,
        )
    if summary["blocked"] and data["input_kind"] == "file":
        return ToolResult.failure(
            "local_content_removed",
            "The local content was explicitly removed; restore it instead of adding it again.",
            data=data,
            warnings=warnings,
            category=ErrorCategory.VALIDATION,
        )
    if summary["unsupported"] and data["input_kind"] == "file":
        return ToolResult.failure(
            "unsupported_local_media",
            "The local file is not a supported media type.",
            data=data,
            warnings=warnings,
            category=ErrorCategory.VALIDATION,
        )
    return ToolResult.success(data, artifacts=artifacts, warnings=warnings)


def _completed_count(summary: dict[str, Any]) -> int:
    return sum(
        int(summary.get(key) or 0)
        for key in ("imported", "adopted", "existing", "repaired")
    )


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path:
    raw = input_data.get("db_path")
    if raw:
        return Path(resolve_placeholders(str(raw), context.env)).expanduser().resolve()
    if context.db_path is None:
        raise ValueError("Provide db_path or set MEDIAGENT_DB_PATH.")
    return context.db_path


def _library_root(context: ToolContext, input_data: dict[str, Any]) -> Path:
    raw = input_data.get("library_root")
    if raw:
        return Path(resolve_placeholders(str(raw), context.env)).expanduser().resolve()
    return default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)


def _input_path(context: ToolContext, raw: str) -> Path:
    expanded = Path(resolve_placeholders(raw, context.env)).expanduser()
    if not expanded.is_absolute():
        expanded = context.cwd / expanded
    return Path(os.path.abspath(expanded))


def _configured_library_roots(context: ToolContext, primary: Path) -> list[Path]:
    roots = [primary]
    if context.library_dir is not None:
        roots.append(context.library_dir)
    elif context.data_dir is not None:
        roots.append(context.data_dir / "library")
    for name, value in context.env.items():
        if name.startswith("MEDIAGENT_") and name.endswith("_LIBRARY_DIR") and value:
            roots.append(Path(resolve_placeholders(str(value), context.env)).expanduser().resolve())
    return list({str(root.resolve()): root.resolve() for root in roots}.values())
