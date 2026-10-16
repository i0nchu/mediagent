"""Inspect and explicitly retry durable Telegram external-link delivery work."""

from pathlib import Path
from typing import Any

from mediagent.core import db, link_delivery_jobs
from mediagent.core.filesystem import ensure_inside, normalize_path
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="link.backlog", description="Inspect durable Telegram external-link delivery backlog without contacting providers.",
                input_schema={"type": "object", "properties": {"db_path": {"type": "string"}}},
                output_schema={"type": "object"}, permissions=(Permission.READ_DB,), dry_run_supported=True,
            ), handler=backlog,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="link.retry", description="Grant selected failed external links another bounded delivery budget.",
                input_schema={"type": "object", "properties": {
                    "db_path": {"type": "string"},
                    "link_ids": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
                    "retry_auth": {"type": "boolean"},
                }, "required": ["link_ids"]},
                output_schema={"type": "object"}, permissions=(Permission.READ_DB, Permission.WRITE_DB),
                dry_run_supported=True,
            ), handler=retry,
        ),
    ]


def _path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    value = input_data.get("db_path") or context.env.get("MEDIAGENT_DB_PATH")
    if not value:
        return None
    path = normalize_path(str(value), env=context.env, cwd=context.cwd)
    ensure_inside(path, context.allowed_write_roots())
    return path


async def backlog(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path = _path(context, input_data)
    if path is None or not path.is_file():
        return ToolResult.failure("missing_db_path", "An existing database is required.", category=ErrorCategory.VALIDATION)
    return ToolResult.success({
        "summary": link_delivery_jobs.summary(path), "jobs": link_delivery_jobs.list_jobs(path),
        "migration_required": db.get_schema_version(path) != db.SCHEMA_VERSION,
    })


async def retry(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    path = _path(context, input_data)
    if path is None or not path.is_file():
        return ToolResult.failure("missing_db_path", "An existing database is required.", category=ErrorCategory.VALIDATION)
    if context.dry_run:
        return ToolResult.success({"dry_run": True, "link_ids": input_data["link_ids"], "would_requeue": True})
    db.initialize_database(path)
    count = link_delivery_jobs.requeue(
        path, link_ids=input_data["link_ids"], blocked_auth=bool(input_data.get("retry_auth")),
    )
    return ToolResult.success({"summary": {"requeued": count}, "next_action": "Run mediagent sync telegram to deliver due work."})
