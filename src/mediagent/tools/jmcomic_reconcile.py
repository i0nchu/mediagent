"""Plan and apply precise JMComic chapter metadata and CBZ repairs."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from mediagent.core import db, library_content
from mediagent.core.comics import (
    CBZ_MIME_TYPE,
    CBZ_STORAGE_LAYOUT,
    build_cbz_atomic,
    comic_archive_relative_path,
    comic_info_xml,
)
from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.storage import safe_storage_segment
from mediagent.platforms.jmcomic.client import canonical_album_chapters
from mediagent.platforms.jmcomic.parser import JMComicAlbum
from mediagent.tools.pixiv_library_tools import _comic_package_plan


def select_jmcomic_items(db_path: Path) -> list[dict[str, Any]]:
    with db.connect(db_path) as connection:
        item_rows = connection.execute(
            """
            SELECT id, remote_id, source_url, author_id, author_name, media_type,
                   status, metadata_json, source_availability, created_at, updated_at
            FROM media_items
            WHERE platform = 'jmcomic'
            ORDER BY id
            """
        ).fetchall()
        file_rows = connection.execute(
            """
            SELECT id, media_item_id, file_key, remote_url, local_path, mime_type,
                   size_bytes, checksum, status, library_relative_path,
                   storage_layout, file_health, source_timestamp, verified_at
            FROM media_files
            WHERE media_item_id IN (
                SELECT id FROM media_items WHERE platform = 'jmcomic'
            )
            ORDER BY media_item_id, id
            """
        ).fetchall()
    files: dict[int, list[dict[str, Any]]] = {}
    for row in file_rows:
        files.setdefault(int(row["media_item_id"]), []).append(dict(row))
    return [
        {
            **{key: row[key] for key in row.keys() if key != "metadata_json"},
            "platform": "jmcomic",
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "files": files.get(int(row["id"]), []),
        }
        for row in item_rows
    ]


def album_id_for_item(item: dict[str, Any]) -> str | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    comic = metadata.get("comic") if isinstance(metadata.get("comic"), dict) else {}
    provider = metadata.get("jmcomic") if isinstance(metadata.get("jmcomic"), dict) else {}
    value = str(provider.get("album_id") or comic.get("series_id") or "").strip()
    return value if value.isdigit() and int(value) > 0 else None


def identities_from_album(album: JMComicAlbum) -> list[dict[str, Any]]:
    """Return metadata-only identities without fetching chapter/page payloads."""

    identities = canonical_album_chapters(album)
    items: list[dict[str, Any]] = []
    for episode in album.episodes:
        identity = identities[episode.photo_id]
        chapter_number = identity["chapter_number"]
        comic = {
            "provider": "jmcomic",
            "provider_work_id": f"photo:{episode.photo_id}",
            "series_id": album.album_id,
            "series_title": album.title,
            "directory_title": f"JM {album.album_id}",
            "archive_title": f"Chapter {chapter_number}",
            "chapter_number": chapter_number,
            "chapter_number_source": identity["chapter_number_source"],
            "provider_chapter_number": identity["provider_chapter_number"],
            "album_position": identity["album_position"],
            "chapter_collision_index": identity["chapter_collision_index"],
            "total_count": len(album.episodes),
            # Album-scoped identity remains a series even while it has one
            # chapter, matching normal exact/favorite resolution.
            "is_one_shot": False,
        }
        items.append(
            {
                "remote_id": f"photo:{episode.photo_id}",
                "metadata": {
                    "comic": comic,
                    "jmcomic": {
                        "entity_type": "photo",
                        "photo_id": episode.photo_id,
                        "album_id": album.album_id,
                        "chapter_number": chapter_number,
                        "chapter_number_source": identity["chapter_number_source"],
                        "provider_chapter_number": identity["provider_chapter_number"],
                        "album_position": identity["album_position"],
                        "chapter_collision_index": identity["chapter_collision_index"],
                    },
                },
            }
        )
    return items


def build_manifest(
    *,
    db_path: Path,
    library_root: Path,
    quarantine_dir: Path,
    resolved_by_album: dict[str, list[dict[str, Any]]],
    failed_albums: dict[str, dict[str, str]],
    include_platform_layer: bool,
    selected_album_ids: set[str] | None = None,
) -> dict[str, Any]:
    library_root = library_root.resolve()
    quarantine_dir = quarantine_dir.resolve()
    ensure_inside(quarantine_dir, [library_root])
    items = select_jmcomic_items(db_path)
    if selected_album_ids:
        items = [item for item in items if album_id_for_item(item) in selected_album_ids]
    items_by_album: dict[str, list[dict[str, Any]]] = {}
    invalid_items: list[dict[str, str]] = []
    for item in items:
        album_id = album_id_for_item(item)
        if album_id is None:
            invalid_items.append(
                {"remote_id": str(item["remote_id"]), "reason": "missing_numeric_album_id"}
            )
            continue
        items_by_album.setdefault(album_id, []).append(item)

    actions: list[dict[str, Any]] = []
    missing_from_manifest: list[dict[str, str]] = []
    new_manifest_items: list[dict[str, str]] = []
    summary = {
        "albums_scanned": len(items_by_album),
        "items_scanned": len(items),
        "archives_checked": 0,
        "already_correct": 0,
        "metadata_updates": 0,
        "archives_to_rebuild": 0,
        "archives_missing": 0,
        "chapter_collisions": 0,
        "manifest_new_items": 0,
        "missing_from_manifest": 0,
        "blocked": len(failed_albums) + len(invalid_items),
    }

    for album_id, album_items in items_by_album.items():
        resolved_items = resolved_by_album.get(album_id)
        if resolved_items is None:
            continue
        resolved_by_id = {str(item["remote_id"]): item for item in resolved_items}
        existing_ids = {str(item["remote_id"]) for item in album_items}
        for remote_id in sorted(set(resolved_by_id) - existing_ids):
            new_manifest_items.append({"album_id": album_id, "remote_id": remote_id})
        for item in album_items:
            remote_id = str(item["remote_id"])
            resolved = resolved_by_id.get(remote_id)
            if resolved is None:
                missing_from_manifest.append({"album_id": album_id, "remote_id": remote_id})
                summary["blocked"] += 1
                continue
            action = _build_item_action(
                item=item,
                resolved_item=resolved,
                library_root=library_root,
                quarantine_dir=quarantine_dir,
                include_platform_layer=include_platform_layer,
            )
            actions.append(action)
            summary["archives_checked"] += int(action["archive_record"] is not None)
            summary["already_correct"] += int(action["status"] == "correct")
            summary["metadata_updates"] += int(action["metadata_changed"])
            summary["archives_to_rebuild"] += int(action["status"] == "rebuild")
            summary["archives_missing"] += int(not action["archive_present"])
            summary["chapter_collisions"] += int(action["chapter_collision_index"] > 0)
            summary["blocked"] += int(action["status"] == "blocked")

    summary["manifest_new_items"] = len(new_manifest_items)
    summary["missing_from_manifest"] = len(missing_from_manifest)
    return {
        "summary": summary,
        "actions": actions,
        "failed_albums": failed_albums,
        "invalid_items": invalid_items,
        "missing_from_manifest": missing_from_manifest,
        "new_manifest_items": new_manifest_items,
    }


def _build_item_action(
    *,
    item: dict[str, Any],
    resolved_item: dict[str, Any],
    library_root: Path,
    quarantine_dir: Path,
    include_platform_layer: bool,
) -> dict[str, Any]:
    desired_metadata = _desired_metadata(item["metadata"], resolved_item.get("metadata") or {})
    desired_item = {**item, "metadata": desired_metadata}
    desired_relative = comic_archive_relative_path(
        item=desired_item,
        include_platform_layer=include_platform_layer,
    )
    desired_path = (library_root / desired_relative).resolve()
    ensure_inside(desired_path, [library_root])
    archive_records = [record for record in item["files"] if _is_cbz(record)]
    metadata_changed = desired_metadata != item["metadata"]
    comic = desired_metadata.get("comic") or {}
    action: dict[str, Any] = {
        "remote_id": str(item["remote_id"]),
        "album_id": str(comic.get("series_id") or ""),
        "current_chapter_number": (item["metadata"].get("comic") or {}).get("chapter_number"),
        "desired_chapter_number": comic.get("chapter_number"),
        "chapter_number_source": comic.get("chapter_number_source"),
        "chapter_collision_index": int(comic.get("chapter_collision_index") or 0),
        "metadata_changed": metadata_changed,
        "desired_relative_path": desired_relative.as_posix(),
        "desired_path": str(desired_path),
        "archive_record": archive_records[0] if len(archive_records) == 1 else None,
        "archive_present": False,
        "current_path": None,
        "quarantine_path": None,
        "status": "correct",
        "reason": None,
        "item": desired_item,
        "package_plan": None,
    }
    if len(archive_records) > 1:
        action["status"] = "blocked"
        action["reason"] = "multiple_cbz_records"
        return action

    current_path: Path | None = None
    if archive_records:
        raw_path = str(archive_records[0].get("local_path") or "").strip()
        if raw_path:
            current_path = Path(raw_path).expanduser().resolve()
            try:
                ensure_inside(current_path, [library_root])
            except PathSafetyError:
                action["status"] = "blocked"
                action["reason"] = "archive_path_outside_library"
                return action
            # A prior operator cleanup is authoritative.  Never revive or move
            # an archive that is already under .trash; build a fresh package
            # from healthy source pages and leave the trashed file untouched.
            if _is_in_trash(current_path, library_root):
                current_path = None
                action["archive_in_trash"] = True
            else:
                action["current_path"] = str(current_path)
                action["archive_present"] = current_path.is_file()

    package_plan = _comic_package_plan(
        item=desired_item,
        library_root=library_root,
        include_platform_layer=include_platform_layer,
        overwrite=True,
        migrate_legacy=True,
    )
    action["package_plan"] = package_plan
    if package_plan["status"] not in {"ready", "cleanup"}:
        action["status"] = "blocked"
        action["reason"] = str(package_plan.get("reason") or package_plan["status"])
        return action

    archive_matches = (
        current_path is not None
        and current_path == desired_path
        and current_path.is_file()
        and _comic_info_matches(
            current_path,
            desired_item,
            int(package_plan["page_count"]),
        )
    )
    if archive_matches:
        return action

    if desired_path.exists() and desired_path != current_path:
        action["status"] = "blocked"
        action["reason"] = "desired_archive_path_conflict"
        return action

    if current_path is not None and current_path.exists():
        quarantine_path = (
            quarantine_dir
            / safe_storage_segment(str(item["remote_id"]), max_length=64)
            / current_path.name
        ).resolve()
        ensure_inside(quarantine_path, [quarantine_dir])
        if quarantine_path.exists():
            action["status"] = "blocked"
            action["reason"] = "quarantine_path_conflict"
            return action
        action["quarantine_path"] = str(quarantine_path)
    action["status"] = "rebuild"
    action["reason"] = "archive_missing_or_metadata_mismatch"
    return action


def _desired_metadata(existing: dict[str, Any], resolved: dict[str, Any]) -> dict[str, Any]:
    desired = deepcopy(existing)
    for key in ("title", "work_type", "storage_category", "page_count"):
        if key in resolved:
            desired[key] = deepcopy(resolved[key])
    if isinstance(resolved.get("comic"), dict):
        current_comic = desired.get("comic") if isinstance(desired.get("comic"), dict) else {}
        desired["comic"] = {**current_comic, **deepcopy(resolved["comic"])}
    if isinstance(resolved.get("jmcomic"), dict):
        current_provider = desired.get("jmcomic") if isinstance(desired.get("jmcomic"), dict) else {}
        desired["jmcomic"] = {**current_provider, **deepcopy(resolved["jmcomic"])}
    # Existing page URLs identify the files already on disk. Reconciliation is
    # metadata/package repair, not a redownload or CDN URL migration.
    if isinstance(existing.get("files"), list):
        desired["files"] = deepcopy(existing["files"])
    return desired


_COMIC_IDENTITY_FIELDS = (
    "Title",
    "Series",
    "SeriesSort",
    "Number",
    "Volume",
    "Format",
    "Publisher",
    "Web",
    "PageCount",
)


def _comic_info_matches(path: Path, item: dict[str, Any], page_count: int) -> bool:
    try:
        expected = ElementTree.fromstring(comic_info_xml(item=item, page_count=page_count))
        with ZipFile(path) as archive:
            actual = ElementTree.fromstring(archive.read("ComicInfo.xml"))
        return all(
            (actual.findtext(field) or "") == (expected.findtext(field) or "")
            for field in _COMIC_IDENTITY_FIELDS
        )
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError, ValueError):
        return False


def _is_cbz(record: dict[str, Any]) -> bool:
    return record.get("mime_type") == CBZ_MIME_TYPE or str(record.get("local_path") or "").lower().endswith(".cbz")


def _is_in_trash(path: Path, library_root: Path) -> bool:
    try:
        relative = path.relative_to(library_root)
    except ValueError:
        return False
    return any(part.lower() == ".trash" for part in relative.parts)


def public_manifest(manifest: dict[str, Any], *, include_details: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "summary": dict(manifest["summary"]),
        "failed_albums": [
            {"album_id": album_id, **error}
            for album_id, error in sorted(manifest["failed_albums"].items())
        ],
        "invalid_items": list(manifest["invalid_items"]),
        "missing_from_manifest": list(manifest["missing_from_manifest"]),
        "new_manifest_items": list(manifest["new_manifest_items"]),
    }
    if manifest.get("applied_at"):
        result["applied_at"] = manifest["applied_at"]
    if include_details:
        result["items"] = [
            {
                key: action.get(key)
                for key in (
                    "remote_id",
                    "album_id",
                    "status",
                    "reason",
                    "current_chapter_number",
                    "desired_chapter_number",
                    "chapter_number_source",
                    "chapter_collision_index",
                    "metadata_changed",
                    "current_path",
                    "desired_path",
                    "quarantine_path",
                )
            }
            for action in manifest["actions"]
        ]
    if manifest.get("apply_results") is not None:
        result["apply_results"] = list(manifest["apply_results"])
    return result


def apply_manifest(*, db_path: Path, library_root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    applied = 0
    failed = 0
    quarantined = 0
    for action in manifest["actions"]:
        if action["status"] == "blocked":
            results.append({"remote_id": action["remote_id"], "status": "blocked", "reason": action["reason"]})
            failed += 1
            continue
        if action["status"] == "correct":
            if action["metadata_changed"]:
                _update_item_metadata(db_path, action)
                applied += 1
                results.append({"remote_id": action["remote_id"], "status": "metadata_updated"})
            else:
                results.append({"remote_id": action["remote_id"], "status": "unchanged"})
            continue
        try:
            package = _apply_rebuild_action(db_path=db_path, library_root=library_root, action=action)
        except Exception as exc:
            failed += 1
            results.append(
                {
                    "remote_id": action["remote_id"],
                    "status": "failed",
                    "reason": "rebuild_failed",
                    "error": {"exception_type": type(exc).__name__},
                }
            )
            continue
        applied += 1
        quarantined += int(bool(package.get("quarantine_path")))
        results.append({"remote_id": action["remote_id"], "status": "rebuilt", **package})

    manifest["applied_at"] = datetime.now(UTC).isoformat()
    manifest["apply_results"] = results
    manifest["summary"] = {
        **manifest["summary"],
        "applied": applied,
        "apply_failed": failed,
        "archives_quarantined": quarantined,
    }
    return manifest


def _update_item_metadata(db_path: Path, action: dict[str, Any]) -> None:
    now = datetime.now(UTC).isoformat()
    with db.connect(db_path) as connection:
        connection.execute(
            "UPDATE media_items SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(action["item"]["metadata"], sort_keys=True), now, int(action["item"]["id"])),
        )


def _apply_rebuild_action(*, db_path: Path, library_root: Path, action: dict[str, Any]) -> dict[str, Any]:
    current_path = Path(action["current_path"]) if action.get("current_path") else None
    quarantine_path = Path(action["quarantine_path"]) if action.get("quarantine_path") else None
    target_path = Path(action["desired_path"])
    moved_old = False
    built_new = False
    try:
        if current_path is not None and current_path.exists():
            if quarantine_path is None:
                raise ValueError("Existing archive requires a quarantine path.")
            quarantine_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(current_path, quarantine_path)
            moved_old = True
        package = build_cbz_atomic(
            target_path=target_path,
            pages=action["package_plan"]["pages"],
            item=action["item"],
            allowed_root=library_root,
        )
        built_new = True
        _commit_rebuilt_archive(db_path=db_path, action=action, package=package)
    except Exception:
        if built_new and target_path.exists():
            target_path.unlink()
        if moved_old and quarantine_path is not None and quarantine_path.exists() and current_path is not None:
            current_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantine_path, current_path)
        raise
    return {
        "target_path": package["target_path"],
        "page_count": package["pages"],
        "size_bytes": package["size_bytes"],
        "checksum": package["checksum"],
        "quarantine_path": str(quarantine_path) if moved_old and quarantine_path else None,
    }


def _commit_rebuilt_archive(*, db_path: Path, action: dict[str, Any], package: dict[str, Any]) -> None:
    now = datetime.now(UTC).isoformat()
    item_id = int(action["item"]["id"])
    archive_record = action.get("archive_record")
    file_id: int
    with db.connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE media_items SET metadata_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(action["item"]["metadata"], sort_keys=True), now, item_id),
        )
        if archive_record is not None:
            file_id = int(archive_record["id"])
            connection.execute(
                """
                UPDATE media_files
                SET remote_url = ?, local_path = ?, mime_type = ?, size_bytes = ?,
                    checksum = ?, status = ?, library_relative_path = ?,
                    storage_layout = ?, file_health = ?, verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    None,
                    package["target_path"],
                    CBZ_MIME_TYPE,
                    int(package["size_bytes"]),
                    package["checksum"],
                    "downloaded",
                    action["desired_relative_path"],
                    CBZ_STORAGE_LAYOUT,
                    "valid",
                    now,
                    now,
                    int(archive_record["id"]),
                ),
            )
        else:
            cursor = connection.execute(
                """
                INSERT INTO media_files (
                    media_item_id, file_key, remote_url, local_path, mime_type,
                    size_bytes, checksum, status, library_relative_path,
                    storage_layout, file_health, source_timestamp, verified_at,
                    created_at, updated_at
                ) VALUES (?, 'archive:cbz', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    None,
                    package["target_path"],
                    CBZ_MIME_TYPE,
                    int(package["size_bytes"]),
                    package["checksum"],
                    "downloaded",
                    action["desired_relative_path"],
                    CBZ_STORAGE_LAYOUT,
                    "valid",
                    now,
                    now,
                    now,
                    now,
                ),
            )
            file_id = int(cursor.lastrowid)
    library_content.adopt_media_file(db_path, file_id=file_id)
