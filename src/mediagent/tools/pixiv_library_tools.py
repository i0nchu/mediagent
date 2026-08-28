"""Pixiv library classification and legacy-layout reconciliation."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mediagent.core import db, library_content
from mediagent.core.comics import (
    CBZ_MIME_TYPE,
    CBZ_STORAGE_LAYOUT,
    IGNORED_COMIC_SPACER_HEALTH,
    build_cbz_atomic,
    comic_archive_relative_path,
)
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path, resolve_placeholders
from mediagent.core.storage import default_library_root, platform_library_env_name, source_datetime
from mediagent.core.tooling import (
    ErrorCategory,
    Permission,
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSpec,
)
from mediagent.platforms.pixiv import parser as pixiv_parser


def definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.library.reconcile",
                description=(
                    "Plan or apply Pixiv work-type metadata updates, comic file moves, "
                    "and quarantine of known Pixiv placeholder downloads."
                ),
                input_schema={
                    "type": "object",
                    "required": ["mode"],
                    "properties": {
                        "mode": {"type": "string", "enum": ["plan", "apply"]},
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "quarantine_dir": {"type": "string"},
                        "confirm": {"type": "boolean"},
                        "include_details": {"type": "boolean"},
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
            handler=reconcile_pixiv_library,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="pixiv.comics.package",
                description="Package downloaded Pixiv manga pages into Kavita-oriented series CBZ files with ComicInfo.xml.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                        "include_platform_layer": {"type": "boolean"},
                        "remote_id": {"type": "string"},
                        "remote_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer"},
                        "overwrite": {"type": "boolean"},
                        "migrate_legacy": {"type": "boolean"},
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
            handler=package_pixiv_comics,
        ),
    ]


async def package_pixiv_comics(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
        library_root, include_platform_layer = _library_target(context, input_data)
        ensure_inside(db_path, context.allowed_write_roots())
        ensure_inside(library_root, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if not db_path.exists():
        return ToolResult.failure(
            "missing_db",
            "Database does not exist.",
            details={"db_path": str(db_path)},
            category=ErrorCategory.DATABASE,
        )

    selected_ids = _selected_remote_ids(input_data)
    items = [item for item in _select_pixiv_items(db_path) if _item_work_type(item) == "comic"]
    if selected_ids:
        items = [item for item in items if item["remote_id"] in selected_ids]
    if input_data.get("limit") is not None:
        items = items[: max(0, int(input_data["limit"]))]
    plans = [
        _comic_package_plan(
            item=item,
            library_root=library_root,
            include_platform_layer=include_platform_layer,
            overwrite=bool(input_data.get("overwrite")),
            migrate_legacy=bool(input_data.get("migrate_legacy")),
        )
        for item in items
    ]
    summary = _comic_plan_summary(plans)
    if context.dry_run:
        return ToolResult.success(
            {
                "dry_run": True,
                "db_path": str(db_path),
                "library_root": str(library_root),
                "summary": summary,
                "packages": [_public_package_plan(plan) for plan in plans],
            }
        )

    results: list[dict[str, Any]] = []
    artifacts: list[dict[str, str]] = []
    applied_summary = {
        **summary,
        "packaged": 0,
        "legacy_cbz_retired": 0,
        "legacy_cbz_quarantined": 0,
        "failed": 0,
    }
    for plan in plans:
        if plan["status"] == "existing":
            results.append(_public_package_plan(plan))
            continue
        if plan["status"] not in {"ready", "cleanup"}:
            applied_summary["failed"] += 1
            results.append(_public_package_plan(plan))
            continue
        try:
            package = _apply_comic_package(
                db_path=db_path,
                item=plan["item"],
                plan=plan,
                library_root=library_root,
            )
        except Exception as exc:
            applied_summary["failed"] += 1
            results.append(
                {
                    **_public_package_plan(plan),
                    "status": "failed",
                    "reason": f"CBZ packaging failed: {exc}",
                    "error": {"code": "comic_package_failed", "exception_type": type(exc).__name__},
                }
            )
            continue
        if plan["status"] == "ready":
            applied_summary["packaged"] += 1
        applied_summary["legacy_cbz_retired"] += int(package.get("legacy_cbz_retired", 0))
        applied_summary["legacy_cbz_quarantined"] += int(package.get("legacy_cbz_quarantined", 0))
        result_status = "packaged" if plan["status"] == "ready" else "legacy_migrated"
        results.append({**_public_package_plan(plan), "status": result_status, **package})
        artifacts.append({"type": "file", "path": package["target_path"]})

    run_status = "success" if not applied_summary["failed"] else "partial"
    db.insert_run(
        db_path,
        run_type="tool",
        name="pixiv.comics.package",
        status=run_status,
        summary=applied_summary,
        error=None,
        dry_run=False,
    )
    data = {
        "dry_run": False,
        "db_path": str(db_path),
        "library_root": str(library_root),
        "summary": applied_summary,
        "packages": results,
    }
    if run_status == "success":
        return ToolResult.success(data, artifacts=artifacts)
    return ToolResult.failure(
        "pixiv_comic_package_partial",
        "One or more Pixiv comics could not be packaged.",
        data=data,
        category=ErrorCategory.FILESYSTEM,
    )


async def reconcile_pixiv_library(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    try:
        db_path = _db_path(context, input_data)
        library_root = _library_root(context, input_data)
        quarantine_dir = _quarantine_dir(context, input_data)
        ensure_inside(db_path, context.allowed_write_roots())
        ensure_inside(library_root, context.allowed_write_roots())
        ensure_inside(quarantine_dir, context.allowed_write_roots())
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)
    if not db_path.exists():
        return ToolResult.failure(
            "missing_db",
            "Database does not exist.",
            details={"db_path": str(db_path)},
            category=ErrorCategory.DATABASE,
        )

    manifest = _build_manifest(
        context=context,
        db_path=db_path,
        library_root=library_root,
        quarantine_dir=quarantine_dir,
    )
    mode = str(input_data["mode"])
    dry_run_effective = context.dry_run or mode == "plan"
    public_manifest = _public_manifest(manifest, include_details=bool(input_data.get("include_details")))

    if mode == "apply" and not context.dry_run and input_data.get("confirm") is not True:
        return ToolResult.failure(
            "pixiv_reconcile_not_confirmed",
            "Pixiv library reconciliation apply requires confirm: true.",
            data={
                "mode": mode,
                "dry_run": False,
                "db_path": str(db_path),
                "library_root": str(library_root),
                "quarantine_dir": str(quarantine_dir),
                "manifest": public_manifest,
            },
            category=ErrorCategory.VALIDATION,
        )
    if dry_run_effective:
        warnings = []
        if mode == "apply" and context.dry_run:
            warnings.append("dry-run: apply mode requested but no mutations were performed")
        return ToolResult.success(
            {
                "mode": mode,
                "dry_run": True,
                "db_path": str(db_path),
                "library_root": str(library_root),
                "quarantine_dir": str(quarantine_dir),
                "manifest": public_manifest,
            },
            warnings=warnings,
        )
    if manifest["summary"]["blocked_actions"]:
        return ToolResult.failure(
            "pixiv_reconcile_blocked",
            "Pixiv library reconciliation is blocked by unsafe paths or target conflicts.",
            data={
                "mode": mode,
                "dry_run": False,
                "db_path": str(db_path),
                "library_root": str(library_root),
                "quarantine_dir": str(quarantine_dir),
                "manifest": public_manifest,
            },
            category=ErrorCategory.FILESYSTEM,
        )

    try:
        applied = _apply_manifest(db_path=db_path, manifest=manifest)
    except OSError as exc:
        return ToolResult.failure(
            "pixiv_reconcile_move_failed",
            "Pixiv library reconciliation could not move one or more files atomically.",
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.FILESYSTEM,
        )
    except Exception as exc:
        return ToolResult.failure(
            "pixiv_reconcile_apply_failed",
            "Pixiv library reconciliation could not commit its database updates.",
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.DATABASE,
        )
    return ToolResult.success(
        {
            "mode": mode,
            "dry_run": False,
            "db_path": str(db_path),
            "library_root": str(library_root),
            "quarantine_dir": str(quarantine_dir),
            "manifest": _public_manifest(applied, include_details=bool(input_data.get("include_details"))),
        }
    )


def _db_path(context: ToolContext, input_data: dict[str, Any]) -> Path:
    raw = input_data.get("db_path")
    if raw:
        return Path(resolve_placeholders(str(raw), context.env)).expanduser().resolve()
    if context.db_path:
        return context.db_path.resolve()
    raise ValueError("Provide db_path or set MEDIAGENT_DB_PATH.")


def _library_root(context: ToolContext, input_data: dict[str, Any]) -> Path:
    return _library_target(context, input_data)[0]


def _library_target(context: ToolContext, input_data: dict[str, Any]) -> tuple[Path, bool]:
    if input_data.get("library_root"):
        return (
            normalize_path(str(input_data["library_root"]), env=context.env, cwd=context.cwd),
            bool(input_data.get("include_platform_layer", True)),
        )
    platform_root = context.env.get(platform_library_env_name("pixiv"))
    if platform_root:
        return (
            normalize_path(str(platform_root), env=context.env, cwd=context.cwd),
            bool(input_data.get("include_platform_layer", False)),
        )
    return (
        default_library_root(data_dir=context.data_dir, library_dir=context.library_dir),
        bool(input_data.get("include_platform_layer", True)),
    )


def _quarantine_dir(context: ToolContext, input_data: dict[str, Any]) -> Path:
    if input_data.get("quarantine_dir"):
        return normalize_path(str(input_data["quarantine_dir"]), env=context.env, cwd=context.cwd)
    if not context.data_dir:
        raise ValueError("Provide quarantine_dir or set MEDIAGENT_DATA_DIR.")
    return (context.data_dir / "quarantine" / "pixiv-library" / context.run_id).resolve()


def _build_manifest(
    *,
    context: ToolContext,
    db_path: Path,
    library_root: Path,
    quarantine_dir: Path,
) -> dict[str, Any]:
    items = _select_pixiv_items(db_path)
    actions: list[dict[str, Any]] = []
    item_updates: list[dict[str, Any]] = []
    summary = {
        "items_scanned": len(items),
        "comic_items": 0,
        "illustration_items": 0,
        "animation_items": 0,
        "unavailable_items": 0,
        "metadata_updates": 0,
        "files_to_move": 0,
        "sidecars_to_move": 0,
        "placeholder_rows_to_remove": 0,
        "placeholder_files_to_quarantine": 0,
        "missing_files": 0,
        "blocked_actions": 0,
    }

    for item in items:
        metadata = item["metadata"]
        pixiv_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
        pixiv_type = str(pixiv_metadata.get("pixiv_type") or metadata.get("pixiv_type") or "illust")
        work_type = pixiv_parser.pixiv_work_type(pixiv_type)
        storage_category = pixiv_parser.PIXIV_STORAGE_CATEGORIES[work_type]
        summary[f"{work_type}_items"] += 1
        placeholder_files = [file_row for file_row in item["files"] if _is_placeholder_file(file_row)]
        visible = pixiv_metadata.get("visible", metadata.get("visible"))
        unavailable_reason = None
        if visible is False or visible == 0:
            unavailable_reason = "visible_false"
        elif placeholder_files and len(placeholder_files) == len(item["files"]):
            unavailable_reason = "placeholder_asset"
        source_availability = "unavailable" if unavailable_reason else "available"
        if unavailable_reason:
            summary["unavailable_items"] += 1

        updated_metadata = json.loads(json.dumps(metadata))
        updated_metadata["pixiv_type"] = pixiv_type
        updated_metadata["work_type"] = work_type
        updated_metadata["storage_category"] = storage_category
        if work_type == "comic":
            updated_metadata["comic"] = pixiv_parser.pixiv_comic_metadata(
                remote_id=item["remote_id"],
                metadata=pixiv_metadata,
            )
        if isinstance(updated_metadata.get("pixiv"), dict):
            updated_metadata["pixiv"]["work_type"] = work_type
            updated_metadata["pixiv"]["storage_category"] = storage_category
            if work_type == "comic":
                updated_metadata["pixiv"]["comic"] = updated_metadata["comic"]
        if unavailable_reason:
            updated_metadata["availability_reason"] = unavailable_reason
            if isinstance(updated_metadata.get("pixiv"), dict):
                updated_metadata["pixiv"]["availability_reason"] = unavailable_reason
        elif updated_metadata.get("availability_reason") in {"visible_false", "placeholder_asset"}:
            updated_metadata.pop("availability_reason", None)

        metadata_changed = updated_metadata != metadata or item["source_availability"] != source_availability
        if metadata_changed:
            summary["metadata_updates"] += 1
        item_updates.append(
            {
                "id": item["id"],
                "remote_id": item["remote_id"],
                "metadata": updated_metadata,
                "source_availability": source_availability,
                "unavailable_reason": unavailable_reason,
                "metadata_changed": metadata_changed,
            }
        )

        for file_row in item["files"]:
            if _is_cbz_file(file_row):
                continue
            if _is_placeholder_file(file_row):
                action = _placeholder_action(
                    context=context,
                    item=item,
                    file_row=file_row,
                    library_root=library_root,
                    quarantine_dir=quarantine_dir,
                )
                actions.append(action)
                summary["placeholder_rows_to_remove"] += 1
                if action["source_exists"]:
                    summary["placeholder_files_to_quarantine"] += 1
                if action["blocked_reason"]:
                    summary["blocked_actions"] += 1
                continue
            action = _classification_move_action(
                context=context,
                item=item,
                file_row=file_row,
                library_root=library_root,
                storage_category=storage_category,
            )
            if action is None:
                continue
            actions.append(action)
            if action["action"] == "move":
                summary["files_to_move"] += 1
                if action["sidecar_source"]:
                    summary["sidecars_to_move"] += 1
            if action["action"] == "missing":
                summary["missing_files"] += 1
            if action["blocked_reason"]:
                summary["blocked_actions"] += 1

    return {
        "summary": summary,
        "item_updates": item_updates,
        "actions": actions,
    }


def _select_pixiv_items(db_path: Path) -> list[dict[str, Any]]:
    with db.connect(db_path) as connection:
        item_rows = connection.execute(
            """
            SELECT id, remote_id, status, source_availability, metadata_json
                 , source_url, author_name, media_type
            FROM media_items
            WHERE platform = 'pixiv'
            ORDER BY id
            """
        ).fetchall()
        file_rows = connection.execute(
            """
            SELECT mf.id, mf.media_item_id, mf.file_key, mf.remote_url,
                   mf.local_path, mf.library_relative_path, mf.storage_layout,
                   mf.mime_type, mf.status, mf.file_health, mf.size_bytes,
                   mf.checksum, mf.library_entry_id,
                   le.state AS library_state,
                   le.trash_path AS library_trash_path,
                   le.display_name_override
            FROM media_files mf
            LEFT JOIN library_entries le ON le.id = mf.library_entry_id
            WHERE mf.media_item_id IN (
                SELECT id FROM media_items WHERE platform = 'pixiv'
            )
            ORDER BY mf.media_item_id, mf.id
            """
        ).fetchall()
    files_by_item: dict[int, list[dict[str, Any]]] = {}
    for row in file_rows:
        files_by_item.setdefault(int(row["media_item_id"]), []).append(dict(row))
    return [
        {
            "id": int(row["id"]),
            "remote_id": str(row["remote_id"]),
            "status": str(row["status"]),
            "platform": "pixiv",
            "source_url": row["source_url"],
            "author_name": row["author_name"],
            "media_type": row["media_type"],
            "source_availability": str(row["source_availability"]),
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "files": files_by_item.get(int(row["id"]), []),
        }
        for row in item_rows
    ]


def _is_placeholder_file(file_row: dict[str, Any]) -> bool:
    return pixiv_parser.is_pixiv_placeholder_url(file_row.get("remote_url"))


def _is_cbz_file(file_row: dict[str, Any]) -> bool:
    return file_row.get("mime_type") == CBZ_MIME_TYPE or str(file_row.get("local_path") or "").lower().endswith(".cbz")


def _is_ignored_comic_spacer(file_row: dict[str, Any]) -> bool:
    return (
        file_row.get("status") == "skipped"
        and file_row.get("file_health") == IGNORED_COMIC_SPACER_HEALTH
    )


def _selected_remote_ids(input_data: dict[str, Any]) -> set[str]:
    selected = {str(value) for value in input_data.get("remote_ids", []) if value is not None}
    if input_data.get("remote_id") is not None:
        selected.add(str(input_data["remote_id"]))
    return selected


def _item_work_type(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    pixiv_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
    return str(metadata.get("work_type") or pixiv_metadata.get("work_type") or pixiv_parser.pixiv_work_type(
        metadata.get("pixiv_type") or pixiv_metadata.get("pixiv_type")
    ))


def _comic_package_plan(
    *,
    item: dict[str, Any],
    library_root: Path,
    include_platform_layer: bool,
    overwrite: bool,
    migrate_legacy: bool,
    refresh_tracked: bool = False,
) -> dict[str, Any]:
    cbz_records = [record for record in item["files"] if _is_cbz_file(record)]
    removed_cbz = [record for record in cbz_records if record.get("library_state") == "removed"]
    renamed_cbz = next(
        (
            record
            for record in cbz_records
            if record.get("library_state") == "active"
            and str(record.get("display_name_override") or "").strip()
            and record.get("local_path")
        ),
        None,
    )
    if renamed_cbz is not None:
        target_path = Path(str(renamed_cbz["local_path"])).expanduser().resolve()
        try:
            relative_path = target_path.relative_to(library_root)
        except ValueError:
            relative_path = Path(str(renamed_cbz.get("library_relative_path") or target_path.name))
    else:
        relative_path = comic_archive_relative_path(item=item, include_platform_layer=include_platform_layer)
        target_path = (library_root / relative_path).resolve()
    plan = {
        "item": item,
        "remote_id": item["remote_id"],
        "status": "ready",
        "reason": None,
        "pages": [],
        "page_count": 0,
        "target_path": str(target_path),
        "relative_path": relative_path.as_posix(),
        "display_name_override": (
            str(renamed_cbz.get("display_name_override")) if renamed_cbz is not None else None
        ),
        "legacy_cbz": [],
    }
    try:
        ensure_inside(target_path, [library_root])
    except PathSafetyError as exc:
        plan["status"] = "blocked"
        plan["reason"] = str(exc)
        return plan
    if item.get("status") == "skipped":
        plan["status"] = "skipped"
        plan["reason"] = "all comic source pages were ignored as non-content"
        return plan
    if item.get("status") != "downloaded":
        plan["status"] = "incomplete"
        plan["reason"] = f"item_status:{item.get('status')}"
        return plan
    existing_cbz = next(
        (
            record
            for record in cbz_records
            if record.get("library_state") != "removed"
            and record.get("local_path")
            and Path(str(record["local_path"])).expanduser().resolve() == target_path
        ),
        None,
    )
    if removed_cbz and existing_cbz is None:
        plan["status"] = "skipped"
        plan["reason"] = "comic archive was explicitly removed"
        return plan
    legacy_cbz: list[dict[str, Any]] = []
    for record in cbz_records:
        if record.get("library_state") == "removed":
            continue
        try:
            legacy_path = _source_path(record, library_root)
            ensure_inside(legacy_path, [library_root])
        except PathSafetyError as exc:
            plan["status"] = "blocked"
            plan["reason"] = str(exc)
            return plan
        if legacy_path == target_path:
            continue
        if _path_is_in_trash(legacy_path, library_root):
            plan["status"] = "blocked"
            plan["reason"] = "active legacy CBZ already points inside trash; reconcile its library state first"
            return plan
        if not legacy_path.is_file():
            plan["status"] = "incomplete"
            plan["reason"] = "tracked legacy CBZ is missing from the library"
            return plan
        legacy_cbz.append({"record": record, "source": legacy_path})
    plan["legacy_cbz"] = legacy_cbz
    if legacy_cbz and not migrate_legacy:
        plan["status"] = "blocked"
        plan["reason"] = "legacy CBZ exists; rerun with migrate_legacy:true after reviewing the plan"
        return plan
    if target_path.exists() and not overwrite:
        if existing_cbz and existing_cbz.get("status") == "downloaded" and existing_cbz.get("file_health") in {"valid", "unknown"}:
            if not refresh_tracked:
                plan["status"] = "cleanup" if legacy_cbz else "existing"
                plan["page_count"] = _effective_metadata_page_count(item)
                return plan
            plan["status"] = "ready"
        else:
            plan["status"] = "blocked"
            plan["reason"] = "target file already exists but is not a healthy tracked CBZ"
            return plan

    page_records, reason = _ordered_comic_page_records(item)
    if reason:
        plan["status"] = "incomplete"
        plan["reason"] = reason
        return plan
    page_paths: list[Path] = []
    for record in page_records:
        try:
            path = _source_path(record, library_root)
            ensure_inside(path, [library_root])
        except PathSafetyError as exc:
            plan["status"] = "blocked"
            plan["reason"] = str(exc)
            return plan
        if _path_is_in_trash(path, library_root) or not path.is_file():
            plan["status"] = "incomplete"
            plan["reason"] = "comic source page is missing from the library"
            return plan
        if record.get("status") != "downloaded" or record.get("file_health") in {"missing", "corrupt"}:
            plan["status"] = "incomplete"
            plan["reason"] = "comic source page is not healthy"
            return plan
        page_paths.append(path)
    plan["pages"] = page_paths
    plan["page_count"] = len(page_paths)
    return plan


def _ordered_comic_page_records(item: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    page_records = [record for record in item["files"] if not _is_cbz_file(record) and not _is_placeholder_file(record)]
    source_records = [record for record in page_records if not _is_ignored_comic_spacer(record)]
    records_by_remote = {str(record.get("remote_url") or ""): record for record in page_records if record.get("remote_url")}
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    metadata_files = metadata.get("files") if isinstance(metadata.get("files"), list) else []
    ordered: list[dict[str, Any]] = []
    ignored = 0
    if metadata_files:
        for file_info in metadata_files:
            if not isinstance(file_info, dict):
                continue
            remote_url = str(file_info.get("url") or file_info.get("remote_url") or "")
            if not remote_url:
                continue
            record = records_by_remote.get(remote_url)
            if record is None:
                return [], "comic source page has no media_files record"
            if _is_ignored_comic_spacer(record):
                ignored += 1
                continue
            ordered.append(record)
    if not ordered:
        ordered = sorted(source_records, key=_comic_page_sort_key)
    if not ordered:
        return [], "comic has no downloaded source pages"
    expected_count = _metadata_page_count(item)
    if expected_count and len(ordered) + ignored != expected_count:
        return [], f"comic page count mismatch: expected {expected_count}, found {len(ordered) + ignored}"
    return ordered, None


def _comic_page_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    path = str(record.get("library_relative_path") or record.get("local_path") or "")
    match = re.search(r"__p(\d+)(?:\.[^.]+)?$", path)
    return (int(match.group(1)) if match else 10**9, path)


def _metadata_page_count(item: dict[str, Any]) -> int:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    pixiv_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
    try:
        return int(metadata.get("page_count") or pixiv_metadata.get("page_count") or 0)
    except (TypeError, ValueError):
        return 0


def _effective_metadata_page_count(item: dict[str, Any]) -> int:
    return max(
        0,
        _metadata_page_count(item)
        - sum(_is_ignored_comic_spacer(record) for record in item.get("files", [])),
    )


def _comic_plan_summary(plans: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "selected": len(plans),
        "ready": 0,
        "cleanup": 0,
        "existing": 0,
        "skipped": 0,
        "incomplete": 0,
        "blocked": 0,
    }
    for plan in plans:
        summary[plan["status"]] += 1
    return summary


def _public_package_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "remote_id": plan["remote_id"],
        "status": plan["status"],
        "reason": plan["reason"],
        "page_count": plan["page_count"],
        "relative_path": plan["relative_path"],
        "target_path": plan["target_path"],
        "legacy_cbz_count": len(plan.get("legacy_cbz", [])),
    }


def _apply_comic_package(
    *,
    db_path: Path,
    item: dict[str, Any],
    plan: dict[str, Any],
    library_root: Path,
) -> dict[str, Any]:
    if plan["status"] == "cleanup":
        package = {
            "target_path": plan["target_path"],
            "pages": plan["page_count"],
        }
    else:
        package_item = _item_with_display_override(item, plan.get("display_name_override"))
        package = build_cbz_atomic(
            target_path=Path(plan["target_path"]),
            pages=plan["pages"],
            item=package_item,
            allowed_root=library_root,
        )
        source_dt, _ = source_datetime(item, {})
        file_record = db.upsert_media_file(
            db_path,
            platform=item["platform"],
            remote_id=item["remote_id"],
            remote_url=None,
            local_path=package["target_path"],
            mime_type=CBZ_MIME_TYPE,
            size_bytes=package["size_bytes"],
            checksum=package["checksum"],
            status="downloaded",
            library_relative_path=plan["relative_path"],
            storage_layout=CBZ_STORAGE_LAYOUT,
            file_health="valid",
            source_timestamp=source_dt.isoformat(),
            verified_at=datetime.now(UTC).isoformat(),
            file_key="archive:cbz" if item["platform"] != "pixiv" else None,
        )
        adoption = library_content.adopt_media_file(db_path, file_id=int(file_record["id"]))
        if adoption.get("target_path"):
            package["target_path"] = adoption["target_path"]
            package["deduplicated"] = adoption.get("deduplicated", False)
            package["hardlinked"] = adoption.get("hardlinked", False)
    quarantined = _retire_legacy_cbz(
        db_path=db_path,
        library_root=library_root,
        item=item,
        entries=plan.get("legacy_cbz", []),
    )
    package["legacy_cbz_retired"] = len(plan.get("legacy_cbz", []))
    package["legacy_cbz_quarantined"] = len(quarantined)
    package["legacy_quarantine_paths"] = quarantined
    return package


def _item_with_display_override(item: dict[str, Any], display_name: Any) -> dict[str, Any]:
    title = str(display_name or "").strip()
    if not title:
        return item
    overridden = dict(item)
    metadata = dict(item.get("metadata") or {})
    comic = dict(metadata.get("comic") or {})
    comic["title"] = title
    metadata["comic"] = comic
    overridden["metadata"] = metadata
    return overridden


def _retire_legacy_cbz(
    *,
    db_path: Path,
    library_root: Path,
    item: dict[str, Any],
    entries: list[dict[str, Any]],
) -> list[str]:
    retired_paths: list[str] = []
    for legacy in entries:
        source = Path(legacy["source"])
        managed_entry = library_content.resolve_entry(db_path, path=source)
        if managed_entry is None:
            raise ValueError(f"Legacy CBZ is not a managed library entry: {source}")
        removal = library_content.remove_entry(
            db_path,
            entry_id=str(managed_entry["id"]),
            library_root=library_root,
            reason="retire legacy comic archive layout",
            external_ref=f"{item['platform']}:{item['remote_id']}:media_file:{legacy['record']['id']}",
        )
        trash_path = removal.get("trash_path") or removal.get("entry", {}).get("trash_path")
        if trash_path:
            retired_paths.append(str(trash_path))
    return retired_paths


def _placeholder_action(
    *,
    context: ToolContext,
    item: dict[str, Any],
    file_row: dict[str, Any],
    library_root: Path,
    quarantine_dir: Path,
) -> dict[str, Any]:
    action = _base_action(item, file_row, "quarantine-placeholder")
    try:
        source = _source_path(file_row, library_root)
        ensure_inside(source, [library_root])
        action["source"] = str(source)
        action["source_exists"] = source.exists() and not _path_is_in_trash(source, library_root)
        if source.exists():
            quarantine = (quarantine_dir / str(item["id"]) / f"{file_row['id']}-{source.name}").resolve()
            ensure_inside(quarantine, [quarantine_dir])
            action["target"] = str(quarantine)
            if quarantine.exists():
                action["blocked_reason"] = "quarantine target already exists"
                return action
            sidecar = source.with_suffix(".json")
            if sidecar.exists():
                action["sidecar_source"] = str(sidecar)
                action["sidecar_target"] = str(quarantine.with_suffix(".json"))
                if Path(str(action["sidecar_target"])).exists():
                    action["blocked_reason"] = "quarantine sidecar target already exists"
    except PathSafetyError as exc:
        action["blocked_reason"] = str(exc)
    return action


def _classification_move_action(
    *,
    context: ToolContext,
    item: dict[str, Any],
    file_row: dict[str, Any],
    library_root: Path,
    storage_category: str,
) -> dict[str, Any] | None:
    relative_text = file_row.get("library_relative_path")
    if not relative_text:
        return None
    current = PurePosixPath(str(relative_text))
    desired = _replace_storage_category(current, storage_category)
    if desired == current:
        return None
    action = _base_action(item, file_row, "move")
    action["current_relative_path"] = current.as_posix()
    action["target_relative_path"] = desired.as_posix()
    try:
        source = _source_path(file_row, library_root)
        target = (library_root / Path(*desired.parts)).resolve()
        ensure_inside(source, [library_root])
        ensure_inside(target, [library_root])
        action["source"] = str(source)
        action["target"] = str(target)
        action["source_exists"] = source.exists() and not _path_is_in_trash(source, library_root)
        if not source.exists():
            action["action"] = "missing"
            return action
        if _path_is_in_trash(source, library_root):
            action["action"] = "missing"
            return action
        if target.exists() and target != source:
            action["blocked_reason"] = "target file already exists"
            return action
        sidecar_source = source.with_suffix(".json")
        sidecar_target = target.with_suffix(".json")
        if sidecar_source.exists():
            if sidecar_target.exists() and sidecar_target != sidecar_source:
                action["blocked_reason"] = "target sidecar already exists"
                return action
            action["sidecar_source"] = str(sidecar_source)
            action["sidecar_target"] = str(sidecar_target)
    except PathSafetyError as exc:
        action["blocked_reason"] = str(exc)
    return action


def _base_action(item: dict[str, Any], file_row: dict[str, Any], action: str) -> dict[str, Any]:
    return {
        "action": action,
        "item_id": item["id"],
        "remote_id": item["remote_id"],
        "file_id": int(file_row["id"]),
        "file_key": file_row["file_key"],
        "source": None,
        "target": None,
        "source_exists": False,
        "current_relative_path": file_row.get("library_relative_path"),
        "target_relative_path": None,
        "sidecar_source": None,
        "sidecar_target": None,
        "blocked_reason": None,
    }


def _source_path(file_row: dict[str, Any], library_root: Path) -> Path:
    if file_row.get("local_path"):
        return Path(str(file_row["local_path"])).expanduser().resolve()
    if file_row.get("library_relative_path"):
        return (library_root / Path(*PurePosixPath(str(file_row["library_relative_path"])).parts)).resolve()
    raise PathSafetyError(f"Pixiv media file row {file_row['id']} has no local path.")


def _path_is_in_trash(path: Path, library_root: Path) -> bool:
    try:
        relative = path.relative_to(library_root)
    except ValueError:
        return False
    return any(part.lower() == ".trash" for part in relative.parts)


def _replace_storage_category(path: PurePosixPath, storage_category: str) -> PurePosixPath:
    parts = list(path.parts)
    index = 1 if len(parts) > 1 and parts[0] == "pixiv" else 0
    if len(parts) <= index or parts[index] not in {"photo", "comic", "comic-pages", "video", "audio"}:
        return path
    parts[index] = storage_category
    return PurePosixPath(*parts)


def package_one_comic(
    *,
    db_path: Path,
    library_root: Path,
    remote_id: str,
    include_platform_layer: bool,
    overwrite: bool = False,
    migrate_legacy: bool = True,
) -> dict[str, Any]:
    """Package one downloaded comic and return its public result."""
    item = next((candidate for candidate in _select_pixiv_items(db_path) if candidate["remote_id"] == remote_id), None)
    if item is None:
        return {
            "remote_id": remote_id,
            "status": "failed",
            "reason": "Pixiv media item is not present in the database",
            "page_count": 0,
        }
    plan = _comic_package_plan(
        item=item,
        library_root=library_root,
        include_platform_layer=include_platform_layer,
        overwrite=overwrite,
        migrate_legacy=migrate_legacy,
    )
    public = _public_package_plan(plan)
    if plan["status"] not in {"ready", "cleanup"}:
        return public
    try:
        package = _apply_comic_package(
            db_path=db_path,
            item=item,
            plan=plan,
            library_root=library_root,
        )
    except Exception as exc:
        return {
            **public,
            "status": "failed",
            "reason": f"CBZ packaging failed: {exc}",
            "error": {"code": "comic_package_failed", "exception_type": type(exc).__name__},
        }
    status = "packaged" if plan["status"] == "ready" else "legacy_migrated"
    return {**public, "status": status, **package}


def _apply_manifest(*, db_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    completed_moves: list[tuple[Path, Path]] = []
    try:
        for action in manifest["actions"]:
            if action["blocked_reason"] or not action["source_exists"]:
                continue
            source = Path(str(action["source"]))
            target = Path(str(action["target"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            completed_moves.append((target, source))
            if action["sidecar_source"]:
                sidecar_source = Path(str(action["sidecar_source"]))
                sidecar_target = Path(str(action["sidecar_target"]))
                sidecar_target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(sidecar_source, sidecar_target)
                completed_moves.append((sidecar_target, sidecar_source))

        now = datetime.now(UTC).isoformat()
        with db.connect(db_path) as connection:
            for action in manifest["actions"]:
                if action["action"] == "quarantine-placeholder":
                    connection.execute("DELETE FROM media_files WHERE id = ?", (action["file_id"],))
                elif action["action"] == "move" and action["source_exists"]:
                    new_local_path = str(action["target"])
                    new_file_key = action["file_key"]
                    if str(new_file_key).startswith("local:"):
                        new_file_key = f"local:{new_local_path}"
                    connection.execute(
                        """
                        UPDATE media_files
                        SET file_key = ?, local_path = ?, library_relative_path = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_file_key, new_local_path, action["target_relative_path"], now, action["file_id"]),
                    )
            for update in manifest["item_updates"]:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM media_files WHERE media_item_id = ?",
                    (update["id"],),
                ).fetchone()[0]
                status_sql = "status"
                downloaded_at_sql = "downloaded_at"
                if update["unavailable_reason"] and remaining == 0:
                    status_sql = "'skipped'"
                    downloaded_at_sql = "NULL"
                connection.execute(
                    f"""
                    UPDATE media_items
                    SET metadata_json = ?, source_availability = ?,
                        status = {status_sql}, downloaded_at = {downloaded_at_sql}, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(update["metadata"], sort_keys=True),
                        update["source_availability"],
                        now,
                        update["id"],
                    ),
                )
    except Exception:
        for current, original in reversed(completed_moves):
            if current.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                os.replace(current, original)
        raise

    manifest["applied_at"] = datetime.now(UTC).isoformat()
    manifest["summary"]["files_moved"] = sum(
        1 for action in manifest["actions"] if action["action"] == "move" and action["source_exists"]
    )
    manifest["summary"]["placeholder_rows_removed"] = sum(
        1 for action in manifest["actions"] if action["action"] == "quarantine-placeholder"
    )
    return manifest


def _public_manifest(manifest: dict[str, Any], *, include_details: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"summary": dict(manifest["summary"])}
    if manifest.get("applied_at"):
        result["applied_at"] = manifest["applied_at"]
    if include_details:
        result["items"] = [
            {
                "remote_id": update["remote_id"],
                "work_type": update["metadata"].get("work_type"),
                "storage_category": update["metadata"].get("storage_category"),
                "source_availability": update["source_availability"],
                "metadata_changed": update["metadata_changed"],
            }
            for update in manifest["item_updates"]
        ]
        result["actions"] = [
            {key: value for key, value in action.items() if key not in {"item_id", "file_key"}}
            for action in manifest["actions"]
        ]
    return result
