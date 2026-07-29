"""SQLite persistence for runs and media state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "6"


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
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
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
    metadata = item.get("metadata", {})

    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT id, status FROM media_items WHERE platform = ? AND remote_id = ?",
            (platform, remote_id),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO media_items (
                platform, remote_id, source_url, author_id, author_name,
                media_type, status, metadata_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    file_key = _media_file_key(remote_url=remote_url, local_path=local_path)
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
    with connect(db_path) as connection:
        existing = connection.execute(
            "SELECT id, status FROM link_queue WHERE normalized_url = ?",
            (link["normalized_url"],),
        ).fetchone()
        if not existing:
            connection.execute(
                """
                INSERT INTO link_queue (
                    ingest_platform, original_url, normalized_url, source_chat_id,
                    source_message_id, source_message_date, collector_run_id,
                    status, skip_reason, resolution_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    now,
                    now,
                ),
            )
        row = connection.execute(
            """
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, created_at, updated_at
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
                   skip_reason, resolution_json, created_at, updated_at
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
                   skip_reason, resolution_json, created_at, updated_at
            FROM link_queue
            {where}
            ORDER BY id
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [_link_row_to_dict(row) for row in rows]


def update_link_resolution(
    db_path: Path,
    *,
    link_id: int,
    status: str,
    resolution: dict[str, Any],
    skip_reason: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    with connect(db_path) as connection:
        connection.execute(
            """
            UPDATE link_queue
            SET status = ?,
                skip_reason = ?,
                resolution_json = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                skip_reason,
                json.dumps(resolution, sort_keys=True),
                now,
                link_id,
            ),
        )
        row = connection.execute(
            """
            SELECT id, ingest_platform, original_url, normalized_url, source_chat_id,
                   source_message_id, source_message_date, collector_run_id, status,
                   skip_reason, resolution_json, created_at, updated_at
            FROM link_queue
            WHERE id = ?
            """,
            (link_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"Unknown link: {link_id}")
    return _link_row_to_dict(row)


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


def _link_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    raw_resolution = result.pop("resolution_json") or "{}"
    try:
        result["resolution"] = json.loads(raw_resolution)
    except json.JSONDecodeError:
        result["resolution"] = {}
    return result


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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
