"""Durable full-delivery jobs for Telegram-collected external links.

Only identifiers, scheduling and stable error codes live here. Resolvers must
reacquire expiring transport context and use the managed download pipeline.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mediagent.core import db

STATUSES = ("pending", "running", "deferred", "blocked", "failed", "succeeded")
_AUTH_CODES = frozenset(
    {
        "requires_auth",
        "login_wall",
        "instagram_session_missing",
        "instagram_session_invalid",
        "instagram_login_required",
        "instagram_checkpoint_required",
        "instagram_two_factor_required",
        "instagram_profile_config_invalid",
    }
)


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Add jobs and reconcile unseeded legacy links without touching media."""
    connection.execute("""CREATE TABLE IF NOT EXISTS link_delivery_jobs (
        id TEXT PRIMARY KEY,
        link_id INTEGER NOT NULL UNIQUE REFERENCES link_queue(id),
        status TEXT NOT NULL CHECK(status IN
            ('pending','running','deferred','blocked','failed','succeeded')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts > 0),
        next_attempt_at TEXT,
        lease_owner TEXT, lease_token TEXT, lease_expires_at TEXT,
        last_error_code TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_link_delivery_ready
        ON link_delivery_jobs(status, next_attempt_at, lease_expires_at)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS link_delivery_media (
        link_id INTEGER NOT NULL REFERENCES link_queue(id),
        media_item_id INTEGER NOT NULL REFERENCES media_items(id),
        PRIMARY KEY(link_id,media_item_id))""")
    connection.execute("""CREATE INDEX IF NOT EXISTS idx_link_delivery_media_item
        ON link_delivery_media(media_item_id,link_id)""")
    # Cursor intake is deliberately independent of delivery. Historical links
    # from another ingest origin may still have Telegram source provenance.
    rows = connection.execute("""SELECT link.* FROM link_queue link
        LEFT JOIN link_delivery_jobs job ON job.link_id = link.id
        WHERE job.id IS NULL""").fetchall()
    timestamp = _utc(None).isoformat()
    for row in rows:
        item = dict(row)
        provenance = _json(item.get("source_provenance_json"), [])
        if item["ingest_platform"] != "telegram" and not any(
            isinstance(source, dict) and source.get("ingest_platform") == "telegram"
            for source in provenance
            if isinstance(provenance, list)
        ):
            continue
        _reconcile_legacy(connection, item, timestamp)
        _enqueue(connection, item, timestamp)


def _reconcile_legacy(
    connection: sqlite3.Connection, item: dict, timestamp: str
) -> None:
    resolution = _json(item.get("resolution_json"), {})
    details = (
        resolution.get("details") if isinstance(resolution.get("details"), dict) else {}
    )
    reason = item.get("skip_reason") or resolution.get("skip_reason")
    try:
        parsed = urlparse(item["normalized_url"])
        host = (parsed.hostname or "").lower()
    except ValueError:
        parsed = urlparse("")
        host = ""
    from mediagent.platforms.pornhub.links import parse_exact_video_link

    # This narrow migration gets a new delivery budget, retaining legacy
    # attempt history. It precedes generic 404 terminal classification.
    misrouted = (
        resolution.get("resolver") == "generic_html_media"
        and reason == "resolver_error"
        and parse_exact_video_link(item["normalized_url"]) is not None
    )
    if misrouted:
        item.update(status="queued", retryable=1, next_attempt_at=None)
        item["max_attempts"] = max(
            int(item["max_attempts"]), int(item["attempt_count"]) + 3
        )
    elif host in {
        "instagram.com",
        "www.instagram.com",
        "m.instagram.com",
    } and parsed.path.startswith("/stories/"):
        item.update(
            status="skipped",
            retryable=0,
            next_attempt_at=None,
            skip_reason="instagram_stories_unsupported",
        )
    elif item["status"] != "resolved" and (reason or details.get("status_code")):
        retryable = db.link_resolution_retryable(
            status=item["status"], resolution=resolution, skip_reason=reason
        )
        item["retryable"] = int(retryable)
        if not retryable:
            item.update(
                status="failed" if item["status"] == "failed" else "skipped",
                next_attempt_at=None,
            )
    else:
        return
    connection.execute(
        """UPDATE link_queue SET status=?, retryable=?,
        next_attempt_at=?, max_attempts=?, skip_reason=?, updated_at=? WHERE id=?""",
        (
            item["status"],
            item["retryable"],
            item["next_attempt_at"],
            item["max_attempts"],
            item.get("skip_reason"),
            timestamp,
            item["id"],
        ),
    )


def enqueue(
    db_path: Path,
    link_id: int,
    *,
    force: bool = False,
    now: datetime | str | None = None,
) -> dict[str, Any]:
    """Persist one link obligation; force explicitly reopens a completed repair.

    Callers must prove local repair is needed and lifecycle permits it before
    forcing success back to pending. Ordinary repeated intake never reopens it.
    """
    timestamp = _utc(now).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        link = connection.execute(
            "SELECT * FROM link_queue WHERE id=?", (link_id,)
        ).fetchone()
        if link is None:
            raise ValueError("Unknown delivery link.")
        _enqueue(connection, dict(link), timestamp)
        if force:
            _requeue(
                connection,
                "link_id=? AND status IN ('failed','deferred','blocked','succeeded')",
                [link_id],
                timestamp,
            )
        row = connection.execute(
            "SELECT * FROM link_delivery_jobs WHERE link_id=?", (link_id,)
        ).fetchone()
    return dict(row)


def _enqueue(connection: sqlite3.Connection, item: dict, timestamp: str) -> None:
    resolution = _json(item.get("resolution_json"), {})
    details = (
        resolution.get("details") if isinstance(resolution.get("details"), dict) else {}
    )
    reason = item.get("skip_reason") or resolution.get("skip_reason")
    code = details.get("error_code") or reason or item.get("last_error_code")
    attempts = int(item.get("attempt_count") or 0)
    maximum = max(1, int(item.get("max_attempts") or 3))
    # A successful resolution has NOT proved that managed media was delivered.
    status, next_at = "pending", None
    # A repaired routing failure retains its historic counter and receives
    # exactly the three additional attempts granted by the migration. Resetting
    # it to zero would turn a large legacy counter into a huge fresh budget.
    job_attempts = attempts if item["status"] == "queued" else 0
    if item["status"] not in {"queued", "resolved"}:
        job_attempts = attempts
        if (
            code in _AUTH_CODES
            or reason in _AUTH_CODES
            or details.get("status_code") in (401, 403)
        ):
            status = "blocked"
        elif not item.get("retryable") or attempts >= maximum:
            status = "failed"
        else:
            status, next_at = "deferred", item.get("next_attempt_at")
    connection.execute(
        """INSERT OR IGNORE INTO link_delivery_jobs
        (id,link_id,status,attempt_count,max_attempts,next_attempt_at,last_error_code,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            "linkjob_" + uuid.uuid4().hex,
            item["id"],
            status,
            job_attempts,
            maximum,
            next_at,
            _safe_code(code),
            timestamp,
            timestamp,
        ),
    )


def claim_ready(
    db_path: Path,
    lease_owner: str,
    *,
    limit: int = 20,
    lease_seconds: int = 900,
    link_ids: list[int] | None = None,
    now: datetime | str | None = None,
) -> list[dict[str, Any]]:
    if not lease_owner or len(lease_owner) > 200:
        raise ValueError("A bounded lease owner is required.")
    if not 1 <= limit <= 100 or not 1 <= lease_seconds <= 86400:
        raise ValueError("Invalid delivery batch size or lease duration.")
    selected_ids: list[int] = []
    selected_sql = ""
    if link_ids is not None:
        if not isinstance(link_ids, list) or len(link_ids) > 1000:
            raise ValueError("Delivery link filter must contain at most 1000 IDs.")
        if not link_ids:
            return []
        selected_ids = list(dict.fromkeys(int(link_id) for link_id in link_ids))
        if any(link_id < 1 for link_id in selected_ids):
            raise ValueError("Delivery link IDs must be positive integers.")
        selected_sql = (
            " AND job.link_id IN (" + ",".join("?" for _ in selected_ids) + ")"
        )
    current = _utc(now)
    timestamp = current.isoformat()
    expires = (current + timedelta(seconds=lease_seconds)).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"""UPDATE link_delivery_jobs SET status='failed',
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
            last_error_code='lease_expired',updated_at=?
            WHERE status='running' AND lease_expires_at<=? AND attempt_count>=max_attempts
            {selected_sql.replace("job.", "")}""",
            (timestamp, timestamp, *selected_ids),
        )
        rows = connection.execute(
            f"""SELECT job.id FROM link_delivery_jobs job
            JOIN link_queue link ON link.id=job.link_id
            WHERE (link.lease_owner IS NULL OR link.lease_expires_at IS NULL OR link.lease_expires_at<=?)
            AND job.attempt_count<job.max_attempts AND (
                (job.status IN ('pending','deferred') AND (job.next_attempt_at IS NULL OR job.next_attempt_at<=?))
                OR (job.status='running' AND job.lease_expires_at<=?))
            {selected_sql}
            ORDER BY COALESCE(job.next_attempt_at,job.created_at),job.id LIMIT ?""",
            (timestamp, timestamp, timestamp, *selected_ids, limit),
        ).fetchall()
        claimed = []
        for row in rows:
            token = uuid.uuid4().hex
            connection.execute(
                """UPDATE link_delivery_jobs SET status='running',
                attempt_count=attempt_count+1,lease_owner=?,lease_token=?,lease_expires_at=?,
                next_attempt_at=NULL,updated_at=? WHERE id=?""",
                (lease_owner, token, expires, timestamp, row["id"]),
            )
            claimed.append(
                dict(
                    connection.execute(
                        "SELECT * FROM link_delivery_jobs WHERE id=?", (row["id"],)
                    ).fetchone()
                )
            )
    return claimed


def renew(
    db_path: Path,
    job_id: str,
    lease_owner: str,
    lease_token: str,
    *,
    lease_seconds: int = 900,
    now: datetime | str | None = None,
) -> bool:
    """Extend a live lease only; an expired or replaced owner cannot revive it."""
    if not 1 <= lease_seconds <= 86400:
        raise ValueError("Invalid delivery lease duration.")
    current = _utc(now)
    with db.connect(db_path) as connection:
        return (
            connection.execute(
                """UPDATE link_delivery_jobs SET lease_expires_at=?,updated_at=?
            WHERE id=? AND status='running' AND lease_owner=? AND lease_token=?
            AND lease_expires_at>?""",
                (
                    (current + timedelta(seconds=lease_seconds)).isoformat(),
                    current.isoformat(),
                    job_id,
                    lease_owner,
                    lease_token,
                    current.isoformat(),
                ),
            ).rowcount
            == 1
        )


def finish(
    db_path: Path,
    job_id: str,
    lease_owner: str,
    lease_token: str,
    *,
    outcome: str = "succeeded",
    error_code: str | None = None,
    retry_after_seconds: int | None = None,
    now: datetime | str | None = None,
) -> dict[str, Any] | None:
    """Fence completion by owner, token AND expiry; stale workers cannot finish."""
    if outcome not in {"succeeded", "retry", "blocked", "failed"}:
        raise ValueError("Invalid delivery outcome.")
    current = _utc(now)
    timestamp = current.isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT * FROM link_delivery_jobs WHERE id=?
            AND status='running' AND lease_owner=? AND lease_token=? AND lease_expires_at>?""",
            (job_id, lease_owner, lease_token, timestamp),
        ).fetchone()
        if row is None:
            return None
        status = outcome
        next_at = None
        if outcome == "retry":
            status = (
                "deferred" if row["attempt_count"] < row["max_attempts"] else "failed"
            )
            if status == "deferred":
                delay = min(3600, 60 * 2 ** min(6, row["attempt_count"] - 1))
                if retry_after_seconds is not None:
                    delay = max(delay, min(86400, max(0, int(retry_after_seconds))))
                next_at = (current + timedelta(seconds=delay)).isoformat()
        connection.execute(
            """UPDATE link_delivery_jobs SET status=?,next_attempt_at=?,
            lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,last_error_code=?,
            completed_at=?,updated_at=? WHERE id=?""",
            (
                status,
                next_at,
                _safe_code(error_code),
                timestamp if status == "succeeded" else None,
                timestamp,
                job_id,
            ),
        )
        result = connection.execute(
            "SELECT * FROM link_delivery_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return dict(result)


def summary(db_path: Path) -> dict[str, int]:
    counts = dict.fromkeys(STATUSES, 0)
    if not db_path.exists():
        return counts
    with db.connect(db_path) as connection:
        if not _has_jobs(connection):
            return counts
        for row in connection.execute(
            "SELECT status,COUNT(*) AS count FROM link_delivery_jobs GROUP BY status"
        ):
            counts[row["status"]] = int(row["count"])
    return counts


def list_jobs(db_path: Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return safe operator diagnostics without URLs or source message data."""
    if not 1 <= limit <= 1000:
        raise ValueError("Delivery listing limit must be between 1 and 1000.")
    if not db_path.exists():
        return []
    with db.connect(db_path) as connection:
        if not _has_jobs(connection):
            return []
        rows = connection.execute(
            """SELECT link_id,status,attempt_count,max_attempts,next_attempt_at,
                last_error_code FROM link_delivery_jobs
                WHERE status!='succeeded' ORDER BY created_at,id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _has_jobs(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='link_delivery_jobs'"
        ).fetchone()
        is not None
    )


def is_leased(
    db_path: Path, link_id: int, *, now: datetime | str | None = None
) -> bool:
    """Report an unexpired delivery claim without creating or migrating a DB."""
    if not db_path.exists():
        return False
    timestamp = _utc(now).isoformat()
    with db.connect(db_path) as connection:
        if not _has_jobs(connection):
            return False
        return (
            connection.execute(
                """SELECT 1 FROM link_delivery_jobs WHERE link_id=? AND status='running'
                AND lease_expires_at>?""",
                (link_id, timestamp),
            ).fetchone()
            is not None
        )


def owns(
    db_path: Path,
    job_id: str,
    owner: str,
    token: str,
    *,
    now: datetime | str | None = None,
) -> bool:
    """Verify the exact live delivery lease before entering its materializer."""
    if not db_path.exists():
        return False
    with db.connect(db_path) as connection:
        if not _has_jobs(connection):
            return False
        return (
            connection.execute(
                """SELECT 1 FROM link_delivery_jobs WHERE id=? AND status='running'
                AND lease_owner=? AND lease_token=? AND lease_expires_at>?""",
                (job_id, owner, token, _utc(now).isoformat()),
            ).fetchone()
            is not None
        )


def requeue(
    db_path: Path,
    link_ids: list[int] | None = None,
    *,
    blocked_auth: bool = False,
    now: datetime | str | None = None,
) -> int:
    """Explicitly grant another bounded budget without erasing attempt history.

    By default retry failed/deferred only; blocked_auth selects ONLY auth blocks.
    Running and succeeded jobs are never implicitly reset.
    """
    statuses = "('blocked')" if blocked_auth else "('failed','deferred')"
    where, params = f"status IN {statuses}", []
    if link_ids is not None:
        if not link_ids:
            return 0
        where += " AND link_id IN (" + ",".join("?" for _ in link_ids) + ")"
        params.extend(int(value) for value in link_ids)
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _requeue(connection, where, params, _utc(now).isoformat())


def _requeue(
    connection: sqlite3.Connection, where: str, params: list, timestamp: str
) -> int:
    # Keep legacy counters intact while allowing old resolver APIs a fresh budget.
    connection.execute(
        f"""UPDATE link_queue SET status='queued',retryable=1,
        next_attempt_at=NULL,max_attempts=MAX(max_attempts,attempt_count+3)
        WHERE id IN (SELECT link_id FROM link_delivery_jobs WHERE {where})""",
        params,
    )
    return connection.execute(
        f"""UPDATE link_delivery_jobs SET status='pending',
        max_attempts=MAX(max_attempts,attempt_count+3),next_attempt_at=NULL,
        lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,completed_at=NULL,updated_at=?
        WHERE {where}""",
        [timestamp, *params],
    ).rowcount


def _json(value: str | None, default: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (ValueError, TypeError):
        return default
    return result if isinstance(result, type(default)) else default


def _safe_code(value: Any) -> str | None:
    if value is None:
        return None
    return (
        str(value)
        if re.fullmatch(r"[a-zA-Z0-9_]{1,100}", str(value))
        else "delivery_failed"
    )


def _utc(value: datetime | str | None) -> datetime:
    current = (
        datetime.fromisoformat(value)
        if isinstance(value, str)
        else value or datetime.now(UTC)
    )
    return (
        current.replace(tzinfo=UTC)
        if current.tzinfo is None
        else current.astimezone(UTC)
    )


def bind_media(db_path: Path, link_id: int, identities: list[tuple[str, str]]) -> int:
    """Bind persisted media identities to a delivery for network-free replay."""
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        count = 0
        for platform, remote_id in identities:
            row = connection.execute(
                "SELECT id FROM media_items WHERE platform=? AND remote_id=?",
                (platform, remote_id),
            ).fetchone()
            if row is None:
                raise ValueError("Cannot bind delivery to unpersisted media.")
            count += connection.execute(
                "INSERT OR IGNORE INTO link_delivery_media VALUES (?,?)",
                (link_id, row["id"]),
            ).rowcount
        _refresh_provenance(connection, link_id)
    return count


def refresh_provenance(db_path: Path, link_id: int) -> int:
    """Merge new Telegram occurrences into already delivered media metadata."""
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _refresh_provenance(connection, link_id)


def _refresh_provenance(connection: sqlite3.Connection, link_id: int) -> int:
    link = connection.execute(
        "SELECT * FROM link_queue WHERE id=?", (link_id,)
    ).fetchone()
    if link is None:
        raise ValueError("Unknown delivery link.")
    rows = connection.execute(
        """SELECT media.id,media.metadata_json FROM media_items media
        JOIN link_delivery_media binding ON binding.media_item_id=media.id
        WHERE binding.link_id=?""",
        (link_id,),
    ).fetchall()
    changed = 0
    for row in rows:
        metadata = _json(row["metadata_json"], {})
        existing = metadata.get("source_provenance")
        merged = list(existing) if isinstance(existing, list) else []
        # A provider refresh may replace media metadata before another alias
        # binds the same canonical item. Reconstruct every bound link's durable
        # provenance rather than trusting the previous metadata snapshot.
        bound_links = connection.execute(
            """SELECT link.id,link.source_provenance_json FROM link_queue link
                JOIN link_delivery_media binding ON binding.link_id=link.id
                WHERE binding.media_item_id=? ORDER BY link.id""",
            (row["id"],),
        ).fetchall()
        for bound_link in bound_links:
            for source in _json(bound_link["source_provenance_json"], []):
                if not isinstance(source, dict):
                    continue
                occurrence = {
                    key: value
                    for key, value in {
                        "platform": source.get("ingest_platform"),
                        "chat_id": source.get("source_chat_id"),
                        "message_id": source.get("source_message_id"),
                        "message_date": source.get("source_message_date"),
                        "collector_run_id": source.get("collector_run_id"),
                        "link_id": bound_link["id"],
                    }.items()
                    if value is not None
                }
                if occurrence not in merged:
                    merged.append(occurrence)
        if merged != existing:
            metadata["source_provenance"] = merged
            connection.execute(
                "UPDATE media_items SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, sort_keys=True), row["id"]),
            )
            changed += 1
    return changed
