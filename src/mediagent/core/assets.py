"""Stable user-facing Asset identity over source and library records."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core.tag_values import canonicalize_tags, is_reserved_tag, tag_key


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the Asset identity tables without changing existing content rows."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            media_type TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active'
                CHECK(state IN ('active', 'removed', 'purged', 'merged')),
            metadata_json TEXT NOT NULL DEFAULT '{"tags": []}',
            primary_library_entry_id TEXT,
            merged_into_asset_id TEXT,
            removed_at TEXT,
            purged_at TEXT,
            purge_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(primary_library_entry_id) REFERENCES library_entries(id),
            FOREIGN KEY(merged_into_asset_id) REFERENCES assets(id)
        );

        CREATE TABLE IF NOT EXISTS asset_sources (
            asset_id TEXT NOT NULL,
            media_item_id INTEGER NOT NULL UNIQUE,
            source_role TEXT NOT NULL DEFAULT 'source',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(asset_id, media_item_id),
            FOREIGN KEY(asset_id) REFERENCES assets(id),
            FOREIGN KEY(media_item_id) REFERENCES media_items(id)
        );

        CREATE TABLE IF NOT EXISTS asset_representations (
            asset_id TEXT NOT NULL,
            library_entry_id TEXT NOT NULL UNIQUE,
            representation_role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(asset_id, library_entry_id),
            FOREIGN KEY(asset_id) REFERENCES assets(id),
            FOREIGN KEY(library_entry_id) REFERENCES library_entries(id)
        );

        CREATE INDEX IF NOT EXISTS idx_assets_state
        ON assets(state);

        CREATE INDEX IF NOT EXISTS idx_assets_merged_into
        ON assets(merged_into_asset_id);

        CREATE INDEX IF NOT EXISTS idx_asset_sources_asset
        ON asset_sources(asset_id);

        CREATE INDEX IF NOT EXISTS idx_asset_representations_asset
        ON asset_representations(asset_id);

        CREATE TABLE IF NOT EXISTS asset_operations (
            id TEXT PRIMARY KEY,
            asset_id TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            state TEXT NOT NULL,
            reason TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY(asset_id) REFERENCES assets(id)
        );

        CREATE INDEX IF NOT EXISTS idx_asset_operations_asset
        ON asset_operations(asset_id, created_at);
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
    for column, definition in (
        ("removed_at", "TEXT"),
        ("purged_at", "TEXT"),
        ("purge_reason", "TEXT"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE assets ADD COLUMN {column} {definition}")


def backfill(connection: sqlite3.Connection) -> dict[str, int]:
    """Idempotently adopt all existing managed source/file relationships."""

    rows = connection.execute(
        """
        SELECT DISTINCT
               mi.id AS media_item_id,
               mi.media_type,
               mf.library_entry_id,
               le.presentation_key,
               le.state AS library_state,
               mf.mime_type
        FROM media_files mf
        JOIN media_items mi ON mi.id = mf.media_item_id
        JOIN library_entries le ON le.id = mf.library_entry_id
        WHERE mf.library_entry_id IS NOT NULL
          AND mf.status != 'skipped'
        ORDER BY mi.id, mf.library_entry_id
        """
    ).fetchall()
    return _backfill_rows(connection, rows)


def needs_backfill(connection: sqlite3.Connection) -> bool:
    """Return whether an adopted file lacks a coherent Asset relationship."""

    row = connection.execute(
        """
        SELECT 1
        FROM media_files mf
        LEFT JOIN asset_sources source ON source.media_item_id = mf.media_item_id
        LEFT JOIN asset_representations representation
               ON representation.library_entry_id = mf.library_entry_id
        WHERE mf.library_entry_id IS NOT NULL
          AND mf.status != 'skipped'
          AND (
              source.asset_id IS NULL
              OR representation.asset_id IS NULL
              OR source.asset_id != representation.asset_id
          )
        LIMIT 1
        """
    ).fetchone()
    return row is not None


def _backfill_rows(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[str, int]:
    if not rows:
        return {"assets_created": 0, "assets_merged": 0, "sources_linked": 0, "representations_linked": 0}

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    row_by_entry: dict[str, sqlite3.Row] = {}
    for row in rows:
        source_node = f"source:{int(row['media_item_id'])}"
        entry_node = f"entry:{row['library_entry_id']}"
        union(source_node, entry_node)
        row_by_entry.setdefault(str(row["library_entry_id"]), row)

    components: dict[str, set[str]] = defaultdict(set)
    for node in parent:
        components[find(node)].add(node)

    existing_by_node: dict[str, str] = {}
    for row in connection.execute("SELECT asset_id, media_item_id FROM asset_sources"):
        existing_by_node[f"source:{int(row['media_item_id'])}"] = str(row["asset_id"])
    for row in connection.execute("SELECT asset_id, library_entry_id FROM asset_representations"):
        existing_by_node[f"entry:{row['library_entry_id']}"] = str(row["asset_id"])

    now = datetime.now(UTC).isoformat()
    created = 0
    merged = 0
    sources_before = int(connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0])
    representations_before = int(
        connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0]
    )

    for nodes in components.values():
        candidates = {
            resolve_asset_id(connection, existing_by_node[node])
            for node in nodes
            if node in existing_by_node
        }
        candidates.discard(None)
        if candidates:
            asset_id = _select_canonical_asset(connection, {str(value) for value in candidates})
            for candidate in sorted(str(value) for value in candidates if value != asset_id):
                _merge_assets(connection, canonical_id=asset_id, merged_id=candidate, now=now)
                merged += 1
        else:
            asset_id = _new_asset(connection, now=now)
            created += 1

        source_ids = sorted(int(node.split(":", 1)[1]) for node in nodes if node.startswith("source:"))
        entry_ids = sorted(node.split(":", 1)[1] for node in nodes if node.startswith("entry:"))
        for media_item_id in source_ids:
            connection.execute(
                """
                INSERT INTO asset_sources (
                    asset_id, media_item_id, source_role, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'source', ?, ?)
                ON CONFLICT(media_item_id) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (asset_id, media_item_id, now, now),
            )
        for entry_id in entry_ids:
            row = row_by_entry[entry_id]
            connection.execute(
                """
                INSERT INTO asset_representations (
                    asset_id, library_entry_id, representation_role,
                    active, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(library_entry_id) DO UPDATE SET
                    asset_id = excluded.asset_id,
                    representation_role = excluded.representation_role,
                    active = excluded.active,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    asset_id,
                    entry_id,
                    representation_role(str(row["presentation_key"])),
                    1 if str(row["library_state"]) == "active" else 0,
                    now,
                    now,
                ),
            )
        _refresh_asset(connection, asset_id=asset_id, now=now)

    sources_after = int(connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0])
    representations_after = int(
        connection.execute("SELECT COUNT(*) FROM asset_representations").fetchone()[0]
    )
    return {
        "assets_created": created,
        "assets_merged": merged,
        "sources_linked": sources_after - sources_before,
        "representations_linked": representations_after - representations_before,
    }


def attach_media_file(db_path: Path, *, file_id: int) -> dict[str, Any]:
    """Attach one adopted media file to a stable Asset, merging aliases safely."""

    from mediagent.core import db

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT mf.id, mf.media_item_id, mf.library_entry_id,
                   le.presentation_key, le.state AS library_state
            FROM media_files mf
            LEFT JOIN library_entries le ON le.id = mf.library_entry_id
            WHERE mf.id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown media file: {file_id}")
        if row["library_entry_id"] is None:
            raise ValueError("Media file must be adopted into the library before Asset attachment.")

        candidate_rows = connection.execute(
            """
            SELECT asset_id, 'source' AS relationship
            FROM asset_sources WHERE media_item_id = ?
            UNION
            SELECT asset_id, 'representation' AS relationship
            FROM asset_representations WHERE library_entry_id = ?
            """,
            (row["media_item_id"], row["library_entry_id"]),
        ).fetchall()
        if str(row["library_state"]) != "active":
            raise ValueError("Only an active library representation can be attached to an active Asset.")
        candidates_by_relationship: dict[str, set[str]] = defaultdict(set)
        for candidate in candidate_rows:
            resolved = resolve_asset_id(connection, str(candidate["asset_id"]))
            if resolved is not None:
                candidates_by_relationship[str(candidate["relationship"])].add(resolved)
        representation_candidates = candidates_by_relationship["representation"]
        for candidate in representation_candidates:
            state = connection.execute(
                "SELECT state FROM assets WHERE id = ?", (candidate,)
            ).fetchone()
            if state is None or str(state["state"]) != "active":
                raise ValueError("An inactive Asset cannot acquire an active representation.")
        candidates = candidates_by_relationship["source"] | representation_candidates
        active_candidates = {
            candidate
            for candidate in candidates
            if str(
                connection.execute(
                    "SELECT state FROM assets WHERE id = ?", (candidate,)
                ).fetchone()["state"]
            )
            == "active"
        }
        if active_candidates:
            asset_id = _select_canonical_asset(connection, active_candidates)
            for candidate in sorted(active_candidates - {asset_id}):
                _merge_assets(connection, canonical_id=asset_id, merged_id=candidate, now=now)
        else:
            asset_id = _new_asset(connection, now=now)

        connection.execute(
            """
            INSERT INTO asset_sources (
                asset_id, media_item_id, source_role, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'source', ?, ?)
            ON CONFLICT(media_item_id) DO UPDATE SET
                asset_id = excluded.asset_id,
                last_seen_at = excluded.last_seen_at
            """,
            (asset_id, row["media_item_id"], now, now),
        )
        connection.execute(
            """
            INSERT INTO asset_representations (
                asset_id, library_entry_id, representation_role,
                active, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_entry_id) DO UPDATE SET
                asset_id = excluded.asset_id,
                representation_role = excluded.representation_role,
                active = excluded.active,
                last_seen_at = excluded.last_seen_at
            """,
            (
                asset_id,
                row["library_entry_id"],
                representation_role(str(row["presentation_key"])),
                1 if str(row["library_state"]) == "active" else 0,
                now,
                now,
            ),
        )
        _refresh_asset(connection, asset_id=asset_id, now=now)
        return get_asset(connection, asset_id)


def attach_media_item(
    db_path: Path,
    *,
    media_item_id: int,
    asset_id: str,
) -> dict[str, Any]:
    """Attach source-only provenance to an existing exact-content Asset."""

    from mediagent.core import db

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        requested_asset_id = resolve_asset_id(connection, asset_id)
        if requested_asset_id is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        requested = connection.execute(
            "SELECT state FROM assets WHERE id = ?", (requested_asset_id,)
        ).fetchone()
        if requested is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        existing = connection.execute(
            "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
            (media_item_id,),
        ).fetchone()
        requested_state = str(requested["state"])
        if requested_state in {"removed", "purged"}:
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO asset_sources (
                        asset_id, media_item_id, source_role, first_seen_at, last_seen_at
                    ) VALUES (?, ?, 'source', ?, ?)
                    """,
                    (requested_asset_id, media_item_id, now, now),
                )
            else:
                current = resolve_asset_id(connection, str(existing["asset_id"]))
                if current == requested_asset_id:
                    connection.execute(
                        "UPDATE asset_sources SET last_seen_at = ? WHERE media_item_id = ?",
                        (now, media_item_id),
                    )
            return get_asset(connection, requested_asset_id)
        if requested_state != "active":
            raise ValueError("A merged Asset cannot accept source provenance.")

        candidates = {requested_asset_id}
        if existing is not None:
            current = resolve_asset_id(connection, str(existing["asset_id"]))
            if current is not None:
                current_state = connection.execute(
                    "SELECT state FROM assets WHERE id = ?", (current,)
                ).fetchone()
                if current_state is not None and str(current_state["state"]) == "active":
                    candidates.add(current)
        canonical_id = _select_canonical_asset(connection, candidates)
        for candidate in sorted(candidates - {canonical_id}):
            _merge_assets(connection, canonical_id=canonical_id, merged_id=candidate, now=now)
        connection.execute(
            """
            INSERT INTO asset_sources (
                asset_id, media_item_id, source_role, first_seen_at, last_seen_at
            ) VALUES (?, ?, 'source', ?, ?)
            ON CONFLICT(media_item_id) DO UPDATE SET
                asset_id = excluded.asset_id,
                last_seen_at = excluded.last_seen_at
            """,
            (canonical_id, media_item_id, now, now),
        )
        _refresh_asset(connection, asset_id=canonical_id, now=now)
        return get_asset(connection, canonical_id)


def attach_media_item_to_inactive_asset(
    db_path: Path,
    *,
    media_item_id: int,
    asset_id: str,
) -> dict[str, Any]:
    """Record inactive provenance without merging or reviving an Asset.

    A multi-file source may already own an active Asset for another file.  In
    that case the source remains attached to the active Asset while the
    content-level tombstone remains independently purged.  A tombstoned
    representation can also belong to an otherwise-active Asset; that case is
    deliberately a no-op for source ownership.
    """

    from mediagent.core import db

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        requested_asset_id = resolve_asset_id(connection, asset_id)
        if requested_asset_id is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        requested = connection.execute(
            "SELECT state FROM assets WHERE id = ?",
            (requested_asset_id,),
        ).fetchone()
        if requested is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        requested_state = str(requested["state"])
        if requested_state == "active":
            return get_asset(connection, requested_asset_id)
        if requested_state not in {"removed", "purged"}:
            raise ValueError("Suppressed provenance requires a managed Asset.")
        existing = connection.execute(
            "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
            (media_item_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO asset_sources (
                    asset_id, media_item_id, source_role, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'source', ?, ?)
                """,
                (requested_asset_id, media_item_id, now, now),
            )
        else:
            current_id = resolve_asset_id(connection, str(existing["asset_id"]))
            if current_id == requested_asset_id:
                connection.execute(
                    "UPDATE asset_sources SET last_seen_at = ? WHERE media_item_id = ?",
                    (now, media_item_id),
                )
        return get_asset(connection, requested_asset_id)


def attach_inactive_media_file(db_path: Path, *, file_id: int) -> dict[str, Any]:
    """Attach a removed or purged representation without reviving an Asset.

    Legacy trash reconciliation needs to establish Asset relationships for
    bytes that are already outside the active library.  Keeping this path
    separate from ``attach_media_file`` prevents inactive and active Assets
    from being merged merely because one source item has multiple files.
    """

    from mediagent.core import db

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT mf.id, mf.media_item_id, mf.library_entry_id,
                   le.presentation_key, le.state AS library_state
            FROM media_files mf
            LEFT JOIN library_entries le ON le.id = mf.library_entry_id
            WHERE mf.id = ?
            """,
            (file_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown media file: {file_id}")
        if row["library_entry_id"] is None:
            raise ValueError("Media file must reference an inactive library entry.")
        library_state = str(row["library_state"])
        if library_state not in {"removed", "purged"}:
            raise ValueError("Inactive attachment requires a removed or purged library entry.")

        representation = connection.execute(
            "SELECT asset_id FROM asset_representations WHERE library_entry_id = ?",
            (row["library_entry_id"],),
        ).fetchone()
        asset_id: str | None = None
        if representation is not None:
            asset_id = resolve_asset_id(connection, str(representation["asset_id"]))
            asset_state = connection.execute(
                "SELECT state FROM assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            if asset_state is None or (
                str(asset_state["state"]) == "purged" and library_state != "purged"
            ):
                raise ValueError("Inactive library representation has an inconsistent Asset state.")
        else:
            source = connection.execute(
                "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
                (row["media_item_id"],),
            ).fetchone()
            if source is not None:
                source_id = resolve_asset_id(connection, str(source["asset_id"]))
                source_state = connection.execute(
                    "SELECT state FROM assets WHERE id = ?",
                    (source_id,),
                ).fetchone()
                if source_state is not None:
                    source_state_value = str(source_state["state"])
                    if source_state_value == "active" or source_state_value == library_state:
                        asset_id = source_id
                    elif source_state_value == "removed" and library_state == "purged":
                        asset_id = source_id
            if asset_id is None:
                asset_id = _new_asset(connection, now=now)

        connection.execute(
            """
            INSERT INTO asset_representations (
                asset_id, library_entry_id, representation_role,
                active, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(library_entry_id) DO UPDATE SET
                asset_id = excluded.asset_id,
                representation_role = excluded.representation_role,
                active = 0,
                last_seen_at = excluded.last_seen_at
            """,
            (
                asset_id,
                row["library_entry_id"],
                representation_role(str(row["presentation_key"])),
                now,
                now,
            ),
        )

        existing_source = connection.execute(
            "SELECT asset_id FROM asset_sources WHERE media_item_id = ?",
            (row["media_item_id"],),
        ).fetchone()
        if existing_source is None:
            connection.execute(
                """
                INSERT INTO asset_sources (
                    asset_id, media_item_id, source_role, first_seen_at, last_seen_at
                ) VALUES (?, ?, 'source', ?, ?)
                """,
                (asset_id, row["media_item_id"], now, now),
            )
        else:
            existing_source_id = resolve_asset_id(connection, str(existing_source["asset_id"]))
            if existing_source_id == asset_id:
                connection.execute(
                    "UPDATE asset_sources SET last_seen_at = ? WHERE media_item_id = ?",
                    (now, row["media_item_id"]),
                )

        _refresh_asset(connection, asset_id=asset_id, now=now)
        return get_asset(connection, asset_id)


def asset_ids_for_media_items(
    db_path: Path,
    items: list[dict[str, Any]],
) -> list[str]:
    """Return canonical Asset IDs for source items in input order."""

    if not items or not db_path.exists():
        return []
    identities = [
        (str(item.get("platform") or ""), str(item.get("remote_id") or ""))
        for item in items
    ]
    identities = [identity for identity in identities if all(identity)]
    if not identities:
        return []
    found: dict[tuple[str, str], str] = {}
    from mediagent.core import db

    with db.connect(db_path) as connection:
        for offset in range(0, len(identities), 400):
            batch = identities[offset : offset + 400]
            placeholders = ",".join("(?, ?)" for _ in batch)
            parameters = [value for identity in batch for value in identity]
            rows = connection.execute(
                f"""
                SELECT mi.platform, mi.remote_id, source.asset_id
                FROM media_items mi
                JOIN asset_sources source ON source.media_item_id = mi.id
                WHERE (mi.platform, mi.remote_id) IN ({placeholders})
                """,
                parameters,
            ).fetchall()
            for row in rows:
                canonical = resolve_asset_id(connection, str(row["asset_id"]))
                if canonical is not None:
                    found[(str(row["platform"]), str(row["remote_id"]))] = canonical
    output: list[str] = []
    seen: set[str] = set()
    for identity in identities:
        asset_id = found.get(identity)
        if asset_id is not None and asset_id not in seen:
            output.append(asset_id)
            seen.add(asset_id)
    return output


def purged_assets_for_media_items(
    db_path: Path,
    items: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    """Return source identities already owned by permanently purged Assets."""

    if not items or not db_path.exists():
        return {}
    identities = {
        (str(item.get("platform") or ""), str(item.get("remote_id") or ""))
        for item in items
        if item.get("platform") and item.get("remote_id")
    }
    if not identities:
        return {}
    from mediagent.core import db

    found: dict[tuple[str, str], str] = {}
    with db.connect(db_path) as connection:
        ordered = sorted(identities)
        for offset in range(0, len(ordered), 400):
            batch = ordered[offset : offset + 400]
            placeholders = ",".join("(?, ?)" for _ in batch)
            parameters = [value for identity in batch for value in identity]
            rows = connection.execute(
                f"""
                SELECT mi.platform, mi.remote_id, source.asset_id
                FROM media_items mi
                JOIN asset_sources source ON source.media_item_id = mi.id
                JOIN assets a ON a.id = source.asset_id
                WHERE a.state = 'purged'
                  AND (mi.platform, mi.remote_id) IN ({placeholders})
                """,
                parameters,
            ).fetchall()
            for row in rows:
                found[(str(row["platform"]), str(row["remote_id"]))] = str(row["asset_id"])
    return found


def resolve_asset_id(connection: sqlite3.Connection, asset_id: str) -> str | None:
    """Resolve an Asset alias to its canonical current identifier."""

    current = asset_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        row = connection.execute(
            "SELECT state, merged_into_asset_id FROM assets WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            return None
        if row["state"] != "merged" or not row["merged_into_asset_id"]:
            return current
        current = str(row["merged_into_asset_id"])
    raise ValueError("Asset merge aliases contain a cycle.")


def get_asset(connection: sqlite3.Connection, asset_id: str) -> dict[str, Any]:
    canonical_id = resolve_asset_id(connection, asset_id)
    if canonical_id is None:
        raise ValueError(f"Unknown Asset: {asset_id}")
    row = connection.execute(
        """
        SELECT id, media_type, state, metadata_json, primary_library_entry_id,
               merged_into_asset_id, removed_at, purged_at, purge_reason,
               created_at, updated_at
        FROM assets WHERE id = ?
        """,
        (canonical_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown Asset: {asset_id}")
    result = dict(row)
    result["metadata"] = _metadata(result.pop("metadata_json"))
    result["source_count"] = int(
        connection.execute("SELECT COUNT(*) FROM asset_sources WHERE asset_id = ?", (canonical_id,)).fetchone()[0]
    )
    result["representation_count"] = int(
        connection.execute(
            "SELECT COUNT(*) FROM asset_representations WHERE asset_id = ?", (canonical_id,)
        ).fetchone()[0]
    )
    if canonical_id != asset_id:
        result["requested_asset_id"] = asset_id
    return result


def load_asset(db_path: Path, asset_id: str) -> dict[str, Any]:
    """Load one canonical Asset while accepting a previously merged identifier."""

    from mediagent.core import db

    with db.connect(db_path) as connection:
        return get_asset(connection, asset_id)


def asset_for_library_entry(db_path: Path, library_entry_id: str) -> dict[str, Any] | None:
    """Return the canonical Asset that owns one managed presentation entry."""

    from mediagent.core import db

    with db.connect(db_path) as connection:
        row = connection.execute(
            "SELECT asset_id FROM asset_representations WHERE library_entry_id = ?",
            (library_entry_id,),
        ).fetchone()
        return get_asset(connection, str(row["asset_id"])) if row else None


def refresh_for_library_entry(db_path: Path, library_entry_id: str) -> dict[str, Any] | None:
    """Synchronize Asset lifecycle state after a library entry changes state."""

    from mediagent.core import db

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT ar.asset_id, le.state
            FROM asset_representations ar
            JOIN library_entries le ON le.id = ar.library_entry_id
            WHERE ar.library_entry_id = ?
            """,
            (library_entry_id,),
        ).fetchone()
        if row is None:
            return None
        asset_id = resolve_asset_id(connection, str(row["asset_id"]))
        if asset_id is None:
            return None
        connection.execute(
            """
            UPDATE asset_representations
            SET active = ?, last_seen_at = ?
            WHERE library_entry_id = ?
            """,
            (1 if row["state"] == "active" else 0, now, library_entry_id),
        )
        _refresh_asset(connection, asset_id=asset_id, now=now)
        return get_asset(connection, asset_id)


def representation_role(presentation_key: str) -> str:
    if presentation_key.startswith("comic-source:"):
        return "source_page"
    if presentation_key.startswith("comic:"):
        return "comic_archive"
    return "original"


def _new_asset(connection: sqlite3.Connection, *, now: str) -> str:
    asset_id = f"asset_{uuid.uuid4().hex}"
    connection.execute(
        """
        INSERT INTO assets (
            id, media_type, state, metadata_json, created_at, updated_at
        ) VALUES (?, 'unknown', 'active', '{"tags": []}', ?, ?)
        """,
        (asset_id, now, now),
    )
    return asset_id


def _select_canonical_asset(connection: sqlite3.Connection, candidates: set[str]) -> str:
    placeholders = ",".join("?" for _ in candidates)
    row = connection.execute(
        f"""
        SELECT id FROM assets
        WHERE id IN ({placeholders}) AND state != 'merged'
        ORDER BY created_at, id
        LIMIT 1
        """,
        tuple(sorted(candidates)),
    ).fetchone()
    if row is None:
        raise ValueError("No canonical Asset is available for the relationship.")
    return str(row["id"])


def _merge_assets(
    connection: sqlite3.Connection,
    *,
    canonical_id: str,
    merged_id: str,
    now: str,
) -> None:
    if canonical_id == merged_id:
        return
    canonical_id = resolve_asset_id(connection, canonical_id) or canonical_id
    merged_id = resolve_asset_id(connection, merged_id) or merged_id
    if canonical_id == merged_id:
        return
    canonical = connection.execute(
        "SELECT metadata_json FROM assets WHERE id = ?", (canonical_id,)
    ).fetchone()
    merged = connection.execute(
        "SELECT metadata_json FROM assets WHERE id = ?", (merged_id,)
    ).fetchone()
    if canonical is None or merged is None:
        raise ValueError("Cannot merge an unknown Asset.")
    connection.execute("UPDATE asset_sources SET asset_id = ? WHERE asset_id = ?", (canonical_id, merged_id))
    connection.execute(
        "UPDATE asset_representations SET asset_id = ? WHERE asset_id = ?",
        (canonical_id, merged_id),
    )
    connection.execute(
        "UPDATE asset_operations SET asset_id = ? WHERE asset_id = ?",
        (canonical_id, merged_id),
    )
    metadata = _merge_metadata(_metadata(canonical["metadata_json"]), _metadata(merged["metadata_json"]))
    connection.execute(
        "UPDATE assets SET metadata_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata, sort_keys=True), now, canonical_id),
    )
    connection.execute(
        """
        UPDATE assets
        SET state = 'merged', merged_into_asset_id = ?, primary_library_entry_id = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (canonical_id, now, merged_id),
    )


def _refresh_asset(connection: sqlite3.Connection, *, asset_id: str, now: str) -> None:
    representation_rows = connection.execute(
        """
        SELECT ar.library_entry_id, ar.representation_role, le.state,
               le.removed_at, le.presentation_key, cb.mime_type
        FROM asset_representations ar
        JOIN library_entries le ON le.id = ar.library_entry_id
        JOIN content_blobs cb ON cb.id = le.content_blob_id
        WHERE ar.asset_id = ?
        ORDER BY
            CASE WHEN le.state = 'active' THEN 0 ELSE 1 END,
            CASE ar.representation_role
                WHEN 'original' THEN 0
                WHEN 'comic_archive' THEN 1
                ELSE 2
            END,
            ar.library_entry_id
        """,
        (asset_id,),
    ).fetchall()
    source_rows = connection.execute(
        """
        SELECT mi.platform, mi.media_type, mi.author_name, mi.metadata_json
        FROM asset_sources source
        JOIN media_items mi ON mi.id = source.media_item_id
        WHERE source.asset_id = ?
        ORDER BY mi.id
        """,
        (asset_id,),
    ).fetchall()
    if not representation_rows:
        return
    active_rows = [row for row in representation_rows if row["state"] == "active"]
    if active_rows:
        state = "active"
    elif representation_rows and all(row["state"] == "purged" for row in representation_rows):
        state = "purged"
    else:
        state = "removed"
    primary = active_rows[0] if active_rows else representation_rows[0]
    media_type = _asset_media_type(source_rows, representation_rows)
    current = connection.execute(
        """
        SELECT metadata_json, removed_at, purged_at, purge_reason
        FROM assets WHERE id = ?
        """,
        (asset_id,),
    ).fetchone()
    metadata = _metadata(current["metadata_json"] if current else None)
    tags = [
        value
        for value in canonicalize_tags(metadata.get("tags"), strict=True)
        if not is_reserved_tag(value)
    ]
    source_tags: list[str] = []
    for source in source_rows:
        source_metadata = _metadata(source["metadata_json"])
        title = _source_title(source_metadata)
        if title and not metadata.get("title"):
            metadata["title"] = title
        author_name = str(source["author_name"] or "").strip()
        if author_name and not metadata.get("author_name"):
            metadata["author_name"] = author_name
        source_tag = _baseline_tag("source", str(source["platform"] or ""))
        if source_tag and tag_key(source_tag) not in {tag_key(value) for value in source_tags}:
            source_tags.append(source_tag)
    type_tag = _baseline_tag("type", media_type)
    metadata["tags"] = canonicalize_tags(
        [*tags, *source_tags, *([type_tag] if type_tag else [])],
        strict=True,
    )
    entry_removed_at = next(
        (
            str(row["removed_at"])
            for row in representation_rows
            if row["removed_at"]
        ),
        None,
    )
    if state == "active":
        removed_at = None
        purged_at = None
        purge_reason = None
    elif state == "removed":
        removed_at = (str(current["removed_at"]) if current and current["removed_at"] else None) or entry_removed_at or now
        purged_at = None
        purge_reason = None
    else:
        removed_at = (str(current["removed_at"]) if current and current["removed_at"] else None) or entry_removed_at or now
        purged_at = (str(current["purged_at"]) if current and current["purged_at"] else None) or now
        purge_reason = (
            str(current["purge_reason"])
            if current and current["purge_reason"]
            else "content permanently purged"
        )
    connection.execute(
        """
        UPDATE assets
        SET media_type = ?, state = ?, primary_library_entry_id = ?,
            metadata_json = ?, merged_into_asset_id = NULL,
            removed_at = ?, purged_at = ?, purge_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            media_type,
            state,
            primary["library_entry_id"],
            json.dumps(metadata, sort_keys=True),
            removed_at,
            purged_at,
            purge_reason,
            now,
            asset_id,
        ),
    )


def _asset_media_type(
    source_rows: list[sqlite3.Row],
    representation_rows: list[sqlite3.Row],
) -> str:
    if any(row["representation_role"] in {"source_page", "comic_archive"} for row in representation_rows):
        return "comic"
    source_types = [str(row["media_type"] or "").lower() for row in source_rows]
    normalized = {"photo": "image", "image": "image", "video": "video", "audio": "audio"}
    for preferred in ("video", "audio", "image"):
        if any(normalized.get(value) == preferred for value in source_types):
            return preferred
    mime_types = [str(row["mime_type"] or "").lower() for row in representation_rows]
    for prefix, media_type in (("video/", "video"), ("audio/", "audio"), ("image/", "image")):
        if any(value.startswith(prefix) for value in mime_types):
            return media_type
    return source_types[0] if source_types else "unknown"


def _metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {"tags": []}
    return dict(parsed) if isinstance(parsed, dict) else {"tags": []}


def _merge_metadata(canonical: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]:
    result = dict(merged)
    result.update(canonical)
    result["tags"] = canonicalize_tags(
        [
            *canonicalize_tags(canonical.get("tags"), strict=True),
            *canonicalize_tags(merged.get("tags"), strict=True),
        ],
        strict=True,
    )
    return result


def _source_title(metadata: dict[str, Any]) -> str | None:
    direct = metadata.get("title")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for namespace in ("comic", "pixiv", "link"):
        nested = metadata.get(namespace)
        if isinstance(nested, dict):
            title = nested.get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()
    return None


def _baseline_tag(namespace: str, value: str) -> str | None:
    normalized = "-".join(value.strip().lower().split())
    normalized = "".join(character for character in normalized if character.isalnum() or character in "._-")
    return f"{namespace}:{normalized}" if normalized else None
