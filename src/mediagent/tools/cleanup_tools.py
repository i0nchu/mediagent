"""Conservative cleanup and recovery tools for media state."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core import db
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.storage import default_library_root
from mediagent.core.sync import MEDIA_ITEM_STATUSES
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
                name="core.cleanup.media_state",
                description="Plan or apply a conservative media-state cleanup with quarantine-first file handling.",
                input_schema={
                    "type": "object",
                    "required": ["mode", "platform"],
                    "properties": {
                        "db_path": {"type": "string"},
                        "mode": {"type": "string", "enum": ["plan", "apply"]},
                        "platform": {"type": "string"},
                        "remote_id": {"type": "string"},
                        "status": {"type": "string", "enum": list(MEDIA_ITEM_STATUSES)},
                        "quarantine_dir": {"type": "string"},
                        "confirm": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(
                    Permission.READ_ENV,
                    Permission.READ_DB,
                    Permission.WRITE_DB,
                    Permission.READ_FILES,
                    Permission.WRITE_FILES,
                ),
                dry_run_supported=True,
            ),
            handler=cleanup_media_state,
        )
    ]


async def cleanup_media_state(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    selector = {
        "platform": input_data.get("platform"),
        "remote_id": input_data.get("remote_id"),
        "status": input_data.get("status"),
    }
    validation_error = _validate_selector(selector)
    if validation_error:
        return validation_error

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
        library_root = default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)
        quarantine_dir = _quarantine_dir(context, input_data)
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)

    manifest = _build_manifest(
        context=context,
        db_path=db_path,
        selector=selector,
        library_root=library_root,
        quarantine_dir=quarantine_dir,
    )
    mode = input_data["mode"]
    warnings = list(manifest["warnings"])
    dry_run_effective = context.dry_run or mode == "plan"

    if mode == "apply" and not context.dry_run and input_data.get("confirm") is not True:
        return ToolResult.failure(
            "cleanup_not_confirmed",
            "Cleanup apply requires confirm: true.",
            data={
                "mode": mode,
                "dry_run": False,
                "db_path": str(db_path),
                "selector": selector,
                "quarantine_dir": str(quarantine_dir),
                "manifest": manifest["data"],
            },
            warnings=warnings,
            category=ErrorCategory.VALIDATION,
        )

    if dry_run_effective:
        if mode == "apply" and context.dry_run:
            warnings.append("dry-run: apply mode requested but no mutations were performed")
        return ToolResult.success(
            {
                "mode": mode,
                "dry_run": True,
                "db_path": str(db_path),
                "selector": selector,
                "quarantine_dir": str(quarantine_dir),
                "manifest": manifest["data"],
            },
            warnings=warnings,
        )

    if manifest["data"]["summary"]["blocked_items"] > 0:
        return ToolResult.failure(
            "blocked_cleanup_manifest",
            "Cleanup apply is blocked because one or more selected items include protected or unsafe file paths.",
            data={
                "mode": mode,
                "dry_run": False,
                "db_path": str(db_path),
                "selector": selector,
                "quarantine_dir": str(quarantine_dir),
                "manifest": manifest["data"],
            },
            warnings=warnings,
            category=ErrorCategory.FILESYSTEM,
        )

    applied = _apply_manifest(db_path=db_path, manifest=manifest["data"], quarantine_dir=quarantine_dir)
    return ToolResult.success(
        {
            "mode": mode,
            "dry_run": False,
            "db_path": str(db_path),
            "selector": selector,
            "quarantine_dir": str(quarantine_dir),
            "manifest": applied,
        },
        warnings=warnings,
    )


def _validate_selector(selector: dict[str, Any]) -> ToolResult | None:
    platform = str(selector.get("platform") or "").strip()
    if not platform:
        return ToolResult.failure(
            "missing_selector",
            "Cleanup requires at least a platform selector.",
            category=ErrorCategory.VALIDATION,
        )
    return None


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path | None:
    raw_path = input_data.get("db_path")
    if raw_path:
        return normalize_path(str(raw_path), env=context.env, cwd=context.cwd)
    return context.db_path


def _quarantine_dir(context: ToolContext, input_data: dict[str, Any]) -> Path:
    if input_data.get("quarantine_dir"):
        path = normalize_path(str(input_data["quarantine_dir"]), env=context.env, cwd=context.cwd)
    elif context.data_dir:
        path = (context.data_dir / "quarantine" / "media-state" / context.run_id).resolve()
    else:
        raise PathSafetyError("Provide MEDIAGENT_DATA_DIR or quarantine_dir.")
    ensure_inside(path, context.allowed_write_roots())
    return path


def _build_manifest(
    *,
    context: ToolContext,
    db_path: Path,
    selector: dict[str, Any],
    library_root: Path,
    quarantine_dir: Path,
) -> dict[str, Any]:
    allowed_roots = context.allowed_write_roots()
    protected_roots = _protected_roots(context)
    rows = _select_rows(db_path, selector)
    item_map: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []

    for row in rows:
        item_id = int(row["media_item_id"])
        item = item_map.setdefault(
            item_id,
            {
                "media_item": {
                    "id": item_id,
                    "platform": row["platform"],
                    "remote_id": row["remote_id"],
                    "media_type": row["media_type"],
                    "status": row["item_status"],
                    "downloaded_at": row["downloaded_at"],
                },
                "media_files": [],
                "blocked_reasons": [],
                "apply_ready": True,
            },
        )
        file_id = row["media_file_id"]
        if file_id is None:
            continue
        file_entry = {
            "id": int(file_id),
            "status": row["file_status"],
            "local_path": row["local_path"],
            "library_relative_path": row["library_relative_path"],
            "exists": False,
            "source_path": None,
            "quarantine_path": None,
            "action": "drop-db-row",
            "skip_reason": None,
        }
        try:
            source_path = _resolve_media_path(row, library_root)
            ensure_inside(source_path, allowed_roots)
            if _is_protected(source_path, protected_roots):
                file_entry["local_path"] = None
                file_entry["library_relative_path"] = None
                file_entry["skip_reason"] = "protected credential path"
                item["blocked_reasons"].append("protected credential path")
                item["apply_ready"] = False
                item["media_files"].append(file_entry)
                continue
            file_entry["source_path"] = str(source_path)
            file_entry["exists"] = source_path.exists()
            if file_entry["exists"]:
                relative = Path(str(item_id)) / f"{int(file_id)}-{source_path.name}"
                quarantine_path = (quarantine_dir / relative).resolve()
                ensure_inside(quarantine_path, [quarantine_dir])
                file_entry["quarantine_path"] = str(quarantine_path)
                file_entry["action"] = "quarantine-then-drop-db-row"
        except PathSafetyError as exc:
            file_entry["skip_reason"] = str(exc)
            item["blocked_reasons"].append(str(exc))
            item["apply_ready"] = False
        item["media_files"].append(file_entry)

    items = list(item_map.values())
    summary = {
        "selected_items": len(items),
        "selected_file_rows": sum(len(item["media_files"]) for item in items),
        "existing_files": sum(
            1
            for item in items
            for file_entry in item["media_files"]
            if file_entry["exists"]
        ),
        "blocked_items": sum(1 for item in items if not item["apply_ready"]),
        "apply_ready_items": sum(1 for item in items if item["apply_ready"]),
    }
    if summary["blocked_items"]:
        warnings.append("Some selected items are blocked and must not be applied until protected or unsafe paths are resolved.")

    return {"data": {"summary": summary, "items": items}, "warnings": warnings}


def _resolve_media_path(row: dict[str, Any], library_root: Path) -> Path:
    relative_path = row["library_relative_path"]
    if relative_path:
        return (library_root / str(relative_path)).resolve()
    local_path = row["local_path"]
    if local_path:
        return Path(str(local_path)).expanduser().resolve()
    raise PathSafetyError(f"Media file row {row['media_file_id']} has no local path.")


def _protected_roots(context: ToolContext) -> list[Path]:
    protected: list[Path] = []
    if context.data_dir:
        protected.append((context.data_dir / "credentials").resolve())
    for name, value in context.env.items():
        if "CREDENTIAL" not in name or not value:
            continue
        try:
            path = normalize_path(str(value), env=context.env, cwd=context.cwd)
        except PathSafetyError:
            continue
        protected.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in protected:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _is_protected(path: Path, protected_roots: list[Path]) -> bool:
    return any(_is_relative_to(path, root) for root in protected_roots)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _select_rows(db_path: Path, selector: dict[str, Any]) -> list[dict[str, Any]]:
    clauses = ["mi.platform = ?"]
    params: list[Any] = [selector["platform"]]
    if selector.get("remote_id"):
        clauses.append("mi.remote_id = ?")
        params.append(selector["remote_id"])
    if selector.get("status"):
        clauses.append("mi.status = ?")
        params.append(selector["status"])
    where = " AND ".join(clauses)
    with db.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT mi.id AS media_item_id,
                   mi.platform,
                   mi.remote_id,
                   mi.media_type,
                   mi.status AS item_status,
                   mi.downloaded_at,
                   mf.id AS media_file_id,
                   mf.status AS file_status,
                   mf.local_path,
                   mf.library_relative_path
            FROM media_items mi
            LEFT JOIN media_files mf ON mf.media_item_id = mi.id
            WHERE {where}
            ORDER BY mi.id, mf.id
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def _apply_manifest(*, db_path: Path, manifest: dict[str, Any], quarantine_dir: Path) -> dict[str, Any]:
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    moved_files = 0
    removed_file_rows = 0
    reset_items = 0
    now = datetime.now(UTC).isoformat()

    with db.connect(db_path) as connection:
        for item in manifest["items"]:
            if not item["apply_ready"]:
                continue
            for file_entry in item["media_files"]:
                source_path_text = file_entry.get("source_path")
                quarantine_path_text = file_entry.get("quarantine_path")
                if source_path_text and quarantine_path_text and file_entry["exists"]:
                    source_path = Path(source_path_text)
                    quarantine_path = Path(quarantine_path_text)
                    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source_path), str(quarantine_path))
                    file_entry["exists"] = False
                    moved_files += 1
                connection.execute("DELETE FROM media_files WHERE id = ?", (file_entry["id"],))
                removed_file_rows += 1
            connection.execute(
                """
                UPDATE media_items
                SET status = 'discovered',
                    downloaded_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, item["media_item"]["id"]),
            )
            item["media_item"]["status"] = "discovered"
            item["media_item"]["downloaded_at"] = None
            reset_items += 1

    manifest["applied_at"] = now
    manifest["summary"]["moved_files"] = moved_files
    manifest["summary"]["removed_file_rows"] = removed_file_rows
    manifest["summary"]["reset_items"] = reset_items
    return manifest
