"""Global content identity and library projection helpers."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, ZipInfo

from mediagent.core import db


GENERAL_PRESENTATION_KEY = "media"


def sha256_checksum(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size_bytes += len(chunk)
    return f"sha256:{digest.hexdigest()}", size_bytes


def ensure_target_write_safe(db_path: Path, target_path: Path) -> None:
    """Refuse in-place replacement of a path shared by multiple source rows."""

    if not db_path.exists():
        return
    resolved = str(target_path.resolve())
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT le.id, le.state, COUNT(mf.id) AS source_count
            FROM library_entries le
            LEFT JOIN media_files mf ON mf.library_entry_id = le.id
            WHERE le.local_path = ?
            GROUP BY le.id
            """,
            (resolved,),
        ).fetchone()
    if row is not None and row["state"] == "removed":
        raise ValueError("Refusing to overwrite an explicitly removed library entry.")
    if row is not None and target_path.exists() and int(row["source_count"]) > 1:
        raise ValueError(
            "Refusing to overwrite globally shared content in place; "
            "the source references must be separated before replacement."
        )


def presentation_key(
    *,
    platform: str,
    remote_id: str,
    file_key: str,
    library_relative_path: str | None,
    mime_type: str | None,
) -> str:
    parts = {part.lower() for part in Path(library_relative_path or "").parts}
    mime = str(mime_type or "").split(";", 1)[0].lower()
    if "comic-pages" in parts:
        return f"comic-source:{platform}:{remote_id}:{file_key}"
    if "comic" in parts or mime == "application/vnd.comicbook+zip":
        return f"comic:{platform}:{remote_id}:{file_key}"
    return GENERAL_PRESENTATION_KEY


def adopt_media_file(db_path: Path, *, file_id: int) -> dict[str, Any]:
    """Attach a downloaded media-file row to global blob and presentation state.

    General photo/video/audio files with identical SHA-256 content share one
    library entry and therefore one scanner-visible path. Contextual comic
    files keep separate entries, but use hard links when the filesystem allows
    physical deduplication.
    """

    row = _media_file_row(db_path, file_id)
    if row is None:
        raise ValueError(f"Unknown media file: {file_id}")
    checksum = _normalized_checksum(row.get("checksum"))
    if row.get("status") != "downloaded" or not checksum:
        return {"adopted": False, "reason": "not_downloaded_or_missing_checksum", **row}

    current_path = Path(str(row["local_path"])).resolve() if row.get("local_path") else None
    relative_path = str(row.get("library_relative_path") or "") or None
    key = presentation_key(
        platform=str(row["platform"]),
        remote_id=str(row["remote_id"]),
        file_key=str(row["file_key"]),
        library_relative_path=relative_path,
        mime_type=row.get("mime_type"),
    )
    now = datetime.now(UTC).isoformat()
    blob_id = f"blob_{checksum.removeprefix('sha256:')}"
    entry: dict[str, Any] | None = None
    entry_content_changed = False

    with db.connect(db_path) as connection:
        existing_blob = connection.execute(
            "SELECT id, size_bytes FROM content_blobs WHERE checksum = ?",
            (checksum,),
        ).fetchone()
        size_bytes = int(row.get("size_bytes") or 0)
        if existing_blob and int(existing_blob["size_bytes"]) not in (0, size_bytes) and size_bytes:
            raise ValueError("Checksum identity has conflicting file sizes.")
        connection.execute(
            """
            INSERT INTO content_blobs (id, checksum, size_bytes, mime_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(checksum) DO UPDATE SET
                size_bytes = CASE WHEN excluded.size_bytes > 0 THEN excluded.size_bytes ELSE content_blobs.size_bytes END,
                mime_type = COALESCE(content_blobs.mime_type, excluded.mime_type),
                updated_at = excluded.updated_at
            """,
            (blob_id, checksum, size_bytes, row.get("mime_type"), now, now),
        )
        blob = connection.execute(
            "SELECT id FROM content_blobs WHERE checksum = ?",
            (checksum,),
        ).fetchone()
        blob_id = str(blob["id"])
        existing_entry = None
        if row.get("library_entry_id"):
            existing_entry = connection.execute(
                """
                SELECT id, content_blob_id, state, local_path,
                       library_relative_path, trash_path, display_name_override
                FROM library_entries
                WHERE id = ?
                """,
                (row["library_entry_id"],),
            ).fetchone()
            if existing_entry and str(existing_entry["content_blob_id"]) != blob_id:
                if current_path is None or not current_path.is_file():
                    return {"adopted": False, "reason": "missing_revised_local_file", **row}
                _require_checksum(current_path, checksum)
                reference_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM media_files WHERE library_entry_id = ?",
                        (existing_entry["id"],),
                    ).fetchone()[0]
                )
                if str(existing_entry["state"]) == "removed":
                    raise ValueError("Removed library entry received different content and remains suppressed.")
                if reference_count > 1:
                    raise ValueError("Shared library content changed in place; refusing to alter other source references.")
                connection.execute(
                    """
                    UPDATE library_entries
                    SET content_blob_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (blob_id, now, existing_entry["id"]),
                )
                entry_content_changed = True
                existing_entry = connection.execute(
                    """
                    SELECT id, content_blob_id, state, local_path,
                           library_relative_path, trash_path, display_name_override
                    FROM library_entries
                    WHERE id = ?
                    """,
                    (row["library_entry_id"],),
                ).fetchone()
        if existing_entry is None:
            existing_entry = connection.execute(
                """
                SELECT id, content_blob_id, state, local_path,
                       library_relative_path, trash_path, display_name_override
                FROM library_entries
                WHERE content_blob_id = ? AND presentation_key = ?
                """,
                (blob_id, key),
            ).fetchone()
        if existing_entry:
            entry = dict(existing_entry)

    deduplicated = False
    hardlinked = False
    bytes_reclaimed = 0
    if entry is None:
        if current_path is None or not current_path.is_file():
            return {"adopted": False, "reason": "missing_local_file", **row}
        source = _active_blob_path(db_path, blob_id=blob_id, exclude=current_path)
        if source is not None:
            hardlinked = _replace_with_hardlink(source=source, target=current_path)
            if hardlinked:
                bytes_reclaimed = int(row.get("size_bytes") or 0)
        entry_id = f"entry_{uuid.uuid4().hex}"
        with db.connect(db_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO library_entries (
                    id, content_blob_id, presentation_key, state, local_path,
                    library_relative_path, created_at, updated_at
                )
                VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (entry_id, blob_id, key, str(current_path), relative_path, now, now),
            )
            selected = connection.execute(
                """
                SELECT id, state, local_path, library_relative_path, trash_path,
                       display_name_override
                FROM library_entries
                WHERE content_blob_id = ? AND presentation_key = ?
                """,
                (blob_id, key),
            ).fetchone()
        if selected is None:
            raise ValueError("Unable to create or resolve the global library entry.")
        entry = dict(selected)
        if str(entry["id"]) == entry_id:
            stored_path = str(current_path)
            stored_relative = relative_path
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    UPDATE media_files
                    SET library_entry_id = ?, local_path = ?, library_relative_path = ?,
                        checksum = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (entry_id, stored_path, stored_relative, checksum, now, file_id),
                )
            return {
                "adopted": True,
                "file_id": file_id,
                "blob_id": blob_id,
                "entry_id": entry_id,
                "presentation_key": key,
                "state": "active",
                "target_path": stored_path,
                "library_relative_path": stored_relative,
                "deduplicated": False,
                "hardlinked": hardlinked,
                "bytes_reclaimed": bytes_reclaimed,
            }

    if entry is not None:
        canonical_path = Path(str(entry["local_path"])).resolve()
        if entry["state"] == "removed":
            trash_path = Path(str(entry["trash_path"])).resolve() if entry.get("trash_path") else None
            if current_path is not None and current_path.is_file():
                if trash_path is not None and not trash_path.exists():
                    _move_file(current_path, trash_path)
                elif trash_path is None or current_path != trash_path:
                    current_path.unlink()
                deduplicated = True
                bytes_reclaimed = int(row.get("size_bytes") or 0)
            current_path = trash_path if trash_path and trash_path.exists() else None
        elif entry_content_changed:
            if current_path is None or not current_path.is_file():
                return {"adopted": False, "reason": "missing_revised_local_file", **row}
            _require_checksum(current_path, checksum)
            if not _same_file(current_path, canonical_path):
                _move_file(current_path, canonical_path)
            current_path = canonical_path
        else:
            if canonical_path.is_file():
                if current_path is not None and current_path.is_file() and current_path != canonical_path:
                    if _same_file(current_path, canonical_path):
                        current_path.unlink()
                        deduplicated = True
                    else:
                        _require_checksum(current_path, checksum)
                        _require_checksum(canonical_path, checksum)
                        current_path.unlink()
                        deduplicated = True
                        bytes_reclaimed = int(row.get("size_bytes") or 0)
                current_path = canonical_path
            elif current_path is not None and current_path.is_file():
                _require_checksum(current_path, checksum)
                _move_file(current_path, canonical_path)
                current_path = canonical_path
            else:
                return {"adopted": False, "reason": "missing_local_file", **row}

    stored_path = str(current_path) if current_path is not None else None
    stored_relative = entry.get("library_relative_path")
    with db.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE media_files
            SET library_entry_id = ?, local_path = ?, library_relative_path = ?,
                checksum = ?, updated_at = ?
            WHERE id = ?
            """,
            (entry["id"], stored_path, stored_relative, checksum, now, file_id),
        )
    return {
        "adopted": True,
        "file_id": file_id,
        "blob_id": blob_id,
        "entry_id": entry["id"],
        "presentation_key": key,
        "state": entry["state"],
        "target_path": stored_path,
        "library_relative_path": stored_relative,
        "deduplicated": deduplicated,
        "hardlinked": hardlinked,
        "bytes_reclaimed": bytes_reclaimed,
    }


def scan_plan(db_path: Path) -> dict[str, Any]:
    rows = _downloaded_rows(db_path)
    files: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    hash_cache: dict[tuple[int, int, int, int], tuple[str, int]] = {}
    for row in rows:
        raw_path = row.get("local_path")
        if not raw_path:
            missing.append({"file_id": row["id"], "reason": "missing_local_path"})
            continue
        path = Path(str(raw_path)).resolve()
        if _in_trash(path):
            continue
        if not path.is_file():
            missing.append({"file_id": row["id"], "path": str(path), "reason": "missing_file"})
            continue
        stat = path.stat()
        physical_identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        checksum_and_size = hash_cache.get(physical_identity)
        if checksum_and_size is None:
            checksum_and_size = sha256_checksum(path)
            hash_cache[physical_identity] = checksum_and_size
        checksum, size_bytes = checksum_and_size
        files.append(
            {
                **row,
                "path": str(path),
                "actual_checksum": checksum,
                "actual_size_bytes": size_bytes,
                "presentation_key": presentation_key(
                    platform=str(row["platform"]),
                    remote_id=str(row["remote_id"]),
                    file_key=str(row["file_key"]),
                    library_relative_path=row.get("library_relative_path"),
                    mime_type=row.get("mime_type"),
                ),
            }
        )

    canonical_by_group: dict[tuple[str, str], dict[str, Any]] = {}
    canonical_by_blob: dict[str, dict[str, Any]] = {}
    actions: list[dict[str, Any]] = []
    duplicate_paths: set[str] = set()
    hardlink_paths: set[str] = set()
    bytes_reclaimable = 0
    for item in sorted(files, key=lambda value: int(value["id"])):
        checksum = str(item["actual_checksum"])
        group = (checksum, str(item["presentation_key"]))
        canonical = canonical_by_group.setdefault(group, item)
        blob_canonical = canonical_by_blob.setdefault(checksum, item)
        action = "register"
        target = str(item["path"])
        canonical_path = str(canonical["path"])
        if target != canonical_path:
            action = "collapse"
            if target not in duplicate_paths:
                duplicate_paths.add(target)
                if not _same_file(Path(target), Path(canonical_path)):
                    bytes_reclaimable += int(item["actual_size_bytes"])
        elif str(item["presentation_key"]) != str(blob_canonical["presentation_key"]) and target != str(blob_canonical["path"]):
            blob_canonical_path = str(blob_canonical["path"])
            action = "hardlink_existing" if _same_file(Path(target), Path(blob_canonical_path)) else "hardlink"
            if action == "hardlink" and target not in hardlink_paths:
                hardlink_paths.add(target)
                bytes_reclaimable += int(item["actual_size_bytes"])
        actions.append(
            {
                "file_id": item["id"],
                "platform": item["platform"],
                "remote_id": item["remote_id"],
                "path": target,
                "checksum": checksum,
                "presentation_key": item["presentation_key"],
                "action": action,
                "canonical_path": canonical_path,
                "recorded_checksum": item.get("checksum"),
            }
        )

    checksum_groups = {item["actual_checksum"] for item in files}
    checksum_updates_required = sum(
        _normalized_checksum(item.get("checksum")) != item["actual_checksum"]
        for item in files
    )
    return {
        "summary": {
            "tracked_rows": len(rows),
            "files_hashed": len(files),
            "physical_files_hashed": len(hash_cache),
            "missing_files": len(missing),
            "content_blobs": len(checksum_groups),
            "checksum_updates_required": checksum_updates_required,
            "duplicate_paths": len(duplicate_paths),
            "hardlink_candidates": len(hardlink_paths),
            "bytes_reclaimable": bytes_reclaimable,
        },
        "missing": missing,
        "actions": actions,
        "_files": files,
    }


def apply_scan_plan(db_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    db.initialize_database(db_path)
    adopted = 0
    collapsed = 0
    hardlinked = 0
    bytes_reclaimed = 0
    now = datetime.now(UTC).isoformat()
    for item in plan.get("_files", []):
        with db.connect(db_path) as connection:
            connection.execute(
                """
                UPDATE media_files
                SET checksum = ?, size_bytes = ?, file_health = 'valid',
                    verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    item["actual_checksum"],
                    item["actual_size_bytes"],
                    now,
                    now,
                    item["id"],
                ),
            )
        result = adopt_media_file(db_path, file_id=int(item["id"]))
        if result.get("adopted"):
            adopted += 1
        if result.get("deduplicated"):
            collapsed += 1
        if result.get("hardlinked"):
            hardlinked += 1
        bytes_reclaimed += int(result.get("bytes_reclaimed") or 0)
    return {
        **{key: value for key, value in plan.items() if key != "_files"},
        "applied": {
            "rows_adopted": adopted,
            "paths_collapsed": collapsed,
            "paths_hardlinked": hardlinked,
            "bytes_reclaimed": bytes_reclaimed,
        },
    }


def resolve_entry(
    db_path: Path,
    *,
    entry_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any] | None:
    db.initialize_database(db_path)
    with db.connect(db_path) as connection:
        row = None
        if entry_id:
            row = connection.execute(
                "SELECT * FROM library_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        elif path is not None:
            resolved = str(path.resolve())
            row = connection.execute(
                "SELECT * FROM library_entries WHERE local_path = ? OR trash_path = ?",
                (resolved, resolved),
            ).fetchone()
            if row is None:
                media_file = connection.execute(
                    "SELECT id, checksum, size_bytes FROM media_files WHERE local_path = ? ORDER BY id LIMIT 1",
                    (resolved,),
                ).fetchone()
                if media_file:
                    file_id = int(media_file["id"])
                    if not _normalized_checksum(media_file["checksum"]) and path.is_file():
                        checksum, size_bytes = sha256_checksum(path)
                        connection.execute(
                            "UPDATE media_files SET checksum = ?, size_bytes = ? WHERE id = ?",
                            (checksum, size_bytes, file_id),
                        )
    if row is not None:
        return _entry_details(db_path, str(row["id"]))
    if path is not None:
        with db.connect(db_path) as connection:
            media_file = connection.execute(
                "SELECT id FROM media_files WHERE local_path = ? ORDER BY id LIMIT 1",
                (str(path.resolve()),),
            ).fetchone()
        if media_file:
            adoption = adopt_media_file(db_path, file_id=int(media_file["id"]))
            if adoption.get("adopted"):
                return _entry_details(db_path, str(adoption["entry_id"]))
    return None


def resolve_removal_entry(db_path: Path, removal_id: str) -> dict[str, Any] | None:
    operation = _operation(db_path, removal_id)
    if operation is None or operation["operation_type"] != "remove":
        return None
    return _entry_details(db_path, str(operation["library_entry_id"]))


def remove_entry(
    db_path: Path,
    *,
    entry_id: str,
    library_root: Path,
    reason: str | None = None,
    external_ref: str | None = None,
) -> dict[str, Any]:
    entry = _entry_details(db_path, entry_id)
    if entry is None:
        raise ValueError(f"Unknown library entry: {entry_id}")
    if entry["state"] == "removed":
        operation = _latest_operation(db_path, entry_id=entry_id, operation_type="remove")
        return {
            "changed": False,
            "result": "already_removed",
            "entry": entry,
            "removal_id": operation["id"] if operation else None,
        }

    source = Path(str(entry["local_path"])).resolve()
    planned = _latest_operation(db_path, entry_id=entry_id, operation_type="remove")
    if planned and planned["state"] == "planned" and planned.get("target_path"):
        planned_target = Path(str(planned["target_path"])).resolve()
        if planned_target.is_file():
            _require_checksum(planned_target, str(entry["checksum"]))
            if source.is_file() and not _same_file(source, planned_target):
                _require_checksum(source, str(entry["checksum"]))
                source.unlink()
            recovered_at = datetime.now(UTC).isoformat()
            _complete_remove(
                db_path,
                entry_id=entry_id,
                operation_id=str(planned["id"]),
                trash_path=planned_target,
                completed_at=recovered_at,
            )
            return {
                "changed": True,
                "result": "recovered_removed",
                "removal_id": planned["id"],
                "original_path": str(source),
                "trash_path": str(planned_target),
                "entry": _entry_details(db_path, entry_id),
            }
    if not source.is_file():
        raise FileNotFoundError(str(source))
    operation_id = f"rmv_{uuid.uuid4().hex}"
    relative = _safe_relative_path(entry, source=source, library_root=library_root)
    trash = (library_root / ".trash" / "mediagent" / operation_id / relative).resolve()
    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO library_operations (
                id, operation_type, library_entry_id, state, original_path,
                target_path, reason, external_ref, metadata_json, created_at
            ) VALUES (?, 'remove', ?, 'planned', ?, ?, ?, ?, '{}', ?)
            """,
            (operation_id, entry_id, str(source), str(trash), reason, external_ref, now),
        )
    _move_file(source, trash)
    _complete_remove(
        db_path,
        entry_id=entry_id,
        operation_id=operation_id,
        trash_path=trash,
        completed_at=now,
    )
    return {
        "changed": True,
        "result": "removed",
        "removal_id": operation_id,
        "original_path": str(source),
        "trash_path": str(trash),
        "entry": _entry_details(db_path, entry_id),
    }


def restore_entry(
    db_path: Path,
    *,
    removal_id: str | None = None,
    entry_id: str | None = None,
) -> dict[str, Any]:
    removal: dict[str, Any] | None = None
    if removal_id:
        removal = _operation(db_path, removal_id)
        if removal is None or removal["operation_type"] != "remove":
            raise ValueError(f"Unknown removal: {removal_id}")
        entry_id = str(removal["library_entry_id"])
    if not entry_id:
        raise ValueError("Provide removal_id or entry_id.")
    entry = _entry_details(db_path, entry_id)
    if entry is None:
        raise ValueError(f"Unknown library entry: {entry_id}")
    if entry["state"] == "active":
        return {"changed": False, "result": "already_active", "entry": entry, "removal_id": removal_id}
    if removal is not None:
        current_trash = Path(str(entry.get("trash_path") or "")).resolve()
        removal_target = Path(str(removal.get("target_path") or "")).resolve()
        if current_trash != removal_target:
            raise ValueError("Removal identifier is stale for the entry's current removed state.")

    source = Path(str(entry["trash_path"])).resolve() if entry.get("trash_path") else None
    target = Path(str(entry["local_path"])).resolve()
    checksum = str(entry["checksum"])
    if target.is_file():
        _require_checksum(target, checksum)
        if source is not None and source.is_file() and not _same_file(source, target):
            _require_checksum(source, checksum)
            source.unlink()
    elif source is not None and source.is_file():
        _require_checksum(source, checksum)
        _move_file(source, target)
    else:
        raise FileNotFoundError(str(source or target))

    operation_id = f"rst_{uuid.uuid4().hex}"
    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO library_operations (
                id, operation_type, library_entry_id, state, original_path,
                target_path, metadata_json, created_at, completed_at
            ) VALUES (?, 'restore', ?, 'completed', ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                entry_id,
                str(source) if source else None,
                str(target),
                json.dumps({"removal_id": removal_id}, sort_keys=True),
                now,
                now,
            ),
        )
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
            (str(target), now, entry_id),
        )
    return {
        "changed": True,
        "result": "restored",
        "restore_id": operation_id,
        "removal_id": removal_id,
        "restored_path": str(target),
        "entry": _entry_details(db_path, entry_id),
    }


def rename_entry(
    db_path: Path,
    *,
    entry_id: str,
    name: str,
    external_ref: str | None = None,
) -> dict[str, Any]:
    entry = _entry_details(db_path, entry_id)
    if entry is None:
        raise ValueError(f"Unknown library entry: {entry_id}")
    if entry["state"] != "active":
        raise ValueError("Removed library entries must be restored before rename.")
    source = Path(str(entry["local_path"])).resolve()
    filename, display_name = _renamed_filename(source, name)
    target = source.with_name(filename).resolve()
    if target == source:
        return {"changed": False, "result": "already_named", "entry": entry}
    content_changed = source.suffix.lower() == ".cbz"
    planned = _latest_operation(db_path, entry_id=entry_id, operation_type="rename")
    if (
        planned
        and planned["state"] == "planned"
        and planned.get("target_path")
        and Path(str(planned["target_path"])).resolve() == target
        and target.is_file()
    ):
        if content_changed:
            _require_cbz_title(target, display_name)
        else:
            _require_checksum(target, str(entry["checksum"]))
        if source.is_file() and not _same_file(source, target):
            if not content_changed:
                _require_checksum(source, str(entry["checksum"]))
            source.unlink()
        return _complete_rename(
            db_path,
            entry=entry,
            entry_id=entry_id,
            operation_id=str(planned["id"]),
            source=source,
            target=target,
            display_name=display_name,
            content_changed=content_changed,
            completed_at=datetime.now(UTC).isoformat(),
            recovered=True,
        )
    if not source.is_file():
        raise FileNotFoundError(str(source))
    if target.exists():
        raise FileExistsError(str(target))
    with db.connect(db_path) as connection:
        reserved = connection.execute(
            "SELECT id FROM library_entries WHERE local_path = ? AND id != ?",
            (str(target), entry_id),
        ).fetchone()
    if reserved is not None:
        raise FileExistsError(str(target))

    operation_id = f"ren_{uuid.uuid4().hex}"
    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO library_operations (
                id, operation_type, library_entry_id, state, original_path,
                target_path, external_ref, metadata_json, created_at
            ) VALUES (?, 'rename', ?, 'planned', ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                entry_id,
                str(source),
                str(target),
                external_ref,
                json.dumps({"display_name": display_name}, sort_keys=True),
                now,
            ),
        )

    if content_changed:
        _rewrite_cbz_title(source=source, target=target, title=display_name)
        source.unlink()
    else:
        _move_file(source, target)
    return _complete_rename(
        db_path,
        entry=entry,
        entry_id=entry_id,
        operation_id=operation_id,
        source=source,
        target=target,
        display_name=display_name,
        content_changed=content_changed,
        completed_at=now,
        recovered=False,
    )


def _complete_remove(
    db_path: Path,
    *,
    entry_id: str,
    operation_id: str,
    trash_path: Path,
    completed_at: str,
) -> None:
    with db.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE library_entries
            SET state = 'removed', trash_path = ?, removed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (str(trash_path), completed_at, completed_at, entry_id),
        )
        connection.execute(
            "UPDATE media_files SET local_path = ?, updated_at = ? WHERE library_entry_id = ?",
            (str(trash_path), completed_at, entry_id),
        )
        connection.execute(
            "UPDATE library_operations SET state = 'completed', completed_at = ? WHERE id = ?",
            (completed_at, operation_id),
        )


def _complete_rename(
    db_path: Path,
    *,
    entry: dict[str, Any],
    entry_id: str,
    operation_id: str,
    source: Path,
    target: Path,
    display_name: str,
    content_changed: bool,
    completed_at: str,
    recovered: bool,
) -> dict[str, Any]:
    checksum, size_bytes = sha256_checksum(target)
    relative = Path(str(entry.get("library_relative_path") or source.name))
    renamed_relative = (relative.parent / target.name).as_posix()
    blob_id = f"blob_{checksum.removeprefix('sha256:')}"
    with db.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO content_blobs (id, checksum, size_bytes, mime_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(checksum) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (blob_id, checksum, size_bytes, entry.get("mime_type"), completed_at, completed_at),
        )
        blob = connection.execute("SELECT id FROM content_blobs WHERE checksum = ?", (checksum,)).fetchone()
        connection.execute(
            """
            UPDATE library_entries
            SET content_blob_id = ?, local_path = ?, library_relative_path = ?,
                display_name_override = ?, updated_at = ?
            WHERE id = ?
            """,
            (blob["id"], str(target), renamed_relative, display_name, completed_at, entry_id),
        )
        connection.execute(
            """
            UPDATE media_files
            SET file_key = CASE WHEN file_key = ? THEN ? ELSE file_key END,
                local_path = ?, library_relative_path = ?, checksum = ?,
                size_bytes = ?, updated_at = ?
            WHERE library_entry_id = ?
            """,
            (
                f"local:{source}",
                f"local:{target}",
                str(target),
                renamed_relative,
                checksum,
                size_bytes,
                completed_at,
                entry_id,
            ),
        )
        connection.execute(
            "UPDATE library_operations SET state = 'completed', completed_at = ? WHERE id = ?",
            (completed_at, operation_id),
        )
    return {
        "changed": True,
        "result": "renamed",
        "rename_id": operation_id,
        "old_path": str(source),
        "new_path": str(target),
        "content_changed": content_changed,
        "recovered": recovered,
        "entry": _entry_details(db_path, entry_id),
    }


def _media_file_row(db_path: Path, file_id: int) -> dict[str, Any] | None:
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT mf.*, mi.platform, mi.remote_id
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            WHERE mf.id = ?
            """,
            (file_id,),
        ).fetchone()
    return dict(row) if row else None


def _entry_details(db_path: Path, entry_id: str) -> dict[str, Any] | None:
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT le.*, cb.checksum, cb.size_bytes, cb.mime_type,
                   COUNT(mf.id) AS source_file_count
            FROM library_entries le
            JOIN content_blobs cb ON cb.id = le.content_blob_id
            LEFT JOIN media_files mf ON mf.library_entry_id = le.id
            WHERE le.id = ?
            GROUP BY le.id
            """,
            (entry_id,),
        ).fetchone()
    return dict(row) if row else None


def _operation(db_path: Path, operation_id: str) -> dict[str, Any] | None:
    with db.connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM library_operations WHERE id = ?",
            (operation_id,),
        ).fetchone()
    return dict(row) if row else None


def _latest_operation(db_path: Path, *, entry_id: str, operation_type: str) -> dict[str, Any] | None:
    with db.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM library_operations
            WHERE library_entry_id = ? AND operation_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (entry_id, operation_type),
        ).fetchone()
    return dict(row) if row else None


def _downloaded_rows(db_path: Path) -> list[dict[str, Any]]:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT mf.*, mi.platform, mi.remote_id
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            WHERE mf.status = 'downloaded' AND mf.local_path IS NOT NULL
            ORDER BY mf.id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _active_blob_path(db_path: Path, *, blob_id: str, exclude: Path) -> Path | None:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT local_path FROM library_entries
            WHERE content_blob_id = ? AND state = 'active'
            ORDER BY created_at, id
            """,
            (blob_id,),
        ).fetchall()
    for row in rows:
        path = Path(str(row["local_path"])).resolve()
        if path != exclude and path.is_file():
            return path
    return None


def _normalized_checksum(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text.startswith("sha256:") or len(text) != len("sha256:") + 64:
        return None
    digest = text.removeprefix("sha256:")
    if any(character not in "0123456789abcdef" for character in digest):
        return None
    return text


def _require_checksum(path: Path, expected: str) -> None:
    actual, _ = sha256_checksum(path)
    if actual != expected:
        raise ValueError(f"Content checksum changed for tracked library file: {path}")


def _require_cbz_title(path: Path, expected: str) -> None:
    try:
        with ZipFile(path, "r") as archive:
            root = ElementTree.fromstring(archive.read("ComicInfo.xml"))
    except (BadZipFile, KeyError, OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"CBZ rename recovery found an invalid ComicInfo.xml: {path}") from exc
    actual = root.findtext("Title")
    if actual != expected:
        raise ValueError(f"CBZ rename recovery title does not match the requested name: {path}")


def _same_file(left: Path, right: Path) -> bool:
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False


def _replace_with_hardlink(*, source: Path, target: Path) -> bool:
    if _same_file(source, target):
        return False
    temporary = target.with_name(f".{target.name}.mediagent-link-{uuid.uuid4().hex}.partial")
    try:
        os.link(source, temporary)
        os.replace(temporary, target)
        return True
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        if exc.errno in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return False
        raise


def _move_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temporary = target.with_name(f".{target.name}.mediagent-move-{uuid.uuid4().hex}.partial")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        source.unlink()


def _safe_relative_path(entry: dict[str, Any], *, source: Path, library_root: Path) -> Path:
    root = library_root.resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError("Library entry is outside the configured library root.") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Library entry has an unsafe relative path.")
    return relative


def _renamed_filename(source: Path, name: str) -> tuple[str, str]:
    value = str(name or "").strip()
    if not value:
        raise ValueError("Rename requires a non-empty name.")
    if value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError("Rename accepts a file name, not a path.")
    if any(ord(character) < 32 for character in value):
        raise ValueError("Rename contains control characters.")
    suffix = source.suffix
    display_name = value
    if suffix and value.lower().endswith(suffix.lower()):
        display_name = value[: -len(suffix)].rstrip()
        filename = value
    else:
        filename = value + suffix
    filename = filename.strip(" .")
    display_name = display_name.strip(" .")
    if not filename or not display_name:
        raise ValueError("Rename produced an empty file name.")
    if len(filename.encode("utf-8")) > 240:
        raise ValueError("Rename exceeds the 240-byte file-name limit.")
    return filename, display_name


def _rewrite_cbz_title(*, source: Path, target: Path, title: str) -> None:
    partial = target.with_name(f".{target.name}.mediagent-rename-{uuid.uuid4().hex}.partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(source, "r") as input_archive, ZipFile(partial, "w", allowZip64=True) as output_archive:
            found_comic_info = False
            for info in input_archive.infolist():
                content = input_archive.read(info.filename)
                if info.filename == "ComicInfo.xml":
                    root = ElementTree.fromstring(content)
                    title_element = root.find("Title")
                    if title_element is None:
                        title_element = ElementTree.SubElement(root, "Title")
                    title_element.text = title
                    ElementTree.indent(root, space="  ")
                    content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
                    found_comic_info = True
                copied = ZipInfo(info.filename, date_time=info.date_time)
                copied.compress_type = info.compress_type
                copied.comment = info.comment
                copied.extra = info.extra
                copied.internal_attr = info.internal_attr
                copied.external_attr = info.external_attr
                copied.create_system = info.create_system
                output_archive.writestr(copied, content)
            if not found_comic_info:
                raise ValueError("CBZ rename requires a root-level ComicInfo.xml.")
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _in_trash(path: Path) -> bool:
    return any(part.lower() == ".trash" for part in path.parts)
