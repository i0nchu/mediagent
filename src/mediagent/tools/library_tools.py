"""Library audit tools."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.storage import FILE_HEALTH_VALUES, default_library_root
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="library.file.verify",
                description="Verify known local media files without deleting or contacting source platforms.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "platform": {"type": "string"},
                        "remote_id": {"type": "string"},
                        "limit": {"type": "integer"},
                        "status": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES),
                dry_run_supported=True,
            ),
            handler=library_file_verify,
        )
    ]


async def library_file_verify(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    db_path = _db_path(context, input_data)
    if not db_path:
        return ToolResult.failure(
            "missing_db_path",
            "Provide db_path or set MEDIAGENT_DB_PATH.",
            category=ErrorCategory.VALIDATION,
        )
    if not db_path.exists():
        return ToolResult.failure(
            "missing_db",
            "Database does not exist.",
            details={"db_path": str(db_path)},
            category=ErrorCategory.DATABASE,
        )
    try:
        library_root = _library_root(context, input_data)
        ensure_inside(library_root, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if _custom_root_without_selector(context, input_data, library_root):
        return ToolResult.failure(
            "custom_library_root_requires_selector",
            "Provide platform or remote_id when verifying an explicit non-default library_root.",
            category=ErrorCategory.VALIDATION,
            details={"library_root": str(library_root)},
        )

    db.initialize_database(db_path)
    records = db.list_media_files(
        db_path,
        platform=input_data.get("platform"),
        remote_id=input_data.get("remote_id"),
        status=input_data.get("status", "downloaded"),
        limit=input_data.get("limit"),
    )
    summary = {value: 0 for value in sorted(FILE_HEALTH_VALUES)}
    checked = []
    for record in records:
        path = _record_path(record, library_root)
        try:
            ensure_inside(path, context.allowed_write_roots())
        except PathSafetyError:
            health = "corrupt"
        else:
            health = _verify_record(path, record)
        summary[health] += 1
        row = {
            "file_id": record["id"],
            "platform": record["platform"],
            "remote_id": record["remote_id"],
            "health": health,
            "relative_path": record.get("library_relative_path"),
        }
        checked.append(row)
        if not context.dry_run:
            db.update_media_file_health(db_path, file_id=record["id"], file_health=health)

    return ToolResult.success(
        {
            "db_path": str(db_path),
            "library_root": str(library_root),
            "summary": {"checked": len(records), **summary},
            "files": checked,
            "dry_run": context.dry_run,
        }
    )


def _verify_record(path: Path, record: dict[str, Any]) -> str:
    if not path.exists():
        return "missing"
    if not path.is_file():
        return "corrupt"
    expected_size = record.get("size_bytes")
    try:
        stat = path.stat()
    except OSError:
        return "unknown"
    if expected_size is not None and stat.st_size != expected_size:
        return "corrupt"
    expected_checksum = record.get("checksum")
    if expected_checksum:
        algorithm, _, expected_value = str(expected_checksum).partition(":")
        if algorithm != "sha256" or not expected_value:
            return "unknown"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_value:
            return "corrupt"
    return "valid"


def _record_path(record: dict[str, Any], library_root: Path) -> Path:
    relative = record.get("library_relative_path")
    if relative:
        return (library_root / relative).resolve()
    local_path = record.get("local_path")
    if local_path:
        return Path(local_path).expanduser().resolve()
    return (library_root / "__missing__").resolve()


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return normalize_path(str(raw_path), env=context.env, cwd=context.cwd)
    return context.db_path


def _library_root(context: ToolContext, input_data: dict[str, Any]) -> Path:
    raw_path = input_data.get("library_root")
    if raw_path:
        return normalize_path(str(raw_path), env=context.env, cwd=context.cwd)
    return default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)


def _custom_root_without_selector(context: ToolContext, input_data: dict[str, Any], library_root: Path) -> bool:
    if not input_data.get("library_root"):
        return False
    if input_data.get("platform") or input_data.get("remote_id"):
        return False
    try:
        default_root = default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)
    except PathSafetyError:
        return True
    return library_root.resolve() != default_root.resolve()
