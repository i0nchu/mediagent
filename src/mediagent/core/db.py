"""SQLite persistence for runs and media state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "8"
SQLITE_BUSY_TIMEOUT_MILLISECONDS = 30_000


def initialize_database(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as connection:
        connection.executescript(SCHEMA_SQL)
        _ensure_media_items_schema(connection)
        _ensure_media_files_schema(connection)
        _ensure_link_queue_schema(connection)
        connection.execute(
            """
            INSERT INTO schema_meta (key, value)
            VALUES ('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (SCHEMA_VERSION,),
        )


def connect(db_path: Path) -> sqlite3.Connection:
    # Recurring source timers share one SQLite database.  A short writer must
    # be allowed to finish instead of making another otherwise healthy run
    # fail immediately with ``database is locked``.
    connection = sqlite3.connect(
        db_path,
        timeout=SQLITE_BUSY_TIMEOUT_MILLISECONDS / 1_000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
    return connection


def get_schema_version(db_path: Path) -> str | None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    return row["value"] if row else None


def insert_run(
    db_path: Path,
    *,
    run_type: str,
    name: str,
    status: str,
    summary: dict[str, Any],
    error: dict[str, Any] | None,
    dry_run: bool,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO runs (
                id, run_type, name, status, started_at, ended_at,
                dry_run, summary_json, error_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run_type,
                name,
                status,
                started_at or now,
                ended_at or now,
                1 if dry_run else 0,
                json.dumps(summary, sort_keys=True),
                json.dumps(error, sort_keys=True) if error else None,
            ),
        )
    return run_id


def upsert_media_item(db_path: Path, item: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    platform = item["platform"]
    remote_id = item["remote_id"]
    metadata = _sanitize_link_resolution_for_storage(item.get("metadata", {}))

    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT id, status FROM media_items WHERE platform = ? AND remote_id = ?",
            (platform, remote_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO media_items (
                platform, remote_id, source_url, author_id, author_name,
                media_type, status, metadata_json, created_at, updated_at,
                source_availability
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, remote_id) DO UPDATE SET
                source_url = excluded.source_url,
                author_id = excluded.author_id,
                author_name = excluded.author_name,
                media_type = excluded.media_type,
                status = CASE
                    WHEN media_items.status IN ('downloaded', 'skipped', 'failed', 'partial')
                    THEN media_items.status
                    ELSE excluded.status
                END,
                metadata_json = excluded.metadata_json,
                source_availability = CASE
                    WHEN excluded.source_availability = 'unknown' THEN media_items.source_availability
                    ELSE excluded.source_availability
                END,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                remote_id,
                item.get("source_url"),
                item.get("author_id"),
                item.get("author_name"),
                item["media_type"],
                item.get("status", "discovered"),
                json.dumps(metadata, sort_keys=True),
                now,
                now,
                item.get("source_availability", "unknown"),
            ),
        )
        row = connection.execute(
            "SELECT id, status FROM media_items WHERE platform = ? AND remote_id = ?",
            (platform, remote_id),
        ).fetchone()

    return {
        "platform": platform,
        "remote_id": remote_id,
        "id": row["id"],
        "status": row["status"],
        "is_new": existing is None,
        "previous_status": existing["status"] if existing else None,
    }


def get_media_statuses(
    db_path: Path,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    if not db_path.exists():
        return {}
    statuses: dict[tuple[str, str], str] = {}
    with connect(db_path) as connection:
        for item in items:
            row = connection.execute(
                "SELECT status FROM media_items WHERE platform = ? AND remote_id = ?",
                (item["platform"], item["remote_id"]),
            ).fetchone()
            if row:
                statuses[(item["platform"], item["remote_id"])] = row["status"]
    return statuses


def update_media_item_status(
    db_path: Path,
    *,
    platform: str,
    remote_id: str,
    status: str,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    downloaded_at = now if status == "downloaded" else None
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT id, status FROM media_items WHERE platform = ? AND remote_id = ?",
            (platform, remote_id),
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown media item: {platform}/{remote_id}")
        connection.execute(
            """
            UPDATE media_items
            SET status = ?,
                updated_at = ?,
                downloaded_at = CASE
                    WHEN ? = 'downloaded' THEN ?
                    ELSE downloaded_at
                END
            WHERE platform = ? AND remote_id = ?
            """,
            (status, now, status, downloaded_at, platform, remote_id),
        )
        updated = connection.execute(
            """
            SELECT id, platform, remote_id, status, downloaded_at, updated_at
            FROM media_items
            WHERE platform = ? AND remote_id = ?
            """,
            (platform, remote_id),
        ).fetchone()
    return dict(updated)


def get_sync_cursor(
    db_path: Path,
    *,
    platform: str,
    cursor_name: str,
) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT cursor_value, metadata_json, updated_at
            FROM sync_cursors
            WHERE platform = ? AND cursor_name = ?
            """,
            (platform, cursor_name),
        ).fetchone()
    if not row:
        return None
    return {
        "platform": platform,
        "cursor_name": cursor_name,
        "cursor_value": row["cursor_value"],
        "metadata": json.loads(row["metadata_json"]),
        "updated_at": row["updated_at"],
    }


def set_sync_cursor(
    db_path: Path,
    *,
    platform: str,
    cursor_name: str,
    cursor_value: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO sync_cursors (
                platform, cursor_name, cursor_value, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(platform, cursor_name) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                platform,
                cursor_name,
                cursor_value,
                json.dumps(metadata or {}, sort_keys=True),
                now,
            ),
        )
    return {
        "platform": platform,
        "cursor_name": cursor_name,
        "cursor_value": cursor_value,
        "metadata": metadata or {},
        "updated_at": now,
    }


def commit_collection_snapshot(
    db_path: Path,
    *,
    provider: str,
    collection_key: str,
    targets: list[dict[str, Any]],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically replace one collector membership snapshot.

    Callers must only invoke this after every remote page was fetched successfully.
    An incomplete collection must leave the previous active memberships untouched.
    """
    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets:
        target_type = str(target.get("target_type") or "").strip()
        target_id = str(target.get("target_id") or "").strip()
        if not target_type or not target_id:
            raise ValueError("Collection targets require target_type and target_id.")
        normalized[(target_type, target_id)] = {
            "target_type": target_type,
            "target_id": target_id,
            "metadata": target.get("metadata") if isinstance(target.get("metadata"), dict) else {},
        }

    now = datetime.now(UTC).isoformat()
    generation = str(uuid.uuid4())
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        previous_rows = connection.execute(
            """
            SELECT target_type, target_id, active
            FROM source_collection_memberships
            WHERE provider = ? AND collection_key = ?
            """,
            (provider, collection_key),
        ).fetchall()
        previous_active = {
            (str(row["target_type"]), str(row["target_id"]))
            for row in previous_rows
            if row["active"]
        }
        current = set(normalized)
        connection.execute(
            """
            INSERT INTO source_collections (
                provider, collection_key, status, item_count, snapshot_generation,
                metadata_json, last_success_at, updated_at
            )
            VALUES (?, ?, 'ready', ?, ?, ?, ?, ?)
            ON CONFLICT(provider, collection_key) DO UPDATE SET
                status = excluded.status,
                item_count = excluded.item_count,
                snapshot_generation = excluded.snapshot_generation,
                metadata_json = excluded.metadata_json,
                last_success_at = excluded.last_success_at,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                collection_key,
                len(normalized),
                generation,
                json.dumps(metadata or {}, sort_keys=True),
                now,
                now,
            ),
        )
        for target in normalized.values():
            connection.execute(
                """
                INSERT INTO source_collection_memberships (
                    provider, collection_key, target_type, target_id, active,
                    first_seen_at, last_seen_at, removed_at, metadata_json
                )
                VALUES (?, ?, ?, ?, 1, ?, ?, NULL, ?)
                ON CONFLICT(provider, collection_key, target_type, target_id) DO UPDATE SET
                    active = 1,
                    last_seen_at = excluded.last_seen_at,
                    removed_at = NULL,
                    metadata_json = excluded.metadata_json
                """,
                (
                    provider,
                    collection_key,
                    target["target_type"],
                    target["target_id"],
                    now,
                    now,
                    json.dumps(target["metadata"], sort_keys=True),
                ),
            )
        removed = previous_active - current
        for target_type, target_id in removed:
            connection.execute(
                """
                UPDATE source_collection_memberships
                SET active = 0, removed_at = ?, last_seen_at = ?
                WHERE provider = ? AND collection_key = ?
                  AND target_type = ? AND target_id = ?
                """,
                (now, now, provider, collection_key, target_type, target_id),
            )
    return {
        "provider": provider,
        "collection_key": collection_key,
        "generation": generation,
        "total": len(normalized),
        "added": len(current - previous_active),
        "retained": len(current & previous_active),
        "removed": len(removed),
        "updated_at": now,
    }


def list_collection_memberships(
    db_path: Path,
    *,
    provider: str,
    collection_key: str,
    active: bool | None = True,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    where_active = "" if active is None else " AND active = ?"
    params: list[Any] = [provider, collection_key]
    if active is not None:
        params.append(1 if active else 0)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT provider, collection_key, target_type, target_id, active,
                   first_seen_at, last_seen_at, removed_at, metadata_json
            FROM source_collection_memberships
            WHERE provider = ? AND collection_key = ?{where_active}
            ORDER BY target_type, target_id
            """,
            params,
        ).fetchall()
    return [
        {
            **{key: row[key] for key in row.keys() if key != "metadata_json"},
            "active": bool(row["active"]),
            "metadata": json.loads(row["metadata_json"]),
        }
        for row in rows
    ]


def upsert_media_file(
    db_path: Path,
    *,
    platform: str,
    remote_id: str,
    remote_url: str | None,
    local_path: str | None,
    mime_type: str | None,
    size_bytes: int | None,
    checksum: str | None,
    status: str,
    library_relative_path: str | None = None,
    storage_layout: str | None = None,
    file_health: str | None = None,
    source_timestamp: str | None = None,
    verified_at: str | None = None,
    file_key: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    file_key = str(file_key).strip() if file_key is not None else _media_file_key(
        remote_url=remote_url,
        local_path=local_path,
    )
    if not file_key:
        raise ValueError("media file key must not be empty")
    with connect(db_path) as connection:
        media_item = connection.execute(
            "SELECT id FROM media_items WHERE platform = ? AND remote_id = ?",
            (platform, remote_id),
        ).fetchone()
        if not media_item:
            raise ValueError(f"Unknown media item: {platform}/{remote_id}")
        connection.execute(
            """
            INSERT INTO media_files (
                media_item_id, file_key, remote_url, local_path, mime_type, size_bytes,
                checksum, status, library_relative_path, storage_layout, file_health,
                source_timestamp, verified_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_item_id, file_key) DO UPDATE SET
                remote_url = excluded.remote_url,
                local_path = excluded.local_path,
                mime_type = excluded.mime_type,
                size_bytes = excluded.size_bytes,
                checksum = excluded.checksum,
                status = excluded.status,
                library_relative_path = excluded.library_relative_path,
                storage_layout = excluded.storage_layout,
                file_health = excluded.file_health,
                source_timestamp = excluded.source_timestamp,
                verified_at = excluded.verified_at,
                updated_at = excluded.updated_at
            """,
            (
                media_item["id"],
                file_key,
                remote_url,
                local_path,
                mime_type,
                size_bytes,
                checksum,
                status,
                library_relative_path,
                storage_layout,
                file_health or "unknown",
                source_timestamp,
                verified_at,
                now,
                now,
            ),
        )
        row = connection.execute(
            """
            SELECT id, media_item_id, file_key, remote_url, local_path, mime_type,
                   size_bytes, checksum, status, library_relative_path, storage_layout,
                   file_health, source_timestamp, verified_at
            FROM media_files
            WHERE media_item_id = ? AND file_key = ?
            """,
            (media_item["id"], file_key),
        ).fetchone()
    return dict(row)


def list_media_files(
    db_path: Path,
    *,
    platform: str | None = None,
    remote_id: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if platform:
        clauses.append("mi.platform = ?")
        params.append(platform)
    if remote_id:
        clauses.append("mi.remote_id = ?")
        params.append(remote_id)
    if status:
        clauses.append("mf.status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT mf.id, mi.platform, mi.remote_id, mf.file_key, mf.remote_url,
                   mf.local_path, mf.library_relative_path, mf.storage_layout,
                   mf.mime_type, mf.size_bytes, mf.checksum, mf.status,
                   mf.file_health, mf.source_timestamp, mf.verified_at
            FROM media_files mf
            JOIN media_items mi ON mi.id = mf.media_item_id
            {where}
            ORDER BY mf.id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def update_media_file_health(
    db_path: Path,
    *,
    file_id: int,
    file_health: str,
    verified_at: str | None = None,
) -> dict[str, Any]:
    now = verified_at or datetime.now(UTC).isoformat()
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE media_files
            SET file_health = ?, verified_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (file_health, now, now, file_id),
        )
        row = connection.execute(
            """
            SELECT id, file_health, verified_at
            FROM media_files
            WHERE id = ?
            """,
            (file_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"Unknown media file: {file_id}")
    return dict(row)


def upsert_link(db_path: Path, link: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    incoming_provenance = _link_provenance_from_input(link)
    with connect(db_path) as connection:
        existing = connection.execute(
            """
            SELECT id, status, source_provenance_json
            FROM link_queue
            WHERE normalized_url = ?
            """,
            (link["normalized_url"],),
        ).fetchone()
        if not existing:
            connection.execute(
                """
                INSERT INTO link_queue (
                    ingest_platform, original_url, normalized_url, source_chat_id,
                    source_message_id, source_message_date, collector_run_id,
                    status, skip_reason, resolution_json, canonical_url, aliases_json,
                    attempt_count, max_attempts, last_error, last_error_code,
                    last_attempt_at, next_attempt_at, retryable, lease_owner,
                    lease_expires_at, source_provenance_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.get("ingest_platform", "unknown"),
                    link["original_url"],
                    link["normalized_url"],
                    link.get("source_chat_id"),
                    link.get("source_message_id"),
                    link.get("source_message_date"),
                    link.get("collector_run_id"),
                    link.get("status", "queued"),
                    link.get("skip_reason"),
                    json.dumps(link.get("resolution") or {}, sort_keys=True),
                    link.get("canonical_url"),
                    json.dumps(link.get("aliases") or [], sort_keys=True),
                    int(link.get("attempt_count", 0) or 0),
                    int(link.get("max_attempts", 3) or 3),
                    link.get("last_error"),
                    link.get("last_error_code"),
                    link.get("last_attempt_at"),
                    link.get("next_attempt_at"),
                    1 if link.get("retryable", True) else 0,
                    link.get("lease_owner"),
                    link.get("lease_expires_at"),
                    json.dumps(incoming_provenance, sort_keys=True),
                    now,
                    now,
                ),
            )
        else:
            merged_provenance = _merge_link_provenance(
                _json_list(existing["source_provenance_json"]),
                incoming_provenance,
            )
            connection.execute(
                """
                UPDATE link_queue
                SET source_provenance_json = ?,
                    updated_at = ?
                WHERE normalized_url = ?
                """,
                (
                    json.dumps(merged_provenance, sort_keys=True),
                    now,
                    link["normalized_url"],
                ),
            )
        row = connection.execute(
            """
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE normalized_url = ?
            """,
            (link["normalized_url"],),
        ).fetchone()
    result = _link_row_to_dict(row)
    result["is_new"] = existing is None
    result["previous_status"] = existing["status"] if existing else None
    return result


def get_link(db_path: Path, *, link_id: int) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    with connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE id = ?
            """,
            (link_id,),
        ).fetchone()
    return _link_row_to_dict(row) if row else None


def list_links(
    db_path: Path,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [_link_row_to_dict(row) for row in rows]


def list_ready_links(
    db_path: Path,
    *,
    status: str | None = None,
    limit: int | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    now_value = now or datetime.now(UTC).isoformat()
    where, params = _ready_link_where(status=status, now=now_value)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [_link_row_to_dict(row) for row in rows]


def claim_links(
    db_path: Path,
    *,
    status: str | None = None,
    limit: int | None = None,
    lease_owner: str,
    lease_seconds: int = 900,
    now: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    now_dt = _parse_iso_datetime(now) if now else datetime.now(UTC)
    now_value = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    where, params = _ready_link_where(status=status, now=now_value)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            f"""
            SELECT id
            FROM link_queue
            WHERE {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
        link_ids = [int(row["id"]) for row in rows]
        if not link_ids:
            return []
        placeholders = ", ".join("?" for _value in link_ids)
        connection.execute(
            f"""
            UPDATE link_queue
            SET lease_owner = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (lease_owner, lease_expires_at, now_value, *link_ids),
        )
        claimed = connection.execute(
            f"""
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            link_ids,
        ).fetchall()
    return [_link_row_to_dict(row) for row in claimed]


def list_auth_skipped_links(
    db_path: Path,
    *,
    limit: int | None = None,
    now: str | None = None,
    ingest_platform: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    now_value = now or datetime.now(UTC).isoformat()
    where, params = _auth_skipped_link_where(now=now_value, ingest_platform=ingest_platform)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [_link_row_to_dict(row) for row in rows]


def claim_auth_skipped_links(
    db_path: Path,
    *,
    limit: int | None = None,
    lease_owner: str,
    lease_seconds: int = 900,
    now: str | None = None,
    ingest_platform: str | None = None,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    now_dt = _parse_iso_datetime(now) if now else datetime.now(UTC)
    now_value = now_dt.isoformat()
    lease_expires_at = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    where, params = _auth_skipped_link_where(now=now_value, ingest_platform=ingest_platform)
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(0, int(limit)))
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            f"""
            SELECT id
            FROM link_queue
            WHERE {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
        link_ids = [int(row["id"]) for row in rows]
        if not link_ids:
            return []
        placeholders = ", ".join("?" for _value in link_ids)
        connection.execute(
            f"""
            UPDATE link_queue
            SET lease_owner = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            (lease_owner, lease_expires_at, now_value, *link_ids),
        )
        claimed = connection.execute(
            f"""
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE id IN ({placeholders})
            ORDER BY id
            """,
            link_ids,
        ).fetchall()
    return [_link_row_to_dict(row) for row in claimed]


def update_link_resolution(
    db_path: Path,
    *,
    link_id: int,
    status: str,
    resolution: dict[str, Any],
    skip_reason: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    storage_resolution = _sanitize_link_resolution_for_storage(resolution)
    retryable = _link_resolution_retryable(
        status=status,
        resolution=storage_resolution,
        skip_reason=skip_reason,
    )
    last_error = None
    last_error_code = None
    if status in {"skipped", "failed", "deferred"}:
        last_error_code = skip_reason or storage_resolution.get("skip_reason")
        last_error = (storage_resolution.get("details") or {}).get("reason") or last_error_code
    aliases = _link_aliases_from_resolution(storage_resolution)
    with connect(db_path) as connection:
        current = connection.execute(
            "SELECT attempt_count, max_attempts FROM link_queue WHERE id = ?",
            (link_id,),
        ).fetchone()
        if not current:
            raise ValueError(f"Unknown link: {link_id}")
        next_attempt_count = int(current["attempt_count"]) + 1
        max_attempts = int(current["max_attempts"])
        stored_status = status
        next_attempt_at = None
        if status != "resolved" and retryable and next_attempt_count < max_attempts:
            stored_status = "deferred"
            next_attempt_at = _next_link_attempt_at(now, next_attempt_count)
        elif status != "resolved" and retryable and next_attempt_count >= max_attempts:
            stored_status = "failed"
            retryable = False
        connection.execute(
            """
            UPDATE link_queue
            SET status = ?,
                skip_reason = ?,
                resolution_json = ?,
                canonical_url = ?,
                aliases_json = ?,
                attempt_count = attempt_count + 1,
                last_error = ?,
                last_error_code = ?,
                last_attempt_at = ?,
                next_attempt_at = ?,
                retryable = ?,
                lease_owner = NULL,
                lease_expires_at = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (
                stored_status,
                skip_reason,
                json.dumps(storage_resolution, sort_keys=True),
                storage_resolution.get("canonical_url")
                or storage_resolution.get("source_url")
                or storage_resolution.get("normalized_url"),
                json.dumps(aliases, sort_keys=True),
                last_error,
                last_error_code,
                now,
                next_attempt_at,
                1 if retryable else 0,
                now,
                link_id,
            ),
        )
        row = connection.execute(
            """
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, canonical_url, aliases_json,
                   attempt_count, max_attempts, last_error, last_error_code,
                   last_attempt_at, next_attempt_at, retryable, lease_owner,
                   lease_expires_at, source_provenance_json, created_at, updated_at
            FROM link_queue
            WHERE id = ?
            """,
            (link_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"Unknown link: {link_id}")
    return _link_row_to_dict(row)


def _ready_link_where(*, status: str | None, now: str) -> tuple[str, list[Any]]:
    lease_clause = "(lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)"
    if status in (None, "", "queued"):
        return (
            """
            (
                status = 'queued'
                OR (
                    status = 'deferred'
                    AND retryable = 1
                    AND attempt_count < max_attempts
                    AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                )
            )
            AND {lease_clause}
            """.format(lease_clause=lease_clause),
            [now, now],
        )
    if status == "deferred":
        return (
            """
            status = 'deferred'
            AND retryable = 1
            AND attempt_count < max_attempts
            AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            AND {lease_clause}
            """.format(lease_clause=lease_clause),
            [now, now],
        )
    return (f"status = ? AND {lease_clause}", [status, now])


def _auth_skipped_link_where(*, now: str, ingest_platform: str | None) -> tuple[str, list[Any]]:
    where = """
        status IN ('skipped', 'failed', 'deferred')
        AND COALESCE(skip_reason, last_error_code) IN ('requires_auth', 'login_wall')
        AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= ?)
    """
    params: list[Any] = [now]
    if ingest_platform:
        where += " AND ingest_platform = ?"
        params.append(ingest_platform)
    return where, params


def _next_link_attempt_at(now: str, attempt_count: int) -> str:
    now_dt = _parse_iso_datetime(now)
    delay_seconds = min(3600, 60 * (2 ** max(0, attempt_count - 1)))
    return (now_dt + timedelta(seconds=delay_seconds)).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def store_auth_session(
    db_path: Path,
    *,
    provider: str,
    account_id: str | None,
    scopes: list[str],
    expires_at: str | None,
    refresh_available: bool,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    subject = account_id or "unknown"
    with connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO auth_sessions (
                provider, account_id, scopes_json, expires_at,
                refresh_available, status, metadata_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, account_id) DO UPDATE SET
                scopes_json = excluded.scopes_json,
                expires_at = excluded.expires_at,
                refresh_available = excluded.refresh_available,
                status = excluded.status,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                subject,
                json.dumps(scopes, sort_keys=True),
                expires_at,
                1 if refresh_available else 0,
                status,
                json.dumps(metadata or {}, sort_keys=True),
                now,
            ),
        )
    return {
        "provider": provider,
        "account_id": subject,
        "scopes": scopes,
        "expires_at": expires_at,
        "refresh_available": refresh_available,
        "status": status,
        "metadata": metadata or {},
        "updated_at": now,
    }


def _media_file_key(*, remote_url: str | None, local_path: str | None) -> str:
    if remote_url:
        return f"remote:{remote_url}"
    if local_path:
        return f"local:{local_path}"
    raise ValueError("media file requires remote_url or local_path")


def _ensure_media_items_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(media_items)").fetchall()
    }
    if "downloaded_at" not in columns:
        connection.execute("ALTER TABLE media_items ADD COLUMN downloaded_at TEXT")
    if "source_availability" not in columns:
        connection.execute("ALTER TABLE media_items ADD COLUMN source_availability TEXT NOT NULL DEFAULT 'unknown'")


def _ensure_media_files_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(media_files)").fetchall()
    }
    if "file_key" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN file_key TEXT")
    if "library_relative_path" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN library_relative_path TEXT")
    if "storage_layout" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN storage_layout TEXT")
    if "file_health" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN file_health TEXT NOT NULL DEFAULT 'unknown'")
    if "source_timestamp" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN source_timestamp TEXT")
    if "verified_at" not in columns:
        connection.execute("ALTER TABLE media_files ADD COLUMN verified_at TEXT")
    connection.execute(
        """
        UPDATE media_files
        SET file_key = CASE
            WHEN remote_url IS NOT NULL AND remote_url != '' THEN 'remote:' || remote_url
            WHEN local_path IS NOT NULL AND local_path != '' THEN 'local:' || local_path
            ELSE 'legacy:' || id
        END
        WHERE file_key IS NULL OR file_key = ''
        """
    )
    connection.execute(
        """
        DELETE FROM media_files
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM media_files
            GROUP BY media_item_id, file_key
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_media_files_item_file_key
        ON media_files(media_item_id, file_key)
        """
    )


def _ensure_link_queue_schema(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(link_queue)").fetchall()
    }
    migrations = {
        "canonical_url": "ALTER TABLE link_queue ADD COLUMN canonical_url TEXT",
        "aliases_json": "ALTER TABLE link_queue ADD COLUMN aliases_json TEXT NOT NULL DEFAULT '[]'",
        "attempt_count": "ALTER TABLE link_queue ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0",
        "max_attempts": "ALTER TABLE link_queue ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3",
        "last_error": "ALTER TABLE link_queue ADD COLUMN last_error TEXT",
        "last_error_code": "ALTER TABLE link_queue ADD COLUMN last_error_code TEXT",
        "last_attempt_at": "ALTER TABLE link_queue ADD COLUMN last_attempt_at TEXT",
        "next_attempt_at": "ALTER TABLE link_queue ADD COLUMN next_attempt_at TEXT",
        "retryable": "ALTER TABLE link_queue ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1",
        "lease_owner": "ALTER TABLE link_queue ADD COLUMN lease_owner TEXT",
        "lease_expires_at": "ALTER TABLE link_queue ADD COLUMN lease_expires_at TEXT",
        "source_provenance_json": "ALTER TABLE link_queue ADD COLUMN source_provenance_json TEXT NOT NULL DEFAULT '[]'",
    }
    for column, statement in migrations.items():
        if column not in columns:
            connection.execute(statement)
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_link_queue_normalized_url
        ON link_queue(normalized_url)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_queue_status
        ON link_queue(status)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_queue_next_attempt
        ON link_queue(status, retryable, next_attempt_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_link_queue_canonical_url
        ON link_queue(canonical_url)
        """
    )


def _link_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_resolution = result.pop("resolution_json") or "{}"
    try:
        result["resolution"] = json.loads(raw_resolution)
    except json.JSONDecodeError:
        result["resolution"] = {}
    result["aliases"] = _json_list(result.pop("aliases_json", "[]"))
    result["source_provenance"] = _json_list(result.pop("source_provenance_json", "[]"))
    if "retryable" in result:
        result["retryable"] = bool(result["retryable"])
    return result


def _json_list(raw_value: Any) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    try:
        parsed = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _link_provenance_from_input(link: dict[str, Any]) -> list[dict[str, Any]]:
    provenance = {
        "ingest_platform": link.get("ingest_platform", "unknown"),
        "source_chat_id": link.get("source_chat_id"),
        "source_message_id": link.get("source_message_id"),
        "source_message_date": link.get("source_message_date"),
        "collector_run_id": link.get("collector_run_id"),
        "original_url": link.get("original_url"),
    }
    return [{key: value for key, value in provenance.items() if value not in (None, "")}]


def _merge_link_provenance(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *incoming]:
        key = json.dumps(item, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def _link_aliases_from_resolution(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    aliases: list[dict[str, Any]] = []
    for key in ("original_url", "normalized_url", "canonical_url", "source_url", "resolved_media_url"):
        value = resolution.get(key)
        if isinstance(value, str) and value:
            aliases.append({"kind": key, "url": value})
    for alias in resolution.get("aliases") or []:
        if isinstance(alias, dict):
            aliases.append(alias)
        elif isinstance(alias, str):
            aliases.append({"kind": "alias", "url": alias})
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for alias in aliases:
        key = json.dumps(alias, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(alias)
    return unique


def _link_resolution_retryable(
    *,
    status: str,
    resolution: dict[str, Any],
    skip_reason: str | None,
) -> bool:
    if status == "resolved":
        return False
    reason = skip_reason or resolution.get("skip_reason")
    if reason in {
        "unsafe_url",
        "unsupported_domain",
        "unsupported_media_type",
        "unsupported_multi_media",
        "ambiguous",
        "ambiguous_candidates",
        "requires_auth",
        "login_wall",
        "external_source_hidden",
        "javascript_required",
        "blocked",
        "deleted_or_removed",
        "quarantined",
        "too_large",
    }:
        return False
    return status in {"failed", "skipped", "deferred"}


_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-csrf-token",
    "x-xsrf-token",
}
_HEADER_CONTAINER_KEYS = {"headers", "persistable_headers"}
_RUNTIME_DOWNLOAD_KEYS = {"runtime_headers", "download_context"}


def _sanitize_link_resolution_for_storage(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_link_resolution_for_storage(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if lowered in _HEADER_CONTAINER_KEYS and isinstance(item, dict):
            sanitized[key] = {
                header: header_value
                for header, header_value in item.items()
                if header.lower() not in _SENSITIVE_HEADER_NAMES
            }
            continue
        if lowered in _RUNTIME_DOWNLOAD_KEYS:
            continue
        sanitized[key] = _sanitize_link_resolution_for_storage(item)
    return sanitized


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    run_type TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT
);

CREATE TABLE IF NOT EXISTS tool_runs (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT
);

CREATE TABLE IF NOT EXISTS workflow_step_runs (
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    output_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT,
    FOREIGN KEY(workflow_run_id) REFERENCES workflow_runs(id)
);

CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    remote_id TEXT NOT NULL,
    source_url TEXT,
    author_id TEXT,
    author_name TEXT,
    media_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    downloaded_at TEXT,
    source_availability TEXT NOT NULL DEFAULT 'unknown',
    UNIQUE(platform, remote_id)
);

CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_item_id INTEGER NOT NULL,
    file_key TEXT NOT NULL,
    remote_url TEXT,
    local_path TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    checksum TEXT,
    status TEXT NOT NULL DEFAULT 'discovered',
    library_relative_path TEXT,
    storage_layout TEXT,
    file_health TEXT NOT NULL DEFAULT 'unknown',
    source_timestamp TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(media_item_id, file_key),
    FOREIGN KEY(media_item_id) REFERENCES media_items(id)
);

CREATE TABLE IF NOT EXISTS sync_cursors (
    platform TEXT NOT NULL,
    cursor_name TEXT NOT NULL,
    cursor_value TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(platform, cursor_name)
);

CREATE TABLE IF NOT EXISTS source_collections (
    provider TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    item_count INTEGER NOT NULL DEFAULT 0,
    snapshot_generation TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_success_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, collection_key)
);

CREATE TABLE IF NOT EXISTS source_collection_memberships (
    provider TEXT NOT NULL,
    collection_key TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    removed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(provider, collection_key, target_type, target_id),
    FOREIGN KEY(provider, collection_key)
        REFERENCES source_collections(provider, collection_key)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    expires_at TEXT,
    refresh_available INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(provider, account_id)
);

CREATE TABLE IF NOT EXISTS link_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_platform TEXT NOT NULL,
    original_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    source_chat_id TEXT,
    source_message_id TEXT,
    source_message_date TEXT,
    collector_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    skip_reason TEXT,
    resolution_json TEXT NOT NULL DEFAULT '{}',
    canonical_url TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    last_error TEXT,
    last_error_code TEXT,
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    retryable INTEGER NOT NULL DEFAULT 1,
    lease_owner TEXT,
    lease_expires_at TEXT,
    source_provenance_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
