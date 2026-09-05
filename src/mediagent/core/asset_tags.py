"""Simple Asset tags and read-only library search."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from mediagent.core import assets, db
from mediagent.core.tag_values import (
    canonicalize_tags,
    is_reserved_tag,
    normalize_tags,
    tag_key,
)


MAX_TAGS_PER_ASSET = 128
MAX_SEARCH_TERMS = 8
MAX_SEARCH_TERM_LENGTH = 200
SEARCHABLE_METADATA_KEYS = {
    "author",
    "author_name",
    "caption",
    "description",
    "name",
    "subreddit",
    "summary",
    "tags",
    "title",
    "username",
}


def update_tags(
    db_path: Path,
    *,
    asset_id: str,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
) -> dict[str, Any]:
    """Atomically add or remove equal-priority tags on one canonical Asset."""

    additions = normalize_tags(add)
    removals = normalize_tags(remove)
    if not additions and not removals:
        raise ValueError("Provide at least one tag to add or remove.")
    reserved = [tag for tag in [*additions, *removals] if is_reserved_tag(tag)]
    if reserved:
        raise ValueError("source: and type: tags are maintained automatically.")
    overlap = {tag_key(tag) for tag in additions} & {tag_key(tag) for tag in removals}
    if overlap:
        raise ValueError("The same tag cannot be added and removed in one operation.")

    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical_id = assets.resolve_asset_id(connection, asset_id)
        if canonical_id is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        row = connection.execute(
            "SELECT metadata_json FROM assets WHERE id = ?", (canonical_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Unknown Asset: {asset_id}")
        metadata = _metadata(row["metadata_json"], strict=True)
        current = canonicalize_tags(metadata.get("tags"), strict=True)
        removal_keys = {tag_key(tag) for tag in removals}
        updated = [tag for tag in current if tag_key(tag) not in removal_keys]
        known = {tag_key(tag) for tag in updated}
        for tag in additions:
            if tag_key(tag) not in known:
                updated.append(tag)
                known.add(tag_key(tag))
        if len(updated) > MAX_TAGS_PER_ASSET:
            raise ValueError(f"An Asset may contain at most {MAX_TAGS_PER_ASSET} tags.")
        metadata["tags"] = updated
        connection.execute(
            "UPDATE assets SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, canonical_id),
        )
        result = assets.get_asset(connection, canonical_id)
    result["tags_added"] = [tag for tag in additions if tag_key(tag) not in {tag_key(v) for v in current}]
    result["tags_removed"] = [tag for tag in current if tag_key(tag) in removal_keys]
    return result


def search(
    db_path: Path,
    *,
    terms: Iterable[str] = (),
    include_inactive: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Search Asset tags, descriptive metadata, sources, and filenames."""

    normalized_terms = _search_terms(terms)
    result_limit = int(limit)
    if result_limit < 1 or result_limit > 200:
        raise ValueError("Search limit must be between 1 and 200.")

    clauses = ["a.state != 'merged'" if include_inactive else "a.state = 'active'"]
    parameters: list[Any] = []
    for term in normalized_terms:
        pattern = f"%{_escape_like(_search_key(term))}%"
        clauses.append(
            """
            (
                CASEFOLD(METADATA_TEXT(a.metadata_json)) LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM asset_sources source
                    JOIN media_items mi ON mi.id = source.media_item_id
                    WHERE source.asset_id = a.id
                      AND CASEFOLD(
                          COALESCE(mi.platform, '') || ' ' ||
                          COALESCE(mi.remote_id, '') || ' ' ||
                          COALESCE(mi.author_name, '') || ' ' ||
                          METADATA_TEXT(mi.metadata_json)
                      ) LIKE ? ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1 FROM asset_representations representation
                    JOIN library_entries entry ON entry.id = representation.library_entry_id
                    WHERE representation.asset_id = a.id
                      AND CASEFOLD(
                          COALESCE(entry.display_name_override, '') || ' ' ||
                          BASENAME(COALESCE(entry.library_relative_path, '')) || ' ' ||
                          BASENAME(COALESCE(entry.local_path, ''))
                      ) LIKE ? ESCAPE '\\'
                )
            )
            """
        )
        parameters.extend((pattern, pattern, pattern))

    where = " AND ".join(clauses)
    with db.connect(db_path) as connection:
        connection.create_function(
            "CASEFOLD",
            1,
            _search_key,
            deterministic=True,
        )
        connection.create_function(
            "METADATA_TEXT",
            1,
            _metadata_search_text,
            deterministic=True,
        )
        connection.create_function(
            "BASENAME",
            1,
            lambda value: Path(str(value or "")).name,
            deterministic=True,
        )
        rows = connection.execute(
            f"""
            SELECT a.id, a.media_type, a.state, a.metadata_json, a.updated_at
            FROM assets a
            WHERE {where}
            ORDER BY a.updated_at DESC, a.id
            LIMIT ?
            """,
            (*parameters, result_limit),
        ).fetchall()
        results = [_search_result(connection, row, normalized_terms) for row in rows]
    return {
        "terms": normalized_terms,
        "include_inactive": include_inactive,
        "limit": result_limit,
        "count": len(results),
        "assets": results,
    }


def _search_result(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    terms: list[str],
) -> dict[str, Any]:
    asset_id = str(row["id"])
    metadata = _metadata(row["metadata_json"])
    tags = canonicalize_tags(metadata.get("tags"))
    sources: list[dict[str, str]] = []
    source_search_values: list[str] = []
    for source in connection.execute(
        """
        SELECT mi.platform, mi.remote_id, mi.author_name, mi.metadata_json
        FROM asset_sources relationship
        JOIN media_items mi ON mi.id = relationship.media_item_id
        WHERE relationship.asset_id = ?
        ORDER BY mi.platform, mi.remote_id
        """,
        (asset_id,),
    ):
        source_result = {
            "platform": str(source["platform"]),
            "remote_id": str(source["remote_id"]),
            **({"author_name": str(source["author_name"])} if source["author_name"] else {}),
        }
        sources.append(source_result)
        source_search_values.extend(str(value) for value in source_result.values())
        source_search_values.append(_metadata_search_text(source["metadata_json"]))
    paths = [
        {
            "path": str(entry["local_path"]),
            "library_relative_path": str(entry["library_relative_path"]),
            **(
                {"display_name": str(entry["display_name_override"])}
                if entry["display_name_override"]
                else {}
            ),
            "state": str(entry["state"]),
            "role": str(entry["representation_role"]),
        }
        for entry in connection.execute(
            """
            SELECT le.local_path, le.library_relative_path, le.display_name_override,
                   le.state, representation.representation_role
            FROM asset_representations representation
            JOIN library_entries le ON le.id = representation.library_entry_id
            WHERE representation.asset_id = ?
            ORDER BY CASE le.state WHEN 'active' THEN 0 ELSE 1 END, le.local_path
            """,
            (asset_id,),
        )
    ]
    searchable = {
        "tags": " ".join(tags),
        "title": str(metadata.get("title") or ""),
        "author": str(metadata.get("author_name") or ""),
        "source": " ".join(source_search_values),
        "filename": " ".join(
            [
                *[str(item.get("display_name") or "") for item in paths],
                *[Path(item["path"]).name for item in paths],
            ]
        ),
    }
    matched_fields = [
        field
        for field, value in searchable.items()
        if terms and any(_search_key(term) in _search_key(value) for term in terms)
    ]
    return {
        "asset_id": asset_id,
        "state": str(row["state"]),
        "media_type": str(row["media_type"]),
        "title": str(metadata.get("title") or ""),
        "author_name": str(metadata.get("author_name") or ""),
        "tags": tags,
        "paths": paths,
        "sources": sources,
        "matched_fields": matched_fields,
        "updated_at": str(row["updated_at"]),
    }


def _search_terms(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Search terms must be provided as a list of strings.")
    terms: list[str] = []
    for value in values:
        term = unicodedata.normalize("NFC", str(value)).strip()
        if not term:
            continue
        if len(term) > MAX_SEARCH_TERM_LENGTH:
            raise ValueError(f"Search terms must not exceed {MAX_SEARCH_TERM_LENGTH} characters.")
        if any(unicodedata.category(character).startswith("C") for character in term):
            raise ValueError("Search terms must not contain control characters.")
        terms.append(term)
    if len(terms) > MAX_SEARCH_TERMS:
        raise ValueError(f"Search accepts at most {MAX_SEARCH_TERMS} terms.")
    return terms


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _metadata(raw: Any, *, strict: bool = False) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        if strict:
            raise ValueError("Asset metadata is invalid and cannot be updated.") from None
        return {"tags": []}
    if isinstance(parsed, dict):
        return dict(parsed)
    if strict:
        raise ValueError("Asset metadata is invalid and cannot be updated.")
    return {"tags": []}


def _metadata_search_text(raw: Any) -> str:
    values: list[str] = []

    def visit(value: Any, *, key: str | None = None) -> None:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                visit(nested_value, key=str(nested_key).casefold())
            return
        if isinstance(value, list):
            for item in value:
                if key == "tags" and isinstance(item, str):
                    values.append(item)
                elif isinstance(item, (dict, list)):
                    visit(item, key=key)
            return
        if key in SEARCHABLE_METADATA_KEYS and isinstance(value, str):
            values.append(value)

    visit(_metadata(raw))
    return " ".join(values)


def _search_key(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    return unicodedata.normalize("NFC", text.casefold())
