"""Provider manifest comparison and bounded local chapter verification."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from mediagent.core.comics import CBZ_MIME_TYPE, IGNORED_COMIC_SPACER_HEALTH
from mediagent.core.filesystem import ensure_inside
from mediagent.platforms.jmcomic.client import canonical_album_chapters
from mediagent.platforms.jmcomic.parser import JMComicAlbum


def chapter_manifest(album: JMComicAlbum) -> list[dict[str, str]]:
    canonical = canonical_album_chapters(album)
    return [
        {
            "id": episode.photo_id,
            # Album total/latest are observations, not per-chapter changes.
            "fingerprint": hashlib.sha256(json.dumps(
                {"title": episode.title, **canonical[episode.photo_id]},
                sort_keys=True,
            ).encode()).hexdigest(),
        }
        for episode in album.episodes
    ]


def merge_album_metadata(item: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(item)
    metadata = result.setdefault("metadata", {})
    for key in ("comic", "jmcomic"):
        metadata[key] = {**metadata.get(key, {}), **identity["metadata"].get(key, {})}
    return result


def is_archive(record: dict[str, Any]) -> bool:
    return record.get("mime_type") == CBZ_MIME_TYPE or str(record.get("local_path") or "").lower().endswith(".cbz")


def suppressed(item: dict[str, Any]) -> bool:
    return any(
        record.get("library_state") in {"removed", "purged"}
        or record.get("file_health") in {"removed", "purged"}
        for record in item.get("files", [])
    )


def _safe_file(record: dict[str, Any], library_root: Path) -> Path | None:
    raw = record.get("local_path")
    if not raw:
        return None
    path = Path(str(raw)).expanduser().resolve()
    ensure_inside(path, [library_root])
    return path if path.is_file() else None


def local_health(item: dict[str, Any] | None, library_root: Path, *, deep: bool = False) -> dict[str, Any]:
    """Fast path trusts healthy tracked pages behind a present managed archive.

    A bounded deep pass verifies every retained page checksum. An archive alone
    is not proof that a newly observed or partial chapter completed.
    """
    if item is None:
        return {"complete": False, "pages_usable": False, "suppressed": False, "bad_files": [], "filesystem_checks": 0}
    if suppressed(item):
        return {"complete": False, "pages_usable": False, "suppressed": True, "bad_files": [], "filesystem_checks": 0}
    records = item.get("files", [])
    archives = [record for record in records if is_archive(record)]
    source_records = [record for record in records if not is_archive(record)]
    by_key = {record.get("file_key"): record for record in source_records if record.get("file_key")}
    by_url = {record.get("remote_url"): record for record in source_records if record.get("remote_url")}
    expected_files = (item.get("metadata") or {}).get("files") or []
    matched = []
    for file_info in expected_files:
        key = file_info.get("file_key") or file_info.get("content_identity") or file_info.get("stable_url")
        if not key and file_info.get("storage_category") == "comic-pages":
            key = f"page:{int(file_info.get('page_number') or int(file_info.get('page', 0)) + 1):06d}"
        record = by_key.get(key) or by_url.get(file_info.get("url"))
        matched.append(record)
    manifest_present = bool(expected_files) and all(record is not None for record in matched)
    pages = [record for record in matched if record is not None and record.get("file_health") != IGNORED_COMIC_SPACER_HEALTH]
    if manifest_present and not pages and item.get("status") == "skipped":
        return {"complete": True, "pages_usable": True, "suppressed": False, "bad_files": [], "archive_ok": True, "filesystem_checks": 0}
    valid_records = bool(pages) and all(
        record.get("status") == "downloaded" and record.get("file_health") in {"valid", "unknown"}
        for record in pages
    )
    filesystem_checks = 0

    def checked_path(record):
        nonlocal filesystem_checks
        filesystem_checks += 1
        return _safe_file(record, library_root)

    archive_ok = any(
        record.get("status") == "downloaded"
        and record.get("file_health") in {"valid", "unknown"}
        and checked_path(record) is not None
        for record in archives
    )
    bad = []
    if deep:
        for record in archives:
            path = checked_path(record)
            if path is not None and record.get("checksum"):
                with path.open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != str(record["checksum"]).removeprefix("sha256:"):
                    archive_ok = False
                    bad.append({**record, "file_health": "corrupt"})
    if deep or not archive_ok:
        for record in pages:
            path = checked_path(record)
            health = "missing" if path is None else record.get("file_health")
            if path is not None and deep and record.get("checksum"):
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                if checksum != str(record["checksum"]).removeprefix("sha256:"):
                    health = "corrupt"
            if health not in {"valid", "unknown"} or record.get("status") != "downloaded":
                bad.append({**record, "file_health": health or "missing"})
    else:
        bad = [record for record in pages if record.get("file_health") not in {"valid", "unknown"} or record.get("status") != "downloaded"]
    pages_usable = valid_records and manifest_present and not any(not is_archive(record) for record in bad)
    return {
        "complete": item.get("status") == "downloaded" and archive_ok and pages_usable,
        "pages_usable": pages_usable,
        "suppressed": False,
        "bad_files": bad,
        "archive_ok": archive_ok,
        "filesystem_checks": filesystem_checks,
    }
