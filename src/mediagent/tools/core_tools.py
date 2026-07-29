"""Core utility tools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import (
    PathSafetyError,
    ensure_inside,
    normalize_path,
    resolve_placeholders,
)
from mediagent.core.redaction import redact_mapping, redact_secrets
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
                name="core.env.check",
                description="Validate required environment variables and configured paths.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "required": {"type": "array", "items": {"type": "string"}},
                        "path_vars": {"type": "array", "items": {"type": "string"}},
                        "require_paths_exist": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV,),
                dry_run_supported=True,
            ),
            handler=env_check,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="core.db.init",
                description="Initialize the local SQLite database.",
                input_schema={
                    "type": "object",
                    "properties": {"db_path": {"type": "string"}},
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_FILES, Permission.WRITE_DB),
                dry_run_supported=True,
            ),
            handler=db_init,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="core.path.prepare",
                description="Resolve and prepare a safe filesystem target path.",
                input_schema={
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {"type": "string"},
                        "base_dir": {"type": "string"},
                        "allowed_roots": {"type": "array", "items": {"type": "string"}},
                        "create": {"type": "boolean"},
                        "kind": {"type": "string", "enum": ["file", "directory"]},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_ENV, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=path_prepare,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="core.run.record",
                description="Record a tool or workflow run summary.",
                input_schema={
                    "type": "object",
                    "required": ["name", "status"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "run_type": {"type": "string"},
                        "name": {"type": "string"},
                        "status": {"type": "string"},
                        "summary": {"type": "object"},
                        "error": {"type": "object"},
                        "started_at": {"type": "string"},
                        "ended_at": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=run_record,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="core.sync_cursor.get",
                description="Read a persistent platform sync cursor.",
                input_schema={
                    "type": "object",
                    "required": ["platform", "cursor_name"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "platform": {"type": "string"},
                        "cursor_name": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB,),
                dry_run_supported=True,
            ),
            handler=sync_cursor_get,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="core.sync_cursor.set",
                description="Set a persistent platform sync cursor.",
                input_schema={
                    "type": "object",
                    "required": ["platform", "cursor_name"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "platform": {"type": "string"},
                        "cursor_name": {"type": "string"},
                        "cursor_value": {"type": ["string", "null"]},
                        "metadata": {"type": "object"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.WRITE_DB,),
                dry_run_supported=True,
            ),
            handler=sync_cursor_set,
        ),
    ]


async def env_check(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    required = input_data.get("required", [])
    path_vars = input_data.get("path_vars", [])
    require_paths_exist = input_data.get("require_paths_exist", False)

    missing = [name for name in required if not context.env.get(name)]
    present = {
        name: context.env[name]
        for name in required
        if context.env.get(name)
    }
    paths: dict[str, dict[str, Any]] = {}
    path_failures: list[str] = []
    for name in path_vars:
        value = context.env.get(name)
        if not value:
            paths[name] = {"configured": False, "exists": False}
            if require_paths_exist:
                path_failures.append(name)
            continue
        path = Path(value).expanduser().resolve()
        exists = path.exists()
        paths[name] = {
            "configured": True,
            "path": str(path),
            "exists": exists,
            "is_dir": path.is_dir() if exists else False,
        }
        if require_paths_exist and not exists:
            path_failures.append(name)

    data = {
        "missing": missing,
        "present": redact_mapping(present),
        "paths": paths,
    }
    if missing or path_failures:
        return ToolResult.failure(
            "env_check_failed",
            "Required environment variables or paths are missing.",
            details={"missing": missing, "path_failures": path_failures},
            data=data,
            category=ErrorCategory.VALIDATION,
        )
    return ToolResult.success(data)


async def db_init(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
    except ValueError as exc:
        return ToolResult.failure("missing_db_path", str(exc), category=ErrorCategory.VALIDATION)

    if context.dry_run:
        return ToolResult.success(
            {
                "db_path": str(db_path),
                "would_initialize": True,
                "schema_version": db.SCHEMA_VERSION,
            }
        )

    db.initialize_database(db_path)
    return ToolResult.success(
        {
            "db_path": str(db_path),
            "initialized": True,
            "schema_version": db.get_schema_version(db_path),
        }
    )


async def path_prepare(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        base_dir = _optional_path(input_data.get("base_dir"), context)
        path = normalize_path(
            input_data["path"],
            env=context.env,
            base_dir=base_dir or context.data_dir,
            cwd=context.cwd,
        )
        roots = _allowed_roots(input_data.get("allowed_roots"), context)
        ensure_inside(path, roots)
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    kind = input_data.get("kind", "file")
    create = input_data.get("create", False)
    create_target = path if kind == "directory" else path.parent
    existed_before = create_target.exists()
    warnings: list[str] = []

    if create and not context.dry_run:
        create_target.mkdir(parents=True, exist_ok=True)
    elif create and context.dry_run and not existed_before:
        warnings.append(f"dry-run: would create {create_target}")

    return ToolResult.success(
        {
            "path": str(path),
            "kind": kind,
            "exists": path.exists(),
            "prepared": create and not context.dry_run,
            "would_create": create and context.dry_run and not existed_before,
        },
        warnings=warnings,
    )


async def run_record(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
    except ValueError as exc:
        return ToolResult.failure("missing_db_path", str(exc), category=ErrorCategory.VALIDATION)

    summary = redact_secrets(input_data.get("summary", {}))
    error = redact_secrets(input_data.get("error")) if input_data.get("error") else None

    if context.dry_run:
        return ToolResult.success(
            {
                "db_path": str(db_path),
                "would_record": True,
                "run_type": input_data.get("run_type", "tool"),
                "name": input_data["name"],
                "status": input_data["status"],
            }
        )

    db.initialize_database(db_path)
    run_id = db.insert_run(
        db_path,
        run_type=input_data.get("run_type", "tool"),
        name=input_data["name"],
        status=input_data["status"],
        summary=summary,
        error=error,
        dry_run=False,
        started_at=input_data.get("started_at") or datetime.now(UTC).isoformat(),
        ended_at=input_data.get("ended_at") or datetime.now(UTC).isoformat(),
    )
    return ToolResult.success({"db_path": str(db_path), "run_id": run_id})


async def sync_cursor_get(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
    except ValueError as exc:
        return ToolResult.failure("missing_db_path", str(exc), category=ErrorCategory.VALIDATION)
    if context.dry_run:
        return ToolResult.success(
            {
                "db_path": str(db_path),
                "platform": input_data["platform"],
                "cursor_name": input_data["cursor_name"],
                "would_read": True,
            }
        )
    db.initialize_database(db_path)
    cursor = db.get_sync_cursor(
        db_path,
        platform=input_data["platform"],
        cursor_name=input_data["cursor_name"],
    )
    return ToolResult.success(
        {
            "db_path": str(db_path),
            "cursor": cursor,
            "found": cursor is not None,
        }
    )


async def sync_cursor_set(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
    except ValueError as exc:
        return ToolResult.failure("missing_db_path", str(exc), category=ErrorCategory.VALIDATION)
    payload = {
        "platform": input_data["platform"],
        "cursor_name": input_data["cursor_name"],
        "cursor_value": input_data.get("cursor_value"),
        "metadata": redact_secrets(input_data.get("metadata", {})),
    }
    if context.dry_run:
        return ToolResult.success({"db_path": str(db_path), "would_set": payload})
    db.initialize_database(db_path)
    cursor = db.set_sync_cursor(db_path, **payload)
    return ToolResult.success({"db_path": str(db_path), "cursor": cursor})


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path:
    raw_path = input_data.get("db_path")
    if raw_path:
        return Path(resolve_placeholders(raw_path, context.env)).expanduser().resolve()
    if context.db_path:
        return context.db_path
    raise ValueError("Provide db_path or set MEDIAGENT_DB_PATH.")


def _optional_path(value: str | None, context: ToolContext) -> Path | None:
    if not value:
        return None
    return Path(resolve_placeholders(value, context.env)).expanduser().resolve()


def _allowed_roots(values: list[str] | None, context: ToolContext) -> list[Path]:
    if values:
        return [
            Path(resolve_placeholders(value, context.env)).expanduser().resolve()
            for value in values
        ]
    return context.allowed_write_roots()
