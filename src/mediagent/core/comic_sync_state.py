"""Durable comic manifest/checkpoints, without credentials or transport metadata.

A manifest is an observation, never proof that its chapters were downloaded.
Leases and generations fence stale workers; media tables remain file authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mediagent.core import db

_DONE = ("succeeded", "suppressed")
_STATUSES = (*_DONE, "pending", "running", "failed", "paused")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,200}\Z")
_UNOBSERVED_MANIFEST = "unobserved-manifest"


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS comic_sync_albums (
        provider TEXT NOT NULL, account_key TEXT NOT NULL, album_id TEXT NOT NULL,
        latest_photo_id TEXT, manifest_fingerprint TEXT NOT NULL,
        generation INTEGER NOT NULL CHECK(generation > 0),
        completed_generation INTEGER, revalidation_active INTEGER NOT NULL DEFAULT 0,
        audit_cursor INTEGER NOT NULL DEFAULT 0, last_audit_at TEXT, last_work_at TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(provider,account_key,album_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS comic_sync_chapters (
        provider TEXT NOT NULL, account_key TEXT NOT NULL, album_id TEXT NOT NULL,
        chapter_id TEXT NOT NULL, fingerprint TEXT NOT NULL, ordinal INTEGER NOT NULL,
        generation INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL CHECK(status IN
            ('pending','running','succeeded','suppressed','failed','paused')),
        attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
        lease_owner TEXT, lease_token TEXT, lease_expires_at TEXT,
        last_error_code TEXT, last_verified_at TEXT, last_local_verified_at TEXT,
        adoption_pending INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(provider,account_key,album_id,chapter_id),
        FOREIGN KEY(provider,account_key,album_id)
            REFERENCES comic_sync_albums(provider,account_key,album_id))""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_comic_sync_ready ON
        comic_sync_chapters(provider,account_key,album_id,active,status,next_attempt_at)
    """)


def _utc(now: datetime | str | None) -> datetime:
    value = datetime.fromisoformat(now) if isinstance(now, str) else now
    value = value or datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _identifier(value: Any) -> str:
    if value is None:
        raise ValueError("comic_checkpoint_identifier_invalid")
    value = str(value)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("comic_checkpoint_identifier_invalid")
    return value


def _scope(provider: str, account_key: str, album_id: str) -> tuple[str, str, str]:
    return tuple(_identifier(value) for value in (provider, account_key, album_id))


def _snapshot(connection: sqlite3.Connection, scope: tuple) -> dict | None:
    row = connection.execute(
        """SELECT * FROM comic_sync_albums
        WHERE provider=? AND account_key=? AND album_id=?""",
        scope,
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["chapters"] = [
        dict(item, id=item["chapter_id"])
        for item in connection.execute(
            """SELECT * FROM comic_sync_chapters
            WHERE provider=? AND account_key=? AND album_id=? AND active=1
            ORDER BY ordinal,chapter_id""",
            scope,
        ).fetchall()
    ]
    return result


def read_album(
    db_path: Path, *, provider: str, account_key: str, album_id: str
) -> dict | None:
    """Read an existing checkpoint without creating or migrating a database."""
    scope = _scope(provider, account_key, album_id)
    if not db_path.is_file():
        return None
    with closing(
        sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        if not connection.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='comic_sync_albums'""").fetchone():
            return None
        return _snapshot(connection, scope)


def _complete(connection: sqlite3.Connection, scope: tuple, timestamp: str) -> None:
    if not connection.execute(
        """SELECT 1 FROM comic_sync_chapters
        WHERE provider=? AND account_key=? AND album_id=? AND active=1
        AND status NOT IN ('succeeded','suppressed') LIMIT 1""",
        scope,
    ).fetchone():
        connection.execute(
            """UPDATE comic_sync_albums SET
            completed_generation=generation,revalidation_active=0,updated_at=?
            WHERE provider=? AND account_key=? AND album_id=?
            AND manifest_fingerprint<>?""",
            (timestamp, *scope, _UNOBSERVED_MANIFEST),
        )


def request_revalidation(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_ids: list[str],
    now: datetime | str | None = None,
) -> bool:
    """Start one collection-wide pass, or resume it without reopening work.

    Register every selected album before remote work begins, including those
    not yet observed. A time budget can then stop the caller without losing
    the intent to verify later albums. Existing active passes are never reset.
    """
    scopes = list(
        dict.fromkeys(_scope(provider, account_key, item) for item in album_ids)
    )
    if not scopes:
        return False
    timestamp = _utc(now).isoformat()
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        albums = [
            connection.execute(
                """SELECT generation,revalidation_active FROM comic_sync_albums
                WHERE provider=? AND account_key=? AND album_id=?""",
                scope,
            ).fetchone()
            for scope in scopes
        ]
        if any(album is not None and album["revalidation_active"] for album in albums):
            return False
        for scope, album in zip(scopes, albums):
            if album is None:
                connection.execute(
                    """INSERT INTO comic_sync_albums
                    (provider,account_key,album_id,manifest_fingerprint,generation,
                    revalidation_active,created_at,updated_at)
                    VALUES (?,?,?,?,1,1,?,?)""",
                    (*scope, _UNOBSERVED_MANIFEST, timestamp, timestamp),
                )
                continue
            generation = album["generation"] + 1
            connection.execute(
                """UPDATE comic_sync_albums SET generation=?,completed_generation=NULL,
                revalidation_active=1,updated_at=?
                WHERE provider=? AND account_key=? AND album_id=?""",
                (generation, timestamp, *scope),
            )
            connection.execute(
                """UPDATE comic_sync_chapters SET generation=?,
                status=CASE WHEN status='suppressed' THEN status ELSE 'pending' END,
                lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,adoption_pending=0,updated_at=?
                WHERE provider=? AND account_key=? AND album_id=? AND active=1""",
                (generation, timestamp, *scope),
            )
    return True


def save_manifest(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    latest_photo_id: str | None,
    episodes: list[dict],
    revalidate_all: bool = False,
    now: datetime | str | None = None,
) -> dict:
    """Persist a complete listing before work; resume unfinished full passes.

    Chapter fingerprints must cover only chapter metadata, not album-wide
    counters. Expiring URLs and downloaded page metadata belong elsewhere.
    """
    scope = _scope(provider, account_key, album_id)
    latest = _identifier(latest_photo_id) if latest_photo_id is not None else None
    manifest = [
        (
            _identifier(item.get("id", item.get("chapter_id"))),
            _identifier(
                item.get("fingerprint", item.get("id", item.get("chapter_id")))
            ),
        )
        for item in episodes
    ]
    if len({item[0] for item in manifest}) != len(manifest):
        raise ValueError("comic_checkpoint_duplicate_chapter")
    fingerprint = hashlib.sha256(
        json.dumps(manifest, separators=(",", ":")).encode()
    ).hexdigest()
    timestamp = _utc(now).isoformat()
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        old = _snapshot(connection, scope)
        new_pass = bool(old and revalidate_all and not old["revalidation_active"])
        changed = old is None or old["manifest_fingerprint"] != fingerprint or new_pass
        generation = (old["generation"] + int(changed)) if old else 1
        active_pass = bool(revalidate_all or (old and old["revalidation_active"]))
        connection.execute(
            """INSERT INTO comic_sync_albums
            (provider,account_key,album_id,latest_photo_id,manifest_fingerprint,
            generation,revalidation_active,last_audit_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(provider,account_key,album_id)
            DO UPDATE SET latest_photo_id=excluded.latest_photo_id,
            manifest_fingerprint=excluded.manifest_fingerprint,generation=excluded.generation,
            revalidation_active=excluded.revalidation_active,updated_at=excluded.updated_at""",
            (
                *scope,
                latest,
                fingerprint,
                generation,
                int(active_pass),
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        previous_chapters = {
            row["chapter_id"]: row
            for row in connection.execute(
                "SELECT * FROM comic_sync_chapters WHERE provider=? AND account_key=? AND album_id=?",
                scope,
            ).fetchall()
        }
        if changed:
            connection.execute(
                """UPDATE comic_sync_chapters SET active=0
                WHERE provider=? AND account_key=? AND album_id=?""",
                scope,
            )
        for ordinal, (chapter_id, chapter_fingerprint) in enumerate(manifest):
            row = previous_chapters.get(chapter_id)
            if row is None:
                connection.execute(
                    """INSERT INTO comic_sync_chapters
                    (provider,account_key,album_id,chapter_id,fingerprint,ordinal,generation,
                    status,adoption_pending,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        *scope,
                        chapter_id,
                        chapter_fingerprint,
                        ordinal,
                        generation,
                        int(not active_pass),
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                invalidate = (
                    row["fingerprint"] != chapter_fingerprint or not row["active"]
                )
                reset = row["status"] != "suppressed" and (invalidate or new_pass)
                # A generation change fences even unchanged running work. A
                # stale worker cannot publish into the new manifest snapshot.
                stale_running = changed and row["status"] == "running"
                status = "pending" if reset or stale_running else row["status"]
                connection.execute(
                    """UPDATE comic_sync_chapters SET fingerprint=?,
                    ordinal=?,generation=?,active=1,status=?,
                    lease_owner=CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_token=CASE WHEN ? THEN NULL ELSE lease_token END,
                    lease_expires_at=CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    next_attempt_at=CASE WHEN ? THEN NULL ELSE next_attempt_at END,
                    adoption_pending=CASE WHEN ? THEN 0 ELSE adoption_pending END,
                    updated_at=? WHERE provider=? AND account_key=? AND album_id=? AND chapter_id=?""",
                    (
                        chapter_fingerprint,
                        ordinal,
                        generation,
                        status,
                        int(reset or stale_running),
                        int(reset or stale_running),
                        int(reset or stale_running),
                        int(reset),
                        int(reset or active_pass),
                        timestamp,
                        *scope,
                        chapter_id,
                    ),
                )
        _complete(connection, scope, timestamp)
        return _snapshot(connection, scope)


def adopt_chapters(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    states: dict[str, str],
    generation: int | None = None,
    now: datetime | str | None = None,
) -> dict:
    """Bulk adopt verified legacy media without inventing remote audit evidence."""
    scope = _scope(provider, account_key, album_id)
    timestamp = _utc(now).isoformat()
    if any(
        status not in _STATUSES or status == "running" for status in states.values()
    ):
        raise ValueError("comic_checkpoint_status_invalid")
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        for chapter_id, status in states.items():
            connection.execute(
                """UPDATE comic_sync_chapters SET status=?,adoption_pending=0,
                last_local_verified_at=CASE WHEN ? THEN ? ELSE last_local_verified_at END,
                updated_at=? WHERE provider=? AND account_key=?
                AND album_id=? AND chapter_id=? AND active=1 AND adoption_pending=1
                AND status='pending' AND (? IS NULL OR generation=?)""",
                (
                    status,
                    int(status == "succeeded"),
                    timestamp,
                    timestamp,
                    *scope,
                    _identifier(chapter_id),
                    generation,
                    generation,
                ),
            )
        _complete(connection, scope, timestamp)
        return _snapshot(connection, scope)


def claim_chapter(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    chapter_id: str,
    owner: str,
    lease_seconds: int = 900,
    generation: int | None = None,
    now: datetime | str | None = None,
) -> dict | None:
    scope = _scope(provider, account_key, album_id)
    chapter_id, owner = _identifier(chapter_id), _identifier(owner)
    instant = _utc(now)
    timestamp = instant.isoformat()
    token = uuid.uuid4().hex
    if lease_seconds <= 0:
        raise ValueError("comic_checkpoint_lease_invalid")
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT * FROM comic_sync_chapters
            WHERE provider=? AND account_key=? AND album_id=? AND chapter_id=? AND active=1""",
            (*scope, chapter_id),
        ).fetchone()
        if row is None or (generation is not None and row["generation"] != generation):
            return None
        if row["status"] in _DONE:
            return None
        if row["status"] == "running" and row["lease_expires_at"] > timestamp:
            return None
        if row["next_attempt_at"] and row["next_attempt_at"] > timestamp:
            return None
        connection.execute(
            """UPDATE comic_sync_chapters SET status='running',attempt_count=attempt_count+1,
            adoption_pending=0,lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE provider=? AND account_key=? AND album_id=? AND chapter_id=?""",
            (
                owner,
                token,
                (instant + timedelta(seconds=lease_seconds)).isoformat(),
                timestamp,
                *scope,
                chapter_id,
            ),
        )
        connection.execute(
            """UPDATE comic_sync_albums SET last_work_at=?
            WHERE provider=? AND account_key=? AND album_id=?""",
            (timestamp, *scope),
        )
        result = dict(row)
        result.update(
            status="running",
            token=token,
            lease_token=token,
            attempt_count=row["attempt_count"] + 1,
            lease_expires_at=(instant + timedelta(seconds=lease_seconds)).isoformat(),
        )
        return result


def finish_chapter(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    chapter_id: str,
    token: str,
    generation: int,
    status: str,
    error_code: str | None = None,
    verified_remote: bool = True,
    now: datetime | str | None = None,
) -> bool:
    if status not in _STATUSES or status == "running":
        raise ValueError("comic_checkpoint_status_invalid")
    scope = _scope(provider, account_key, album_id)
    code = _identifier(error_code) if error_code is not None else None
    instant = _utc(now)
    timestamp = instant.isoformat()
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT * FROM comic_sync_chapters WHERE
            provider=? AND account_key=? AND album_id=? AND chapter_id=?
            AND active=1 AND status='running' AND generation=? AND lease_token=?
            AND lease_expires_at>?""",
            (*scope, _identifier(chapter_id), generation, token, timestamp),
        ).fetchone()
        if row is None:
            return False
        retry_at = (
            (
                instant
                + timedelta(
                    seconds=min(3600, 60 * 2 ** min(row["attempt_count"] - 1, 6))
                )
            ).isoformat()
            if status == "failed"
            else None
        )
        connection.execute(
            """UPDATE comic_sync_chapters SET status=?,next_attempt_at=?,
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error_code=?,
            last_verified_at=CASE WHEN ? THEN ? ELSE last_verified_at END,
            last_local_verified_at=CASE WHEN ? THEN ? ELSE last_local_verified_at END,
            updated_at=? WHERE provider=? AND account_key=? AND album_id=? AND chapter_id=?""",
            (
                status,
                retry_at,
                code,
                int(status == "succeeded" and verified_remote),
                timestamp,
                int(status == "succeeded"),
                timestamp,
                timestamp,
                *scope,
                chapter_id,
            ),
        )
        _complete(connection, scope, timestamp)
        return True


def mark_chapter_pending(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    chapter_id: str,
    reason: str = "repair",
    now: datetime | str | None = None,
) -> bool:
    """Explicit health/audit reopening, never override a live lease or suppression."""
    scope = _scope(provider, account_key, album_id)
    timestamp = _utc(now).isoformat()
    reason = _identifier(reason)
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        count = connection.execute(
            """UPDATE comic_sync_chapters SET status='pending',
            next_attempt_at=NULL,last_error_code=?,adoption_pending=0,updated_at=?
            WHERE provider=? AND account_key=? AND album_id=? AND chapter_id=? AND active=1
            AND status NOT IN ('running','suppressed','pending')""",
            (reason, timestamp, *scope, _identifier(chapter_id)),
        ).rowcount
        if count:
            connection.execute(
                """UPDATE comic_sync_albums SET completed_generation=NULL,
                updated_at=? WHERE provider=? AND account_key=? AND album_id=?""",
                (timestamp, *scope),
            )
        return bool(count)


def set_audit_cursor(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    cursor: int,
    now: datetime | str | None = None,
) -> None:
    scope = _scope(provider, account_key, album_id)
    if cursor < 0:
        raise ValueError("comic_checkpoint_cursor_invalid")
    timestamp = _utc(now).isoformat()
    with closing(db.connect(db_path)) as connection, connection:
        connection.execute(
            """UPDATE comic_sync_albums SET audit_cursor=?,last_audit_at=?,
            updated_at=? WHERE provider=? AND account_key=? AND album_id=?""",
            (cursor, timestamp, timestamp, *scope),
        )


def renew_chapter(
    db_path: Path,
    *,
    provider: str,
    account_key: str,
    album_id: str,
    chapter_id: str,
    token: str,
    generation: int,
    lease_seconds: int = 900,
    now: datetime | str | None = None,
) -> bool:
    """Extend only a currently owned, unexpired lease in the current manifest."""
    scope = _scope(provider, account_key, album_id)
    instant = _utc(now)
    if lease_seconds <= 0:
        raise ValueError("comic_checkpoint_lease_invalid")
    with closing(db.connect(db_path)) as connection, connection:
        count = connection.execute(
            """UPDATE comic_sync_chapters SET
            lease_expires_at=?,updated_at=? WHERE provider=? AND account_key=?
            AND album_id=? AND chapter_id=? AND active=1 AND status='running'
            AND generation=? AND lease_token=? AND lease_expires_at>?""",
            (
                (instant + timedelta(seconds=lease_seconds)).isoformat(),
                instant.isoformat(),
                *scope,
                _identifier(chapter_id),
                generation,
                token,
                instant.isoformat(),
            ),
        ).rowcount
        return bool(count)
