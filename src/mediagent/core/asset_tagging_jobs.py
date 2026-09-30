"""Durable, leased work queue for automated Asset metadata tagging."""

from __future__ import annotations

import sqlite3
import unicodedata
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from mediagent.core import asset_tags, assets, db
from mediagent.core.tag_values import is_reserved_tag, normalize_tags


DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_SECONDS = 300
MAX_BATCH_SIZE = 100


class TaggingLeaseLostError(ValueError):
    """Raised when a worker no longer owns the requested tagging job."""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the automated tagging queue without scanning Asset content."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS asset_tagging_jobs (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
            input_fingerprint TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 5 CHECK(max_attempts > 0),
            retryable INTEGER NOT NULL DEFAULT 1 CHECK(retryable IN (0, 1)),
            next_attempt_at TEXT,
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            last_error_code TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        );

        CREATE INDEX IF NOT EXISTS idx_asset_tagging_jobs_ready
        ON asset_tagging_jobs(status, retryable, next_attempt_at, lease_expires_at);

        CREATE INDEX IF NOT EXISTS idx_asset_tagging_jobs_asset
        ON asset_tagging_jobs(asset_id);
        """
    )


def enqueue(
    db_path: Path,
    *,
    asset_id: str,
    input_fingerprint: str,
    provider: str,
    model: str,
    prompt_version: str,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create or refresh one canonical Asset's tagging job.

    Re-enqueueing unchanged work is idempotent. A changed input fingerprint or
    model contract invalidates any old lease and makes the work pending again.
    """

    fingerprint = _required_text(input_fingerprint, name="input fingerprint", limit=512)
    provider_value = _required_text(provider, name="provider", limit=200)
    model_value = _required_text(model, name="model", limit=300)
    prompt_value = _required_text(prompt_version, name="prompt version", limit=100)
    attempt_limit = int(max_attempts)
    if attempt_limit < 1 or attempt_limit > 100:
        raise ValueError("Tagging max attempts must be between 1 and 100.")
    timestamp = _timestamp(now)

    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical_id = assets.resolve_asset_id(connection, asset_id)
        if canonical_id is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        asset = connection.execute(
            "SELECT state FROM assets WHERE id = ?", (canonical_id,)
        ).fetchone()
        if asset is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        existing = connection.execute(
            "SELECT * FROM asset_tagging_jobs WHERE asset_id = ?", (canonical_id,)
        ).fetchone()
        contract_changed = existing is not None and any(
            str(existing[field]) != expected
            for field, expected in (
                ("input_fingerprint", fingerprint),
                ("provider", provider_value),
                ("model", model_value),
                ("prompt_version", prompt_value),
            )
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO asset_tagging_jobs (
                    id, asset_id, status, input_fingerprint, provider, model,
                    prompt_version, attempt_count, max_attempts, retryable,
                    created_at, updated_at
                ) VALUES (?, ?, 'pending', ?, ?, ?, ?, 0, ?, 1, ?, ?)
                """,
                (
                    f"tagjob_{uuid.uuid4().hex}",
                    canonical_id,
                    fingerprint,
                    provider_value,
                    model_value,
                    prompt_value,
                    attempt_limit,
                    timestamp,
                    timestamp,
                ),
            )
        elif force or contract_changed:
            connection.execute(
                """
                UPDATE asset_tagging_jobs
                SET status = 'pending', input_fingerprint = ?, provider = ?,
                    model = ?, prompt_version = ?, attempt_count = 0,
                    max_attempts = ?, retryable = 1, next_attempt_at = NULL,
                    lease_owner = NULL, lease_token = NULL,
                    lease_expires_at = NULL, last_error_code = NULL,
                    last_error = NULL, completed_at = NULL, updated_at = ?
                WHERE asset_id = ?
                """,
                (
                    fingerprint,
                    provider_value,
                    model_value,
                    prompt_value,
                    attempt_limit,
                    timestamp,
                    canonical_id,
                ),
            )
        row = connection.execute(
            "SELECT * FROM asset_tagging_jobs WHERE asset_id = ?", (canonical_id,)
        ).fetchone()
    if row is None:  # pragma: no cover - guarded by the transaction above
        raise RuntimeError("Tagging job enqueue did not persist a row.")
    return _job(row)


def claim(
    db_path: Path,
    *,
    owner: str,
    limit: int = 1,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    asset_ids: Iterable[str] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Atomically lease ready work for active Assets only."""

    owner_value = _required_text(owner, name="lease owner", limit=200)
    batch_size = int(limit)
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"Tagging claim limit must be between 1 and {MAX_BATCH_SIZE}.")
    lease_duration = int(lease_seconds)
    if lease_duration < 1 or lease_duration > 86_400:
        raise ValueError("Tagging lease must be between 1 and 86400 seconds.")
    current = _utc(now)
    timestamp = current.isoformat()
    lease_expires_at = (current + timedelta(seconds=lease_duration)).isoformat()
    requested_ids = _asset_id_filter(asset_ids)

    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical_ids: list[str] | None = None
        if requested_ids is not None:
            canonical_ids = []
            seen: set[str] = set()
            for asset_id in requested_ids:
                canonical_id = assets.resolve_asset_id(connection, asset_id)
                if canonical_id is None:
                    raise ValueError(f"Unknown Asset: {asset_id}")
                if canonical_id not in seen:
                    canonical_ids.append(canonical_id)
                    seen.add(canonical_id)
            if not canonical_ids:
                return []
        _expire_exhausted_leases(connection, timestamp=timestamp)
        asset_filter = ""
        filter_parameters: list[Any] = []
        if canonical_ids is not None:
            placeholders = ",".join("?" for _ in canonical_ids)
            asset_filter = f"AND job.asset_id IN ({placeholders})"
            filter_parameters.extend(canonical_ids)
        rows = connection.execute(
            f"""
            SELECT job.id
            FROM asset_tagging_jobs job
            JOIN assets asset ON asset.id = job.asset_id
            WHERE asset.state = 'active'
              AND job.attempt_count < job.max_attempts
              AND (
                    (job.status = 'pending'
                     AND (job.next_attempt_at IS NULL OR job.next_attempt_at <= ?))
                 OR (job.status = 'failed' AND job.retryable = 1
                     AND (job.next_attempt_at IS NULL OR job.next_attempt_at <= ?))
                 OR (job.status = 'running' AND job.lease_expires_at IS NOT NULL
                     AND job.lease_expires_at <= ?)
              )
              {asset_filter}
            ORDER BY COALESCE(job.next_attempt_at, job.created_at), job.created_at, job.id
            LIMIT ?
            """,
            (timestamp, timestamp, timestamp, *filter_parameters, batch_size),
        ).fetchall()
        claimed_ids: list[str] = []
        for row in rows:
            job_id = str(row["id"])
            lease_token = f"lease_{uuid.uuid4().hex}"
            connection.execute(
                """
                UPDATE asset_tagging_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                    next_attempt_at = NULL, last_error_code = NULL,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (owner_value, lease_token, lease_expires_at, timestamp, job_id),
            )
            claimed_ids.append(job_id)
        if not claimed_ids:
            return []
        placeholders = ",".join("?" for _ in claimed_ids)
        claimed = connection.execute(
            f"SELECT * FROM asset_tagging_jobs WHERE id IN ({placeholders}) ORDER BY created_at, id",
            tuple(claimed_ids),
        ).fetchall()
    return [_job(row) for row in claimed]


def has_ready(db_path: Path, *, now: datetime | None = None) -> bool:
    """Recover exhausted leases and report whether work is claimable."""

    timestamp = _timestamp(now)
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _expire_exhausted_leases(connection, timestamp=timestamp)
        row = connection.execute(
            """
            SELECT 1
            FROM asset_tagging_jobs job
            JOIN assets asset ON asset.id = job.asset_id
            WHERE asset.state = 'active'
              AND job.attempt_count < job.max_attempts
              AND (
                    (job.status = 'pending'
                     AND (job.next_attempt_at IS NULL OR job.next_attempt_at <= ?))
                 OR (job.status = 'failed' AND job.retryable = 1
                     AND (job.next_attempt_at IS NULL OR job.next_attempt_at <= ?))
                 OR (job.status = 'running' AND job.lease_expires_at IS NOT NULL
                     AND job.lease_expires_at <= ?)
              )
            LIMIT 1
            """,
            (timestamp, timestamp, timestamp),
        ).fetchone()
    return row is not None


def _expire_exhausted_leases(
    connection: sqlite3.Connection,
    *,
    timestamp: str,
) -> None:
    """Finalize crashed last attempts so they cannot remain running forever."""

    connection.execute(
        """
        UPDATE asset_tagging_jobs
        SET status = 'failed', retryable = 0, next_attempt_at = NULL,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_error_code = COALESCE(last_error_code, 'lease_expired'),
            last_error = COALESCE(last_error, 'Tagging lease expired.'),
            updated_at = ?
        WHERE status = 'running'
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= ?
          AND attempt_count >= max_attempts
        """,
        (timestamp, timestamp),
    )


def complete(
    db_path: Path,
    *,
    job_id: str,
    lease_token: str,
    tags: Iterable[str],
    provider: str | None = None,
    model: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Merge tags and complete a leased job in one transaction."""

    timestamp = _timestamp(now)
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return complete_in_connection(
            connection,
            job_id=job_id,
            lease_token=lease_token,
            tags=tags,
            provider=provider,
            model=model,
            now=timestamp,
        )


def complete_in_connection(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    tags: Iterable[str],
    provider: str | None = None,
    model: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    """Complete one leased job inside the caller's write transaction."""

    normalized_tags = normalize_tags(tags)
    if any(is_reserved_tag(tag) for tag in normalized_tags):
        raise ValueError("source: and type: tags are maintained automatically.")
    token = _required_text(lease_token, name="lease token", limit=200)
    provider_value = _optional_text(provider, name="provider", limit=200)
    model_value = _optional_text(model, name="model", limit=300)
    timestamp = now or datetime.now(UTC).isoformat()
    row = _leased_job(connection, job_id=job_id, lease_token=token)
    asset = connection.execute(
        "SELECT state FROM assets WHERE id = ?", (row["asset_id"],)
    ).fetchone()
    if asset is None or str(asset["state"]) != "active":
        raise ValueError("Only an active Asset can complete automated tagging.")
    if normalized_tags:
        asset_result = asset_tags.update_tags_in_connection(
            connection,
            asset_id=str(row["asset_id"]),
            add=normalized_tags,
            now=timestamp,
        )
    else:
        asset_result = assets.get_asset(connection, str(row["asset_id"]))
        asset_result["tags_added"] = []
        asset_result["tags_removed"] = []
    updated = connection.execute(
        """
        UPDATE asset_tagging_jobs
        SET status = 'succeeded', retryable = 0, next_attempt_at = NULL,
            lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
            last_error_code = NULL, last_error = NULL,
            provider = COALESCE(?, provider), model = COALESCE(?, model),
            completed_at = ?, updated_at = ?
        WHERE id = ? AND status = 'running' AND lease_token = ?
        """,
        (provider_value, model_value, timestamp, timestamp, job_id, token),
    )
    if updated.rowcount != 1:
        raise ValueError("Tagging job lease is no longer valid.")
    completed = connection.execute(
        "SELECT * FROM asset_tagging_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if completed is None:  # pragma: no cover - guarded by _leased_job
        raise RuntimeError("Completed tagging job disappeared.")
    return {"job": _job(completed), "asset": asset_result}


def requeue_stale_in_connection(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
    input_fingerprint: str,
    provider: str,
    model: str,
    prompt_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replace leased work whose metadata changed, without consuming retries."""

    token = _required_text(lease_token, name="lease token", limit=200)
    fingerprint = _required_text(input_fingerprint, name="input fingerprint", limit=512)
    provider_value = _required_text(provider, name="provider", limit=200)
    model_value = _required_text(model, name="model", limit=300)
    prompt_value = _required_text(prompt_version, name="prompt version", limit=100)
    current = _utc(now)
    timestamp = current.isoformat()
    next_attempt_at = (current + timedelta(seconds=60)).isoformat()
    _leased_job(connection, job_id=job_id, lease_token=token)
    updated = connection.execute(
        """
        UPDATE asset_tagging_jobs
        SET status = 'pending', input_fingerprint = ?, provider = ?, model = ?,
            prompt_version = ?, attempt_count = 0, retryable = 1,
            next_attempt_at = ?, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error_code = NULL, last_error = NULL,
            completed_at = NULL, updated_at = ?
        WHERE id = ? AND status = 'running' AND lease_token = ?
        """,
        (
            fingerprint,
            provider_value,
            model_value,
            prompt_value,
            next_attempt_at,
            timestamp,
            job_id,
            token,
        ),
    )
    if updated.rowcount != 1:
        raise ValueError("Tagging job lease is no longer valid.")
    row = connection.execute(
        "SELECT * FROM asset_tagging_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:  # pragma: no cover - guarded above
        raise RuntimeError("Requeued tagging job disappeared.")
    return _job(row)


def fail(
    db_path: Path,
    *,
    job_id: str,
    lease_token: str,
    error_code: str,
    error: str,
    retryable: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Release a leased job as failed, with bounded exponential backoff."""

    token = _required_text(lease_token, name="lease token", limit=200)
    code = _error_code(error_code)
    # Never persist exception text from an HTTP/LLM client. It may contain a
    # credential-bearing URL, local path, header, or response body. The stable
    # code supplies a fixed human-readable diagnostic instead.
    del error
    message = _error_message(code)
    current = _utc(now)
    timestamp = current.isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _leased_job(connection, job_id=job_id, lease_token=token)
        can_retry = bool(retryable) and int(row["attempt_count"]) < int(row["max_attempts"])
        next_attempt_at = (
            current + timedelta(seconds=_backoff_seconds(int(row["attempt_count"])))
        ).isoformat() if can_retry else None
        updated = connection.execute(
            """
            UPDATE asset_tagging_jobs
            SET status = 'failed', retryable = ?, next_attempt_at = ?,
                lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
                last_error_code = ?, last_error = ?, completed_at = NULL,
                updated_at = ?
            WHERE id = ? AND status = 'running' AND lease_token = ?
            """,
            (1 if can_retry else 0, next_attempt_at, code, message, timestamp, job_id, token),
        )
        if updated.rowcount != 1:
            raise ValueError("Tagging job lease is no longer valid.")
        failed = connection.execute(
            "SELECT * FROM asset_tagging_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if failed is None:  # pragma: no cover - guarded by _leased_job
        raise RuntimeError("Failed tagging job disappeared.")
    return _job(failed)


def retry(
    db_path: Path,
    *,
    asset_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Explicitly reset one failed or completed Asset job for another run."""

    timestamp = _timestamp(now)
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical_id = assets.resolve_asset_id(connection, asset_id)
        if canonical_id is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        row = connection.execute(
            "SELECT id FROM asset_tagging_jobs WHERE asset_id = ?", (canonical_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Asset has no tagging job: {asset_id}")
        connection.execute(
            """
            UPDATE asset_tagging_jobs
            SET status = 'pending', attempt_count = 0, retryable = 1,
                next_attempt_at = NULL, lease_owner = NULL, lease_token = NULL,
                lease_expires_at = NULL, last_error_code = NULL,
                last_error = NULL, completed_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, row["id"]),
        )
        retried = connection.execute(
            "SELECT * FROM asset_tagging_jobs WHERE id = ?", (row["id"],)
        ).fetchone()
    if retried is None:  # pragma: no cover - guarded above
        raise RuntimeError("Retried tagging job disappeared.")
    return _job(retried)


def reconcile_asset_merge(
    connection: sqlite3.Connection,
    *,
    canonical_id: str,
    merged_id: str,
    now: str,
) -> None:
    """Collapse merged Asset jobs onto one pending canonical job.

    A merge changes the metadata input, so even a previously successful job is
    made pending. Any in-flight lease is invalidated; stale workers cannot
    complete it because completion requires the exact current lease token.
    """

    if not _table_exists(connection, "asset_tagging_jobs"):
        return
    canonical = connection.execute(
        "SELECT * FROM asset_tagging_jobs WHERE asset_id = ?", (canonical_id,)
    ).fetchone()
    merged = connection.execute(
        "SELECT * FROM asset_tagging_jobs WHERE asset_id = ?", (merged_id,)
    ).fetchone()
    if canonical is None and merged is None:
        return
    if canonical is None:
        connection.execute(
            """
            UPDATE asset_tagging_jobs
            SET asset_id = ?, status = 'pending', attempt_count = 0,
                retryable = 1, next_attempt_at = NULL, lease_owner = NULL,
                lease_token = NULL, lease_expires_at = NULL,
                last_error_code = NULL, last_error = NULL,
                completed_at = NULL, updated_at = ?
            WHERE asset_id = ?
            """,
            (canonical_id, now, merged_id),
        )
        return
    if merged is not None:
        connection.execute("DELETE FROM asset_tagging_jobs WHERE id = ?", (merged["id"],))
    connection.execute(
        """
        UPDATE asset_tagging_jobs
        SET status = 'pending', attempt_count = 0, retryable = 1,
            next_attempt_at = NULL, lease_owner = NULL, lease_token = NULL,
            lease_expires_at = NULL, last_error_code = NULL, last_error = NULL,
            completed_at = NULL, updated_at = ?
        WHERE id = ?
        """,
        (now, canonical["id"]),
    )


def _leased_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    lease_token: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM asset_tagging_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise TaggingLeaseLostError(f"Unknown tagging job: {job_id}")
    if str(row["status"]) != "running" or str(row["lease_token"] or "") != lease_token:
        raise TaggingLeaseLostError("Tagging job lease is no longer valid.")
    return row


def _job(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    result["attempt_count"] = int(result["attempt_count"])
    result["max_attempts"] = int(result["max_attempts"])
    result["retryable"] = bool(result["retryable"])
    return result


def _required_text(value: Any, *, name: str, limit: int) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    if not text:
        raise ValueError(f"Tagging {name} is required.")
    if len(text) > limit:
        raise ValueError(f"Tagging {name} must not exceed {limit} characters.")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ValueError(f"Tagging {name} must not contain control characters.")
    return text


def _optional_text(value: Any, *, name: str, limit: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, name=name, limit=limit)


def _asset_id_filter(values: Iterable[str] | None) -> list[str] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ValueError("Tagging Asset filter must be a list of Asset IDs.")
    return [_required_text(value, name="Asset ID", limit=200) for value in values]


def _error_code(value: Any) -> str:
    code = unicodedata.normalize("NFC", str(value or "")).strip().casefold()
    if not code or len(code) > 80 or any(
        not (character.isascii() and (character.isalnum() or character == "_"))
        for character in code
    ):
        raise ValueError("Tagging error code must use only ASCII letters, digits, and underscores.")
    return code


def _error_message(code: str) -> str:
    return {
        "llm_timeout": "The language model request timed out.",
        "llm_api_error": "The language model request failed.",
        "invalid_response": "The language model returned an invalid tagging response.",
        "stale_input": "Asset metadata changed before tagging completed.",
    }.get(code, "Automated tagging failed.")


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _timestamp(value: datetime | None) -> str:
    return _utc(value).isoformat()


def _backoff_seconds(attempt_count: int) -> int:
    return min(3_600, 60 * (2 ** max(0, attempt_count - 1)))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None
