"""Asset-level remove, restore, and permanent trash purge operations."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mediagent.core import assets, db, library_content


DEFAULT_TRASH_RETENTION_DAYS = 30


def remove_asset(
    db_path: Path,
    *,
    asset_id: str,
    library_root: Path,
    reason: str | None = None,
) -> dict[str, Any]:
    """Move every active representation of one Asset into managed trash."""

    _prepare_database(db_path, dry_run=False)
    asset_id = str(assets.load_asset(db_path, asset_id)["id"])
    root = _library_root(library_root)
    namespace = _managed_namespace(root, create=False)
    _recover_incomplete_moves(db_path, asset_id=asset_id, root=root, namespace=namespace)
    _recover_restore_duplicate_cleanups(
        db_path, asset_id=asset_id, root=root, namespace=namespace
    )
    asset, entries = _asset_entries(db_path, asset_id)
    if asset["state"] == "purged":
        raise ValueError("A permanently purged Asset cannot be removed or restored.")
    if asset["state"] == "removed":
        return {"changed": False, "result": "already_removed", "asset": asset, "entries": entries}
    namespace = _managed_namespace(root, create=True)
    operation_id = f"arm_{uuid.uuid4().hex}"
    plans: list[dict[str, Any]] = []
    for entry in entries:
        if entry["state"] != "active":
            continue
        source = _lexical_path(Path(str(entry["local_path"])))
        _require_safe_regular_content(source, root, str(entry["checksum"]))
        target = _lexical_path(namespace / operation_id / str(entry["id"]) / source.name)
        _require_safe_path(target, namespace, allow_missing=True)
        if target.exists():
            raise FileExistsError(str(target))
        plans.append({"entry": entry, "source": source, "target": target})
    if not plans:
        raise ValueError("The Asset has no active managed representations.")
    _require_unique_plan_paths(plans)

    now = datetime.now(UTC).isoformat()
    _insert_asset_operation(
        db_path,
        operation_id=operation_id,
        asset_id=asset["id"],
        operation_type="remove",
        state="planned",
        reason=reason,
        metadata={
            "entry_count": len(plans),
            "plans": [
                {
                    "entry_id": str(plan["entry"]["id"]),
                    "source": str(plan["source"]),
                    "target": str(plan["target"]),
                    "checksum": str(plan["entry"]["checksum"]),
                }
                for plan in plans
            ],
        },
        created_at=now,
    )
    moved: list[tuple[Path, Path]] = []
    try:
        for plan in plans:
            plan["target"].parent.mkdir(parents=True, exist_ok=True)
            _require_safe_path(plan["target"], namespace, allow_missing=True)
            _require_safe_regular_content(
                plan["source"], root, str(plan["entry"]["checksum"])
            )
            os.replace(plan["source"], plan["target"])
            moved.append((plan["target"], plan["source"]))
    except Exception as exc:
        rollback_failures = _rollback_moves(moved)
        if rollback_failures:
            raise RuntimeError(
                "Asset removal failed and filesystem rollback was incomplete."
            ) from exc
        _try_finish_asset_operation(db_path, operation_id, state="failed")
        raise

    try:
        with db.connect(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for plan in plans:
                entry_id = str(plan["entry"]["id"])
                connection.execute(
                    """
                    UPDATE library_entries
                    SET state = 'removed', trash_path = ?, removed_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (str(plan["target"]), now, now, entry_id),
                )
                connection.execute(
                    "UPDATE media_files SET local_path = ?, updated_at = ? WHERE library_entry_id = ?",
                    (str(plan["target"]), now, entry_id),
                )
                connection.execute(
                    "UPDATE asset_representations SET active = 0, last_seen_at = ? WHERE library_entry_id = ?",
                    (now, entry_id),
                )
            connection.execute(
                """
                UPDATE assets
                SET state = 'removed', removed_at = ?, purged_at = NULL,
                    purge_reason = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, asset["id"]),
            )
            connection.execute(
                "UPDATE asset_operations SET state = 'completed', completed_at = ? WHERE id = ?",
                (now, operation_id),
            )
    except Exception:
        rollback_failures = _rollback_moves(moved)
        if rollback_failures:
            raise RuntimeError(
                "Asset removal database update failed and filesystem rollback was incomplete."
            )
        _try_finish_asset_operation(db_path, operation_id, state="failed")
        raise
    return {
        "changed": True,
        "result": "removed",
        "operation_id": operation_id,
        "asset": assets.load_asset(db_path, asset["id"]),
        "paths_moved": len(plans),
        "trash_paths": [str(plan["target"]) for plan in plans],
    }


def restore_asset(db_path: Path, *, asset_id: str, library_root: Path) -> dict[str, Any]:
    """Restore every recoverable representation of one removed Asset."""

    _prepare_database(db_path, dry_run=False)
    asset_id = str(assets.load_asset(db_path, asset_id)["id"])
    root = _library_root(library_root)
    namespace = _managed_namespace(root, create=False)
    _recover_incomplete_moves(db_path, asset_id=asset_id, root=root, namespace=namespace)
    _recover_restore_duplicate_cleanups(
        db_path, asset_id=asset_id, root=root, namespace=namespace
    )
    asset, entries = _asset_entries(db_path, asset_id)
    if asset["state"] == "purged":
        raise ValueError("A permanently purged Asset cannot be restored.")
    if asset["state"] == "active":
        return {
            "changed": False,
            "result": "already_active",
            "asset": asset,
            "entries": entries,
            "purged_representations": sum(entry["state"] == "purged" for entry in entries),
        }
    plans: list[dict[str, Any]] = []
    for entry in entries:
        if entry["state"] != "removed":
            continue
        source = _lexical_path(Path(str(entry.get("trash_path") or "")))
        target = _lexical_path(Path(str(entry["local_path"])))
        source_boundary = _trash_boundary(source, root=root, namespace=namespace)
        _require_safe_regular_content(source, source_boundary, str(entry["checksum"]))
        _require_safe_path(target, root, allow_missing=True)
        if target.exists():
            _require_safe_regular_content(target, root, str(entry["checksum"]))
            action = "discard_duplicate"
        else:
            action = "move"
        plans.append(
            {
                "entry": entry,
                "source": source,
                "source_boundary": source_boundary,
                "target": target,
                "action": action,
            }
        )
    if not plans:
        raise ValueError("The Asset has no recoverable removed representations.")
    _require_unique_plan_paths(plans)

    operation_id = f"ars_{uuid.uuid4().hex}"
    now = datetime.now(UTC).isoformat()
    _insert_asset_operation(
        db_path,
        operation_id=operation_id,
        asset_id=asset["id"],
        operation_type="restore",
        state="planned",
        reason=None,
        metadata={
            "entry_count": len(plans),
            "plans": [
                {
                    "entry_id": str(plan["entry"]["id"]),
                    "source": str(plan["source"]),
                    "target": str(plan["target"]),
                    "checksum": str(plan["entry"]["checksum"]),
                    "action": str(plan["action"]),
                }
                for plan in plans
            ],
        },
        created_at=now,
    )
    moved: list[tuple[Path, Path]] = []
    duplicate_cleanup: list[dict[str, Any]] = []
    try:
        for plan in plans:
            if plan["action"] == "move":
                plan["target"].parent.mkdir(parents=True, exist_ok=True)
                _require_safe_path(plan["target"], root, allow_missing=True)
                _require_safe_regular_content(
                    plan["source"], plan["source_boundary"], str(plan["entry"]["checksum"])
                )
                os.replace(plan["source"], plan["target"])
                moved.append((plan["target"], plan["source"]))
            else:
                _require_safe_regular_content(
                    plan["source"], plan["source_boundary"], str(plan["entry"]["checksum"])
                )
                _require_safe_regular_content(
                    plan["target"], root, str(plan["entry"]["checksum"])
                )
                duplicate_cleanup.append(plan)
    except Exception as exc:
        rollback_failures = _rollback_moves(moved)
        if rollback_failures:
            raise RuntimeError(
                "Asset restoration failed and filesystem rollback was incomplete."
            ) from exc
        _try_finish_asset_operation(db_path, operation_id, state="failed")
        raise

    try:
        with db.connect(db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for plan in plans:
                entry_id = str(plan["entry"]["id"])
                connection.execute(
                    """
                    UPDATE library_entries
                    SET state = 'active', trash_path = NULL, removed_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, entry_id),
                )
                connection.execute(
                    "UPDATE media_files SET local_path = ?, updated_at = ? WHERE library_entry_id = ?",
                    (str(plan["target"]), now, entry_id),
                )
                connection.execute(
                    "UPDATE asset_representations SET active = 1, last_seen_at = ? WHERE library_entry_id = ?",
                    (now, entry_id),
                )
            connection.execute(
                """
                UPDATE assets
                SET state = 'active', removed_at = NULL, purged_at = NULL,
                    purge_reason = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, asset["id"]),
            )
            operation_state = "cleanup_pending" if duplicate_cleanup else "completed"
            connection.execute(
                "UPDATE asset_operations SET state = ?, completed_at = ? WHERE id = ?",
                (operation_state, None if duplicate_cleanup else now, operation_id),
            )
    except Exception:
        rollback_failures = _rollback_moves(moved)
        if rollback_failures:
            raise RuntimeError(
                "Asset restoration database update failed and filesystem rollback was incomplete."
            )
        _try_finish_asset_operation(db_path, operation_id, state="failed")
        raise
    cleanup_failures = _finish_restore_duplicate_cleanup(
        db_path,
        operation_id=operation_id,
        asset_id=str(asset["id"]),
        plans=duplicate_cleanup,
        root=root,
        namespace=namespace,
    )
    return {
        "changed": True,
        "result": "restored",
        "operation_id": operation_id,
        "asset": assets.load_asset(db_path, asset["id"]),
        "paths_restored": len(plans),
        "purged_representations": sum(entry["state"] == "purged" for entry in entries),
        "duplicate_trash_cleanup_failed": len(cleanup_failures),
    }


def purge_trash(
    db_path: Path,
    *,
    library_root: Path,
    retention_days: int,
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Permanently delete due removed representations while retaining tombstones."""

    if retention_days < 0:
        raise ValueError("Trash retention days must not be negative.")
    _prepare_database(db_path, dry_run=dry_run)
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(days=retention_days)
    root = _library_root(library_root)
    namespace = _managed_namespace(root, create=False)
    if not dry_run:
        _recover_incomplete_purges(
            db_path,
            root=root,
            namespace=namespace,
            completed_at=current.isoformat(),
        )
        _finalize_ready_purged_assets(db_path, completed_at=current.isoformat())
    due_assets = _due_removed_assets(db_path, cutoff.isoformat())
    candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for asset_id in due_assets:
        asset, entries = _asset_entries(db_path, asset_id)
        for entry in entries:
            if entry["state"] != "removed":
                continue
            removed_at = entry.get("removed_at") or asset.get("removed_at")
            if not removed_at or str(removed_at) > cutoff.isoformat():
                continue
            if not entry.get("trash_path"):
                blocked.append({"asset_id": asset_id, "entry_id": entry["id"], "reason": "entry_not_removed"})
                continue
            paths = [
                _lexical_path(Path(str(entry["trash_path"]))),
                *_legacy_duplicate_paths(db_path, entry_id=str(entry["id"])),
            ]
            paths = list(dict.fromkeys(paths))
            entry_candidates: list[dict[str, Any]] = []
            try:
                for path in paths:
                    boundary = _trash_boundary(path, root=root, namespace=namespace)
                    _require_safe_regular_content(path, boundary, str(entry["checksum"]))
                    file_stat = path.lstat()
                    entry_candidates.append(
                        {
                            "asset_id": asset_id,
                            "entry_id": str(entry["id"]),
                            "path": path,
                            "boundary": boundary,
                            "checksum": str(entry["checksum"]),
                            "size_bytes": int(entry["size_bytes"]),
                            "inode": (int(file_stat.st_dev), int(file_stat.st_ino)),
                            "link_count": int(file_stat.st_nlink),
                        }
                    )
            except (OSError, ValueError) as exc:
                blocked.append(
                    {"asset_id": asset_id, "entry_id": entry["id"], "reason": str(exc)}
                )
                continue
            candidates.extend(entry_candidates)
    path_counts: dict[Path, int] = {}
    for candidate in candidates:
        path_counts[candidate["path"]] = path_counts.get(candidate["path"], 0) + 1
    duplicate_paths = {path for path, count in path_counts.items() if count > 1}
    if duplicate_paths:
        blocked.extend(
            {
                "asset_id": candidate["asset_id"],
                "entry_id": candidate["entry_id"],
                "reason": "duplicate_trash_path",
            }
            for candidate in candidates
            if candidate["path"] in duplicate_paths
        )
    blocked_assets = {item["asset_id"] for item in blocked}
    candidates = [item for item in candidates if item["asset_id"] not in blocked_assets]
    inode_counts: dict[tuple[int, int], int] = {}
    for candidate in candidates:
        inode_counts[candidate["inode"]] = inode_counts.get(candidate["inode"], 0) + 1
    reclaimable_inodes = {
        candidate["inode"]
        for candidate in candidates
        if candidate["link_count"] <= inode_counts[candidate["inode"]]
    }
    logical_bytes = sum(item["size_bytes"] for item in candidates)
    seen_inodes: set[tuple[int, int]] = set()
    reclaimable_bytes = 0
    for item in candidates:
        if item["inode"] in reclaimable_inodes and item["inode"] not in seen_inodes:
            reclaimable_bytes += item["size_bytes"]
        seen_inodes.add(item["inode"])
    plan = {
        "dry_run": dry_run,
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "assets_due": len(due_assets),
        "assets_ready": len({item["asset_id"] for item in candidates}),
        "paths_ready": len(candidates),
        "blocked": blocked,
        "logical_bytes": logical_bytes,
        "bytes_expected_reclaimed": reclaimable_bytes,
    }
    if dry_run:
        return {**plan, "paths_unlinked": 0, "assets_purged": 0, "failed": []}

    failed: list[dict[str, Any]] = []
    purged_assets: set[str] = set()
    paths_unlinked = 0
    for asset_id in dict.fromkeys(item["asset_id"] for item in candidates):
        asset_candidates = [item for item in candidates if item["asset_id"] == asset_id]
        operation_id = f"prg_{uuid.uuid4().hex}"
        started = current.isoformat()
        _insert_asset_operation(
            db_path,
            operation_id=operation_id,
            asset_id=asset_id,
            operation_type="purge",
            state="planned",
            reason="trash retention expired",
            metadata={
                "entry_count": len(asset_candidates),
                "retention_days": retention_days,
                "plans": [
                    {
                        "entry_id": candidate["entry_id"],
                        "path": str(candidate["path"]),
                        "checksum": candidate["checksum"],
                    }
                    for candidate in asset_candidates
                ],
            },
            created_at=started,
        )
        failed_entries: set[str] = set()
        for candidate in asset_candidates:
            try:
                _require_safe_regular_content(
                    candidate["path"], candidate["boundary"], candidate["checksum"]
                )
                candidate["path"].unlink()
                paths_unlinked += 1
            except (OSError, ValueError) as exc:
                failed_entries.add(candidate["entry_id"])
                failed.append(
                    {"asset_id": asset_id, "entry_id": candidate["entry_id"], "reason": str(exc)}
                )
        for entry_id in dict.fromkeys(item["entry_id"] for item in asset_candidates):
            if entry_id in failed_entries:
                continue
            _mark_entry_purged(
                db_path,
                entry_id=entry_id,
                operation_id=operation_id,
                completed_at=datetime.now(UTC).isoformat(),
            )
        if failed_entries:
            _finish_asset_operation(
                db_path,
                operation_id,
                state="partial",
                completed_at=datetime.now(UTC).isoformat(),
            )
            continue
        completed = datetime.now(UTC).isoformat()
        if _finalize_purged_asset_if_complete(
            db_path,
            asset_id=asset_id,
            completed_at=completed,
            operation_id=operation_id,
        ):
            purged_assets.add(asset_id)
        else:
            _finish_asset_operation(
                db_path,
                operation_id,
                state="completed",
                completed_at=completed,
            )
    return {
        **plan,
        "paths_unlinked": paths_unlinked,
        "assets_purged": len(purged_assets),
        "failed": failed,
    }


def _asset_entries(db_path: Path, asset_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    asset = assets.load_asset(db_path, asset_id)
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT le.*, cb.checksum, cb.size_bytes, cb.mime_type,
                   ar.representation_role
            FROM asset_representations ar
            JOIN library_entries le ON le.id = ar.library_entry_id
            JOIN content_blobs cb ON cb.id = le.content_blob_id
            WHERE ar.asset_id = ?
            ORDER BY le.id
            """,
            (asset["id"],),
        ).fetchall()
    return asset, [dict(row) for row in rows]


def _due_removed_assets(db_path: Path, cutoff: str) -> list[str]:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT a.id
            FROM assets a
            LEFT JOIN asset_representations ar ON ar.asset_id = a.id
            LEFT JOIN library_entries le ON le.id = ar.library_entry_id
            WHERE a.state IN ('active', 'removed')
              AND le.state = 'removed'
              AND COALESCE(le.removed_at, a.removed_at) <= ?
            GROUP BY a.id
            ORDER BY a.id
            """,
            (cutoff,),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _legacy_duplicate_paths(
    db_path: Path,
    *,
    entry_id: str,
    include_missing: bool = False,
) -> list[Path]:
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT metadata_json
            FROM library_operations
            WHERE library_entry_id = ?
              AND operation_type = 'remove'
              AND reason = 'legacy trash import'
              AND state = 'completed'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (entry_id,),
        ).fetchone()
    if row is None:
        return []
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError:
        return []
    values = metadata.get("duplicate_candidate_paths") if isinstance(metadata, dict) else None
    if not isinstance(values, list):
        return []
    paths = [
        _lexical_path(Path(value))
        for value in values
        if isinstance(value, str) and value
    ]
    if include_missing:
        return paths
    existing: list[Path] = []
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        existing.append(path)
    return existing


def _library_root(library_root: Path) -> Path:
    root = library_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Library root does not exist: {root}")
    return root


def _prepare_database(db_path: Path, *, dry_run: bool) -> None:
    """Require an existing database and never migrate it during a preview."""

    if not db_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    if not dry_run:
        db.initialize_database(db_path)
        return
    try:
        version = db.get_schema_version(db_path)
    except sqlite3.DatabaseError as exc:
        raise ValueError("Database is not initialized for Asset lifecycle operations.") from exc
    if version != db.SCHEMA_VERSION:
        raise ValueError("Database schema must be upgraded with 'mediagent init' before previewing trash.")


def _managed_namespace(root: Path, *, create: bool) -> Path:
    if create:
        library_content.prepare_managed_trash(root)
    namespace = _lexical_path(root / library_content.MANAGED_TRASH_DIRECTORY)
    _require_safe_path(namespace, root, allow_missing=not create)
    return namespace


def _trash_boundary(path: Path, *, root: Path, namespace: Path) -> Path:
    """Resolve the owned boundary for managed or reconciled legacy trash."""

    candidate = _lexical_path(path)
    try:
        candidate.relative_to(namespace)
    except ValueError:
        trash_root = _lexical_path(root / ".trash")
        _require_safe_path(candidate, trash_root, allow_missing=True)
        return trash_root
    _require_safe_path(candidate, namespace, allow_missing=True)
    return namespace


def _lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _require_safe_path(path: Path, boundary: Path, *, allow_missing: bool) -> None:
    candidate = _lexical_path(path)
    root = _lexical_path(boundary)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Managed content path is outside its configured boundary.") from exc

    current = root
    paths = [root, *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1))]
    for index, component in enumerate(paths):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise ValueError("Managed content is missing or is not a regular file.")
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Managed content path contains a symbolic link.")
        if index < len(paths) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("Managed content parent is not a directory.")


def _require_safe_regular_content(path: Path, boundary: Path, checksum: str) -> None:
    _require_safe_path(path, boundary, allow_missing=False)
    _require_regular_content(path, checksum)


def _require_regular_content(path: Path, checksum: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("Managed content is missing or is not a regular file.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Managed content is missing or is not a regular file.")
    actual, _size = library_content.sha256_checksum(path)
    if actual != checksum:
        raise ValueError("Managed content checksum does not match its database identity.")


def _rollback_moves(moved: list[tuple[Path, Path]]) -> list[str]:
    failures: list[str] = []
    for target, source in reversed(moved):
        try:
            if target.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        except OSError:
            failures.append(str(source))
    return failures


def _require_unique_plan_paths(plans: list[dict[str, Any]]) -> None:
    sources = [plan["source"] for plan in plans]
    targets = [plan["target"] for plan in plans]
    if len(set(sources)) != len(sources) or len(set(targets)) != len(targets):
        raise ValueError("Asset representations contain duplicate managed paths.")


def _try_finish_asset_operation(db_path: Path, operation_id: str, *, state: str) -> None:
    try:
        _finish_asset_operation(
            db_path,
            operation_id,
            state=state,
            completed_at=datetime.now(UTC).isoformat(),
        )
    except Exception:
        return


def _finish_restore_duplicate_cleanup(
    db_path: Path,
    *,
    operation_id: str,
    asset_id: str,
    plans: list[dict[str, Any]],
    root: Path,
    namespace: Path,
) -> list[str]:
    failures: list[str] = []
    for plan in plans:
        if str(plan.get("action") or "") != "discard_duplicate":
            continue
        source = _lexical_path(Path(str(plan.get("source") or "")))
        target = _lexical_path(Path(str(plan.get("target") or "")))
        checksum = str(plan.get("checksum") or plan.get("entry", {}).get("checksum") or "")
        try:
            if not checksum:
                raise ValueError("An Asset restore cleanup has no checksum.")
            _validate_restore_cleanup_plan(
                db_path,
                canonical_asset_id=asset_id,
                plan=plan,
                source=source,
                target=target,
                checksum=checksum,
            )
            _require_safe_regular_content(target, root, checksum)
            source_boundary = _trash_boundary(source, root=root, namespace=namespace)
            _require_safe_path(source, source_boundary, allow_missing=True)
            if source.exists():
                _require_safe_regular_content(source, source_boundary, checksum)
                source.unlink()
        except (OSError, ValueError):
            failures.append(str(source))
    if not failures:
        _finish_asset_operation(
            db_path,
            operation_id,
            state="completed",
            completed_at=datetime.now(UTC).isoformat(),
        )
    return failures


def _recover_restore_duplicate_cleanups(
    db_path: Path,
    *,
    asset_id: str,
    root: Path,
    namespace: Path,
) -> None:
    lineage = _asset_lineage_ids(db_path, asset_id)
    placeholders = ",".join("?" for _ in lineage)
    with db.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, asset_id, metadata_json
            FROM asset_operations
            WHERE asset_id IN ({placeholders})
              AND operation_type = 'restore'
              AND state = 'cleanup_pending'
            ORDER BY created_at, id
            """,
            tuple(lineage),
        ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("An incomplete Asset restore cleanup has an invalid journal.") from exc
        plans = metadata.get("plans") if isinstance(metadata, dict) else None
        if not isinstance(plans, list):
            raise RuntimeError("An incomplete Asset restore cleanup has no recovery journal.")
        failures = _finish_restore_duplicate_cleanup(
            db_path,
            operation_id=str(row["id"]),
            asset_id=asset_id,
            plans=[plan for plan in plans if isinstance(plan, dict)],
            root=root,
            namespace=namespace,
        )
        if failures:
            raise RuntimeError("An incomplete Asset restore cleanup could not be completed safely.")


def _validate_restore_cleanup_plan(
    db_path: Path,
    *,
    canonical_asset_id: str,
    plan: dict[str, Any],
    source: Path,
    target: Path,
    checksum: str,
) -> None:
    entry_id = str(plan.get("entry_id") or plan.get("entry", {}).get("id") or "")
    if not entry_id or plan.get("action") != "discard_duplicate":
        raise ValueError("An Asset restore cleanup has an invalid journal.")
    with db.connect(db_path) as connection:
        entry = connection.execute(
            """
            SELECT le.state, le.local_path, cb.checksum, ar.asset_id
            FROM library_entries le
            JOIN content_blobs cb ON cb.id = le.content_blob_id
            JOIN asset_representations ar ON ar.library_entry_id = le.id
            WHERE le.id = ?
            """,
            (entry_id,),
        ).fetchone()
        represented_asset_id = (
            assets.resolve_asset_id(connection, str(entry["asset_id"])) if entry else None
        )
    if (
        entry is None
        or represented_asset_id != canonical_asset_id
        or str(entry["state"]) != "active"
        or str(entry["checksum"]) != checksum
        or target != _lexical_path(Path(str(entry["local_path"])))
        or not _restore_cleanup_source_matches_history(
            db_path,
            entry_id=entry_id,
            source=source,
            target=target,
        )
    ):
        raise ValueError("An Asset restore cleanup does not match current managed state.")


def _restore_cleanup_source_matches_history(
    db_path: Path,
    *,
    entry_id: str,
    source: Path,
    target: Path,
) -> bool:
    if source.parent.name == entry_id and source.name == target.name:
        return True
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM library_operations
            WHERE library_entry_id = ?
              AND operation_type = 'remove'
              AND state = 'completed'
              AND target_path = ?
            LIMIT 1
            """,
            (entry_id, str(source)),
        ).fetchone()
    return row is not None


def _recover_incomplete_moves(
    db_path: Path,
    *,
    asset_id: str,
    root: Path,
    namespace: Path,
) -> None:
    lineage = _asset_lineage_ids(db_path, asset_id)
    placeholders = ",".join("?" for _ in lineage)
    with db.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, asset_id, operation_type, metadata_json
            FROM asset_operations
            WHERE asset_id IN ({placeholders})
              AND operation_type IN ('remove', 'restore')
              AND state = 'planned'
            ORDER BY created_at, id
            """,
            tuple(lineage),
        ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError("An incomplete Asset operation has an invalid recovery journal.") from exc
        plans = metadata.get("plans") if isinstance(metadata, dict) else None
        if not isinstance(plans, list):
            raise RuntimeError("An incomplete Asset operation has no recovery journal.")
        operation_type = str(row["operation_type"])
        for plan in reversed(plans):
            if not isinstance(plan, dict):
                raise RuntimeError("An incomplete Asset operation has an invalid recovery journal.")
            source = _lexical_path(Path(str(plan.get("source") or "")))
            target = _lexical_path(Path(str(plan.get("target") or "")))
            checksum = str(plan.get("checksum") or "")
            if not checksum:
                raise RuntimeError("An incomplete Asset operation has an invalid recovery journal.")
            _validate_recovery_plan(
                db_path,
                canonical_asset_id=asset_id,
                operation_id=str(row["id"]),
                operation_type=operation_type,
                plan=plan,
                source=source,
                target=target,
                checksum=checksum,
                namespace=namespace,
            )
            if plan.get("action") == "discard_duplicate":
                continue
            source_boundary = (
                root
                if operation_type == "remove"
                else _trash_boundary(source, root=root, namespace=namespace)
            )
            target_boundary = namespace if operation_type == "remove" else root
            _require_safe_path(source, source_boundary, allow_missing=True)
            _require_safe_path(target, target_boundary, allow_missing=True)
            source_exists = source.exists()
            target_exists = target.exists()
            if source_exists and target_exists:
                raise RuntimeError("An incomplete Asset operation has conflicting source and target files.")
            if source_exists:
                _require_safe_regular_content(source, source_boundary, checksum)
                continue
            if not target_exists:
                raise RuntimeError("An incomplete Asset operation is missing both recovery paths.")
            _require_safe_regular_content(target, target_boundary, checksum)
            source.parent.mkdir(parents=True, exist_ok=True)
            _require_safe_path(source, source_boundary, allow_missing=True)
            os.replace(target, source)
        _finish_asset_operation(
            db_path,
            str(row["id"]),
            state="failed",
            completed_at=datetime.now(UTC).isoformat(),
        )


def _validate_recovery_plan(
    db_path: Path,
    *,
    canonical_asset_id: str,
    operation_id: str,
    operation_type: str,
    plan: dict[str, Any],
    source: Path,
    target: Path,
    checksum: str,
    namespace: Path,
) -> None:
    entry_id = str(plan.get("entry_id") or "")
    if not entry_id or operation_type not in {"remove", "restore"}:
        raise RuntimeError("An incomplete Asset operation has an invalid recovery journal.")
    with db.connect(db_path) as connection:
        entry = connection.execute(
            """
            SELECT le.state, le.local_path, le.trash_path, cb.checksum, ar.asset_id
            FROM library_entries le
            JOIN content_blobs cb ON cb.id = le.content_blob_id
            JOIN asset_representations ar ON ar.library_entry_id = le.id
            WHERE le.id = ?
            """,
            (entry_id,),
        ).fetchone()
        represented_asset_id = (
            assets.resolve_asset_id(connection, str(entry["asset_id"])) if entry else None
        )
    if entry is None or represented_asset_id != canonical_asset_id:
        raise RuntimeError("An incomplete Asset operation references an unrelated entry.")
    if str(entry["checksum"]) != checksum:
        raise RuntimeError("An incomplete Asset operation checksum does not match its entry.")
    if operation_type == "remove":
        expected_target = _lexical_path(namespace / operation_id / entry_id / source.name)
        valid = (
            str(entry["state"]) == "active"
            and source == _lexical_path(Path(str(entry["local_path"])))
            and target == expected_target
            and not plan.get("action")
        )
    else:
        valid = (
            str(entry["state"]) == "removed"
            and source == _lexical_path(Path(str(entry["trash_path"] or "")))
            and target == _lexical_path(Path(str(entry["local_path"])))
            and plan.get("action") in {"move", "discard_duplicate"}
        )
    if not valid:
        raise RuntimeError("An incomplete Asset operation does not match current managed state.")


def _asset_lineage_ids(db_path: Path, asset_id: str) -> list[str]:
    """Return a canonical Asset and every alias merged into its lineage."""

    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            WITH RECURSIVE lineage(id) AS (
                SELECT ?
                UNION
                SELECT a.id
                FROM assets a
                JOIN lineage parent ON a.merged_into_asset_id = parent.id
            )
            SELECT id FROM lineage ORDER BY id
            """,
            (asset_id,),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _recover_incomplete_purges(
    db_path: Path,
    *,
    root: Path,
    namespace: Path,
    completed_at: str,
) -> None:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, asset_id, metadata_json
            FROM asset_operations
            WHERE operation_type = 'purge' AND state IN ('planned', 'partial')
            ORDER BY created_at, id
            """
        ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError:
            continue
        plans = metadata.get("plans") if isinstance(metadata, dict) else None
        if not isinstance(plans, list):
            continue
        plans_by_entry: dict[str, list[dict[str, Any]]] = {}
        for plan in plans:
            if isinstance(plan, dict) and plan.get("entry_id"):
                plans_by_entry.setdefault(str(plan["entry_id"]), []).append(plan)
        for entry_id, entry_plans in plans_by_entry.items():
            with db.connect(db_path) as connection:
                entry = connection.execute(
                    """
                    SELECT le.state, le.trash_path, cb.checksum, ar.asset_id
                    FROM library_entries le
                    JOIN content_blobs cb ON cb.id = le.content_blob_id
                    JOIN asset_representations ar ON ar.library_entry_id = le.id
                    WHERE le.id = ?
                    """,
                    (entry_id,),
                ).fetchone()
                journal_asset_id = assets.resolve_asset_id(connection, str(row["asset_id"]))
                represented_asset_id = (
                    assets.resolve_asset_id(connection, str(entry["asset_id"]))
                    if entry is not None
                    else None
                )
            if (
                entry is None
                or journal_asset_id is None
                or represented_asset_id != journal_asset_id
                or str(entry["state"]) != "removed"
            ):
                continue
            checksum = str(entry["checksum"])
            primary_path = _lexical_path(Path(str(entry["trash_path"] or "")))
            allowed_paths = {
                primary_path,
                *_legacy_duplicate_paths(
                    db_path,
                    entry_id=entry_id,
                    include_missing=True,
                ),
            }
            required_paths = {
                primary_path,
                *_legacy_duplicate_paths(db_path, entry_id=entry_id),
            }
            journal_paths: list[Path] = []
            valid_journal = True
            for plan in entry_plans:
                path_text = str(plan.get("path") or "")
                if not path_text or str(plan.get("checksum") or "") != checksum:
                    valid_journal = False
                    break
                path = _lexical_path(Path(path_text))
                if path not in allowed_paths:
                    valid_journal = False
                    break
                journal_paths.append(path)
            journal_path_set = set(journal_paths)
            if (
                not valid_journal
                or not required_paths.issubset(journal_path_set)
                or not journal_path_set.issubset(allowed_paths)
            ):
                continue
            existing_paths: list[tuple[Path, Path]] = []
            try:
                for path in journal_paths:
                    boundary = _trash_boundary(path, root=root, namespace=namespace)
                    _require_safe_path(path, boundary, allow_missing=True)
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        continue
                    _require_safe_regular_content(path, boundary, checksum)
                    existing_paths.append((path, boundary))
                for path, _boundary in existing_paths:
                    path.unlink()
                _mark_entry_purged(
                    db_path,
                    entry_id=entry_id,
                    operation_id=str(row["id"]),
                    completed_at=completed_at,
                )
            except (OSError, ValueError):
                continue
        planned_entry_ids = list(plans_by_entry)
        if not planned_entry_ids:
            continue
        placeholders = ",".join("?" for _ in planned_entry_ids)
        with db.connect(db_path) as connection:
            remaining = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) FROM library_entries
                    WHERE id IN ({placeholders}) AND state != 'purged'
                    """,
                    tuple(planned_entry_ids),
                ).fetchone()[0]
            )
        if remaining:
            continue
        finalized = _finalize_purged_asset_if_complete(
            db_path,
            asset_id=str(row["asset_id"]),
            completed_at=completed_at,
            operation_id=str(row["id"]),
        )
        if not finalized:
            _finish_asset_operation(
                db_path,
                str(row["id"]),
                state="completed",
                completed_at=completed_at,
            )


def _finalize_ready_purged_assets(db_path: Path, *, completed_at: str) -> None:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT a.id
            FROM assets a
            WHERE a.state = 'removed'
              AND EXISTS (
                  SELECT 1 FROM asset_representations ar
                  WHERE ar.asset_id = a.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM asset_representations ar
                  JOIN library_entries le ON le.id = ar.library_entry_id
                  WHERE ar.asset_id = a.id AND le.state != 'purged'
              )
            ORDER BY a.id
            """
        ).fetchall()
    for row in rows:
        _finalize_purged_asset_if_complete(
            db_path,
            asset_id=str(row["id"]),
            completed_at=completed_at,
            operation_id=None,
        )


def _finalize_purged_asset_if_complete(
    db_path: Path,
    *,
    asset_id: str,
    completed_at: str,
    operation_id: str | None,
) -> bool:
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        remaining = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM asset_representations ar
                JOIN library_entries le ON le.id = ar.library_entry_id
                WHERE ar.asset_id = ? AND le.state != 'purged'
                """,
                (asset_id,),
            ).fetchone()[0]
        )
        if remaining:
            return False
        connection.execute(
            """
            UPDATE assets
            SET state = 'purged', purged_at = COALESCE(purged_at, ?),
                purge_reason = COALESCE(purge_reason, 'trash retention expired'),
                updated_at = ?
            WHERE id = ? AND state = 'removed'
            """,
            (completed_at, completed_at, asset_id),
        )
        if operation_id is not None:
            connection.execute(
                "UPDATE asset_operations SET state = 'completed', completed_at = ? WHERE id = ?",
                (completed_at, operation_id),
            )
    return True


def _insert_asset_operation(
    db_path: Path,
    *,
    operation_id: str,
    asset_id: str,
    operation_type: str,
    state: str,
    reason: str | None,
    metadata: dict[str, Any],
    created_at: str,
) -> None:
    with db.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO asset_operations (
                id, asset_id, operation_type, state, reason,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                asset_id,
                operation_type,
                state,
                reason,
                json.dumps(metadata, sort_keys=True),
                created_at,
            ),
        )


def _finish_asset_operation(db_path: Path, operation_id: str, *, state: str, completed_at: str) -> None:
    with db.connect(db_path) as connection:
        connection.execute(
            "UPDATE asset_operations SET state = ?, completed_at = ? WHERE id = ?",
            (state, completed_at, operation_id),
        )


def _mark_entry_purged(
    db_path: Path,
    *,
    entry_id: str,
    operation_id: str,
    completed_at: str,
) -> None:
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE library_entries
            SET state = 'purged', trash_path = NULL, updated_at = ?
            WHERE id = ?
            """,
            (completed_at, entry_id),
        )
        connection.execute(
            """
            UPDATE media_files
            SET local_path = NULL, status = 'skipped', file_health = 'purged', updated_at = ?
            WHERE library_entry_id = ?
            """,
            (completed_at, entry_id),
        )
        connection.execute(
            "UPDATE asset_representations SET active = 0, last_seen_at = ? WHERE library_entry_id = ?",
            (completed_at, entry_id),
        )
        connection.execute(
            """
            INSERT INTO library_operations (
                id, operation_type, library_entry_id, state, reason,
                metadata_json, created_at, completed_at
            ) VALUES (?, 'purge', ?, 'completed', 'trash retention expired', '{}', ?, ?)
            """,
            (f"{operation_id}_{entry_id}", entry_id, completed_at, completed_at),
        )
