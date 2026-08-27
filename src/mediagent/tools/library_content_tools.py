"""Global content deduplication and one-shot library entry operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mediagent.core import db, library_content
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.storage import default_library_root
from mediagent.core.tooling import ErrorCategory, Permission, ToolContext, ToolDefinition, ToolResult, ToolSpec


def definitions() -> list[ToolDefinition]:
    common_properties = {
        "db_path": {"type": "string"},
        "library_root": {"type": "string"},
        "entry_id": {"type": "string"},
        "path": {"type": "string"},
    }
    return [
        ToolDefinition(
            spec=ToolSpec(
                name="library.content.deduplicate",
                description="Scan all tracked downloaded files and apply global SHA-256 content deduplication.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=deduplicate_content,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="library.trash.reconcile",
                description="Import verified pre-v10 trash files as explicit removed library entries.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "db_path": {"type": "string"},
                        "library_root": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES),
                dry_run_supported=True,
            ),
            handler=reconcile_legacy_trash,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="library.entry.remove",
                description="Move one managed scanner-visible library entry to Mediagent trash and suppress resync.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **common_properties,
                        "reason": {"type": "string"},
                        "external_ref": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=False,
            ),
            handler=remove_library_entry,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="library.entry.restore",
                description="Restore one previously removed managed library entry.",
                input_schema={
                    "type": "object",
                    "properties": {
                        **common_properties,
                        "removal_id": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=False,
            ),
            handler=restore_library_entry,
        ),
        ToolDefinition(
            spec=ToolSpec(
                name="library.entry.rename",
                description="Rename one active managed library entry and persist a user title override.",
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        **common_properties,
                        "name": {"type": "string"},
                        "external_ref": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.READ_DB, Permission.WRITE_DB, Permission.READ_FILES, Permission.WRITE_FILES),
                dry_run_supported=False,
            ),
            handler=rename_library_entry,
        ),
    ]


def deduplicate_content(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _paths(context, input_data, require_library_root=False)
    if isinstance(resolved, ToolResult):
        return resolved
    db_path, _ = resolved
    if not db_path.exists():
        return ToolResult.failure("missing_db", "Database does not exist.", category=ErrorCategory.DATABASE)
    try:
        plan = library_content.scan_plan(db_path)
        unsafe = _unsafe_plan_paths(context, input_data, plan)
        public_plan = {key: value for key, value in plan.items() if not key.startswith("_")}
        if unsafe:
            return ToolResult.failure(
                "unsafe_library_path",
                "One or more tracked files are outside configured library roots.",
                data={"dry_run": context.dry_run, "plan": public_plan, "unsafe_paths": unsafe},
                category=ErrorCategory.FILESYSTEM,
            )
        if context.dry_run:
            return ToolResult.success({"dry_run": True, "plan": public_plan})
        applied = library_content.apply_scan_plan(db_path, plan)
        return ToolResult.success({"dry_run": False, "plan": applied})
    except (OSError, ValueError) as exc:
        return ToolResult.failure(
            "library_dedup_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.FILESYSTEM,
        )


def reconcile_legacy_trash(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _paths(context, input_data, require_library_root=True)
    if isinstance(resolved, ToolResult):
        return resolved
    db_path, explicit_root = resolved
    if not db_path.exists():
        return ToolResult.failure("missing_db", "Database does not exist.", category=ErrorCategory.DATABASE)
    library_root = explicit_root or default_library_root(
        data_dir=context.data_dir,
        library_dir=context.library_dir,
    )
    try:
        ensure_inside(library_root, context.allowed_write_roots())
        plan = library_content.legacy_trash_plan(db_path, library_root=library_root)
        if context.dry_run:
            return ToolResult.success({"dry_run": True, "plan": plan})
        if plan.get("blocked"):
            return ToolResult.failure(
                "legacy_trash_reconcile_blocked",
                "Legacy trash reconciliation is blocked; review the dry-run report.",
                data={"dry_run": False, "plan": plan},
                category=ErrorCategory.FILESYSTEM,
            )
        applied = library_content.apply_legacy_trash_plan(db_path, plan)
        return ToolResult.success({"dry_run": False, "plan": applied})
    except (OSError, ValueError) as exc:
        return ToolResult.failure(
            "legacy_trash_reconcile_failed",
            str(exc),
            details={"exception_type": type(exc).__name__},
            category=ErrorCategory.FILESYSTEM,
        )


def remove_library_entry(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    selection = _entry_selection(context, input_data)
    if isinstance(selection, ToolResult):
        return selection
    db_path, library_root, entry = selection
    try:
        result = library_content.remove_entry(
            db_path,
            entry_id=str(entry["id"]),
            library_root=library_root,
            reason=input_data.get("reason"),
            external_ref=input_data.get("external_ref"),
        )
        return ToolResult.success(result)
    except FileNotFoundError as exc:
        return ToolResult.failure(
            "library_entry_missing",
            "Managed library file is missing.",
            details={"path": str(exc)},
            category=ErrorCategory.FILESYSTEM,
        )
    except (OSError, ValueError) as exc:
        return ToolResult.failure("library_remove_failed", str(exc), category=ErrorCategory.FILESYSTEM)


def restore_library_entry(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    resolved = _paths(context, input_data, require_library_root=False)
    if isinstance(resolved, ToolResult):
        return resolved
    db_path, _ = resolved
    selector_count = sum(bool(input_data.get(name)) for name in ("removal_id", "entry_id", "path"))
    if selector_count != 1:
        return ToolResult.failure(
            "invalid_library_selector",
            "Provide exactly one of removal_id, entry_id, or path.",
            category=ErrorCategory.VALIDATION,
        )
    try:
        entry_id = input_data.get("entry_id")
        entry = None
        if input_data.get("removal_id"):
            entry = library_content.resolve_removal_entry(db_path, str(input_data["removal_id"]))
            if entry is None:
                return ToolResult.failure(
                    "library_removal_not_found",
                    "No removal matches the identifier.",
                    category=ErrorCategory.DATABASE,
                )
            entry_id = entry["id"]
        elif input_data.get("path"):
            path = _managed_path(context, input_data, str(input_data["path"]))
            entry = library_content.resolve_entry(db_path, path=path)
            if entry is None:
                return ToolResult.failure(
                    "library_entry_not_found",
                    "No managed library entry matches the path.",
                    category=ErrorCategory.DATABASE,
                )
            entry_id = entry["id"]
        else:
            entry = library_content.resolve_entry(db_path, entry_id=str(entry_id))
            if entry is None:
                return ToolResult.failure(
                    "library_entry_not_found",
                    "No managed library entry matches the identifier.",
                    category=ErrorCategory.DATABASE,
                )
        for raw_path in (entry.get("local_path"), entry.get("trash_path")):
            if raw_path and _matching_library_root(context, input_data, Path(str(raw_path)).resolve()) is None:
                return ToolResult.failure(
                    "unsafe_library_path",
                    "Managed entry is outside configured library roots.",
                    category=ErrorCategory.FILESYSTEM,
                )
        result = library_content.restore_entry(
            db_path,
            removal_id=input_data.get("removal_id"),
            entry_id=entry_id,
        )
        return ToolResult.success(result)
    except FileExistsError:
        return ToolResult.failure(
            "restore_path_conflict",
            "Restore target is occupied by different content.",
            category=ErrorCategory.FILESYSTEM,
        )
    except FileNotFoundError:
        return ToolResult.failure(
            "restore_content_missing",
            "Neither trash content nor an existing valid target can be restored.",
            category=ErrorCategory.FILESYSTEM,
        )
    except (OSError, ValueError) as exc:
        code = "restore_checksum_conflict" if "checksum" in str(exc).lower() else "library_restore_failed"
        return ToolResult.failure(code, str(exc), category=ErrorCategory.FILESYSTEM)


def rename_library_entry(context: ToolContext, input_data: dict[str, Any]) -> ToolResult:
    selection = _entry_selection(context, input_data)
    if isinstance(selection, ToolResult):
        return selection
    db_path, _, entry = selection
    try:
        result = library_content.rename_entry(
            db_path,
            entry_id=str(entry["id"]),
            name=str(input_data["name"]),
            external_ref=input_data.get("external_ref"),
        )
        return ToolResult.success(result)
    except FileExistsError:
        return ToolResult.failure("rename_path_conflict", "Rename target already exists.", category=ErrorCategory.FILESYSTEM)
    except FileNotFoundError:
        return ToolResult.failure("library_entry_missing", "Managed library file is missing.", category=ErrorCategory.FILESYSTEM)
    except (OSError, ValueError) as exc:
        return ToolResult.failure("library_rename_failed", str(exc), category=ErrorCategory.FILESYSTEM)


def _entry_selection(context: ToolContext, input_data: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]] | ToolResult:
    resolved = _paths(context, input_data, require_library_root=True)
    if isinstance(resolved, ToolResult):
        return resolved
    db_path, explicit_root = resolved
    selector_count = sum(bool(input_data.get(name)) for name in ("entry_id", "path"))
    if selector_count != 1:
        return ToolResult.failure(
            "invalid_library_selector",
            "Provide exactly one of entry_id or path.",
            category=ErrorCategory.VALIDATION,
        )
    try:
        path = _managed_path(context, input_data, str(input_data["path"])) if input_data.get("path") else None
        entry = library_content.resolve_entry(db_path, entry_id=input_data.get("entry_id"), path=path)
    except (OSError, ValueError) as exc:
        return ToolResult.failure("library_entry_lookup_failed", str(exc), category=ErrorCategory.FILESYSTEM)
    if entry is None:
        return ToolResult.failure(
            "library_entry_not_found",
            "No managed library entry matches the selector.",
            category=ErrorCategory.DATABASE,
        )
    entry_path = Path(str(entry.get("local_path") or entry.get("trash_path"))).resolve()
    root = explicit_root or _matching_library_root(context, input_data, entry_path)
    if root is None:
        return ToolResult.failure(
            "unsafe_library_path",
            "Managed entry is outside configured library roots.",
            category=ErrorCategory.FILESYSTEM,
        )
    return db_path, root, entry


def _paths(context: ToolContext, input_data: dict[str, Any], *, require_library_root: bool) -> tuple[Path, Path | None] | ToolResult:
    db_path = normalize_path(str(input_data["db_path"]), env=context.env, cwd=context.cwd) if input_data.get("db_path") else context.db_path
    if db_path is None:
        return ToolResult.failure("missing_db_path", "Provide db_path or set MEDIAGENT_DB_PATH.", category=ErrorCategory.VALIDATION)
    try:
        ensure_inside(db_path, context.allowed_write_roots())
        root = None
        if input_data.get("library_root"):
            root = normalize_path(str(input_data["library_root"]), env=context.env, cwd=context.cwd)
            ensure_inside(root, context.allowed_write_roots())
        elif require_library_root:
            default_root = default_library_root(data_dir=context.data_dir, library_dir=context.library_dir)
            ensure_inside(default_root, context.allowed_write_roots())
        return db_path, root
    except (PathSafetyError, ValueError) as exc:
        return ToolResult.failure("unsafe_path", str(exc), category=ErrorCategory.FILESYSTEM)


def _managed_path(context: ToolContext, input_data: dict[str, Any], raw_path: str) -> Path:
    path = normalize_path(raw_path, env=context.env, cwd=context.cwd)
    root = _matching_library_root(context, input_data, path)
    if root is None:
        raise PathSafetyError("Path is outside configured library roots.")
    ensure_inside(path, [root])
    return path


def _matching_library_root(context: ToolContext, input_data: dict[str, Any], path: Path) -> Path | None:
    for root in _library_roots(context, input_data):
        try:
            ensure_inside(path, [root])
            return root
        except PathSafetyError:
            continue
    return None


def _library_roots(context: ToolContext, input_data: dict[str, Any]) -> list[Path]:
    roots: list[Path] = []
    if input_data.get("library_root"):
        roots.append(normalize_path(str(input_data["library_root"]), env=context.env, cwd=context.cwd))
    if context.library_dir:
        roots.append(context.library_dir.resolve())
    elif context.data_dir:
        roots.append((context.data_dir / "library").resolve())
    for name, value in context.env.items():
        if name.startswith("MEDIAGENT_") and name.endswith("_LIBRARY_DIR") and value:
            roots.append(normalize_path(str(value), env=context.env, cwd=context.cwd))
    unique: dict[str, Path] = {str(root): root for root in roots}
    return list(unique.values())


def _unsafe_plan_paths(context: ToolContext, input_data: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    unsafe: list[str] = []
    roots = _library_roots(context, input_data)
    for action in plan.get("actions", []):
        path = Path(str(action["path"])).resolve()
        try:
            ensure_inside(path, roots)
        except PathSafetyError:
            unsafe.append(str(path))
    return unsafe
