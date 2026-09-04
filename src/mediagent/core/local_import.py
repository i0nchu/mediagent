"""Safe copy-only intake for explicit local files and directories."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

from mediagent.core import assets, db, library_content
from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.storage import extension_from_mime, normalize_extension, safe_storage_segment


ProgressCallback = Callable[[int, int, int], None]
IMAGE_SUFFIXES = frozenset({".avif", ".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
VIDEO_SUFFIXES = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ogv", ".webm"})
AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".wav"})
COMIC_SUFFIXES = frozenset({".cbz"})
EXPECTED_MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES | AUDIO_SUFFIXES | COMIC_SUFFIXES
IMAGE_ARCHIVE_SUFFIXES = IMAGE_SUFFIXES


class UnsupportedLocalMediaError(ValueError):
    pass


class BrokenLocalMediaError(ValueError):
    pass


@dataclass(frozen=True)
class DetectedMedia:
    media_type: str
    storage_category: str
    mime_type: str
    extension: str
    representation_role: str


@dataclass(frozen=True)
class ExistingIdentity:
    asset_id: str
    library_entry_id: str
    state: str
    local_path: str
    library_relative_path: str | None
    mime_type: str | None
    size_bytes: int
    checksum: str


def import_input(
    *,
    db_path: Path,
    library_root: Path,
    input_path: Path,
    configured_library_roots: Iterable[Path],
    data_dir: Path | None,
    dry_run: bool,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Import one explicit local path without modifying external source files."""

    root = library_root.expanduser().resolve()
    roots = _unique_roots([root, *configured_library_roots])
    trash_roots = [(item / ".trash").resolve() for item in roots]
    directory_exclusions = [*roots, *trash_roots]
    if data_dir is not None:
        directory_exclusions.append(data_dir.expanduser().resolve())
    candidates, discovery_results = discover_files(
        input_path,
        directory_exclusions=directory_exclusions,
        forbidden_file_roots=trash_roots,
    )
    input_kind = "file" if Path(os.path.abspath(input_path.expanduser())).resolve().is_file() else "directory"
    if not dry_run:
        db.initialize_database(db_path)

    results = list(discovery_results)
    artifacts: list[str] = []
    failed = sum(item["status"] == "failed" for item in results)
    for index, source in enumerate(candidates, start=1):
        try:
            result = _import_file(
                db_path=db_path,
                library_root=root,
                configured_library_roots=roots,
                source=source,
                dry_run=dry_run,
            )
        except UnsupportedLocalMediaError as exc:
            result = {"source_path": str(source), "status": "unsupported", "reason": str(exc)}
        except (BrokenLocalMediaError, OSError, ValueError) as exc:
            result = {
                "source_path": str(source),
                "status": "failed",
                "reason": str(exc),
                "exception_type": type(exc).__name__,
            }
            failed += 1
        results.append(result)
        if result.get("target_path") and result["status"] in {"imported", "adopted", "repaired"}:
            artifacts.append(str(result["target_path"]))
        if progress is not None:
            progress(index, len(candidates) - index, failed)

    summary = {
        "scanned": len(candidates),
        "imported": sum(item["status"] == "imported" for item in results),
        "adopted": sum(item["status"] == "adopted" for item in results),
        "existing": sum(item["status"] == "existing" for item in results),
        "repaired": sum(item["status"] == "repaired" for item in results),
        "blocked": sum(item["status"] == "blocked" for item in results),
        "unsupported": sum(item["status"] == "unsupported" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
        "symlinks_skipped": sum(item["status"] == "symlink_skipped" for item in results),
        "excluded": sum(item["status"] == "excluded" for item in results),
        "bytes_copied": sum(
            int(item.get("size_bytes") or 0)
            for item in results
            if item.get("copied") is True and not dry_run
        ),
        "would_import": sum(item["status"] == "would_import" for item in results),
        "would_adopt": sum(item["status"] == "would_adopt" for item in results),
        "would_repair": sum(item["status"] == "would_repair" for item in results),
    }
    asset_ids = list(
        dict.fromkeys(
            str(item["asset_id"])
            for item in results
            if item.get("asset_id")
        )
    )
    return {
        "dry_run": dry_run,
        "input_kind": input_kind,
        "input_path": str(input_path),
        "library_root": str(root),
        "summary": summary,
        "asset_ids": asset_ids,
        "results": results,
        "artifacts": artifacts,
    }


def discover_files(
    input_path: Path,
    *,
    directory_exclusions: Iterable[Path],
    forbidden_file_roots: Iterable[Path],
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Discover regular files deterministically without following any symlink."""

    lexical = Path(os.path.abspath(input_path.expanduser()))
    if _has_symlink_component(lexical):
        raise PathSafetyError("Local input must not pass through a symbolic link.")
    if not lexical.exists():
        raise FileNotFoundError(str(lexical))
    resolved = lexical.resolve(strict=True)
    forbidden_files = _unique_roots(forbidden_file_roots)
    if any(_inside(resolved, root) for root in forbidden_files):
        raise PathSafetyError("Managed trash content cannot be imported directly.")
    if resolved.is_file():
        if not stat.S_ISREG(resolved.stat(follow_symlinks=False).st_mode):
            raise PathSafetyError("Local input is not a regular file.")
        return [resolved], []
    if not resolved.is_dir():
        raise PathSafetyError("Local input must be a regular file or directory.")

    excluded_directories = _unique_roots(directory_exclusions)
    if any(_inside(resolved, root) for root in excluded_directories):
        raise PathSafetyError("Configured data or trash directories cannot be recursively imported.")

    candidates: list[Path] = []
    results: list[dict[str, Any]] = []
    pending = [resolved]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name.casefold())
        except OSError as exc:
            results.append(
                {
                    "source_path": str(directory),
                    "status": "failed",
                    "reason": str(exc),
                    "exception_type": type(exc).__name__,
                }
            )
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink():
                    results.append(
                        {
                            "source_path": str(path),
                            "status": "symlink_skipped",
                            "reason": "Symbolic links are not followed.",
                        }
                    )
                elif entry.is_dir(follow_symlinks=False):
                    child = path.resolve(strict=True)
                    if any(_inside(child, root) for root in excluded_directories):
                        results.append(
                            {
                                "source_path": str(child),
                                "status": "excluded",
                                "reason": "Configured data or trash directory was excluded.",
                            }
                        )
                    else:
                        pending.append(child)
                elif entry.is_file(follow_symlinks=False):
                    candidates.append(path.resolve(strict=True))
                else:
                    results.append(
                        {
                            "source_path": str(path),
                            "status": "unsupported",
                            "reason": "Non-regular filesystem entry.",
                        }
                    )
            except OSError as exc:
                results.append(
                    {
                        "source_path": str(path),
                        "status": "failed",
                        "reason": str(exc),
                        "exception_type": type(exc).__name__,
                    }
                )
    return sorted(candidates, key=lambda path: str(path).casefold()), results


def detect_media(path: Path) -> DetectedMedia:
    """Validate supported content and return a normalized media classification."""

    suffix = path.suffix.lower()
    if suffix in COMIC_SUFFIXES:
        return _detect_cbz(path)
    try:
        with Image.open(path) as image:
            image_format = str(image.format or "").upper()
            image.verify()
        mime_type = Image.MIME.get(image_format) or mimetypes.guess_type(path.name)[0]
        if not mime_type or not mime_type.startswith("image/"):
            raise BrokenLocalMediaError("Image content has no recognized MIME type.")
        extension = _preferred_extension(path, mime_type)
        return DetectedMedia("photo", "photo", mime_type, extension, "original")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        if suffix in IMAGE_SUFFIXES:
            raise BrokenLocalMediaError("Image content is unreadable or invalid.") from exc

    with path.open("rb") as stream:
        header = stream.read(64)
    guessed = (mimetypes.guess_type(path.name)[0] or "").lower()
    if _is_audio(header, suffix):
        mime_type = guessed if guessed.startswith("audio/") else _audio_mime(header, suffix)
        return DetectedMedia("audio", "audio", mime_type, _preferred_extension(path, mime_type), "original")
    if _is_video(header, suffix):
        mime_type = guessed if guessed.startswith("video/") else _video_mime(header, suffix)
        return DetectedMedia("video", "video", mime_type, _preferred_extension(path, mime_type), "original")
    if suffix in EXPECTED_MEDIA_SUFFIXES:
        raise BrokenLocalMediaError("Media content does not match a supported file signature.")
    raise UnsupportedLocalMediaError("File type is not supported for local media import.")


def _import_file(
    *,
    db_path: Path,
    library_root: Path,
    configured_library_roots: list[Path],
    source: Path,
    dry_run: bool,
) -> dict[str, Any]:
    detected = detect_media(source)
    checksum, size_bytes = library_content.sha256_checksum(source)
    source_stat = source.stat()
    source_timestamp = datetime.fromtimestamp(source_stat.st_mtime, tz=UTC).isoformat()
    managed_root = _containing_root(source, configured_library_roots)
    path_identity = _identity_for_path(source, checksum)

    path_entry = _identity_for_path_entry(db_path, source) if db_path.exists() else None
    if path_entry is not None and path_entry.checksum != checksum:
        raise BrokenLocalMediaError("Managed file content changed without a matching database update.")
    identity = path_entry or (
        _identity_for_checksum(db_path, checksum, detected.representation_role)
        if db_path.exists()
        else None
    )
    if identity is not None:
        status = "existing" if identity.state == "active" else "blocked"
        repair_target: Path | None = None
        if status == "existing":
            tracked_path = Path(identity.local_path)
            tracked_healthy = False
            if tracked_path.is_file() and not tracked_path.is_symlink():
                tracked_checksum, tracked_size = library_content.sha256_checksum(tracked_path)
                tracked_healthy = tracked_checksum == checksum and tracked_size == size_bytes
            if not tracked_healthy:
                repair_target = tracked_path.resolve()
        if repair_target is not None:
            repair_root = _containing_root(repair_target, configured_library_roots)
            if repair_root is None:
                raise PathSafetyError("Tracked repair target is outside configured library roots.")
            status = "would_repair" if dry_run else "repaired"
        if dry_run:
            return _result(
                source=source,
                status=status,
                detected=detected,
                checksum=checksum,
                size_bytes=size_bytes,
                asset_id=identity.asset_id,
                target_path=(identity.local_path if status in {"existing", "would_repair"} else None),
            )
        copied = False
        if repair_target is not None:
            copied = copy_verified(
                source=source,
                target=repair_target,
                library_root=repair_root,
                expected_checksum=checksum,
                expected_size=size_bytes,
                replace_existing_invalid=True,
            )
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    UPDATE media_files
                    SET local_path = ?, file_health = 'valid', verified_at = ?, updated_at = ?
                    WHERE library_entry_id = ?
                    """,
                    (
                        str(repair_target),
                        datetime.now(UTC).isoformat(),
                        datetime.now(UTC).isoformat(),
                        identity.library_entry_id,
                    ),
                )
        media_item = _upsert_local_source(
            db_path,
            source=source,
            identity=path_identity,
            detected=detected,
            checksum=checksum,
            source_timestamp=source_timestamp,
            status="downloaded" if status in {"existing", "repaired"} else "skipped",
        )
        attach = (
            assets.attach_media_item
            if status in {"existing", "repaired"}
            else assets.attach_media_item_to_inactive_asset
        )
        asset = attach(
            db_path,
            media_item_id=int(media_item["id"]),
            asset_id=identity.asset_id,
        )
        return _result(
            source=source,
            status=status,
            detected=detected,
            checksum=checksum,
            size_bytes=size_bytes,
            asset_id=asset["id"],
            target_path=identity.local_path if status in {"existing", "repaired"} else None,
            copied=copied,
        )

    if managed_root is not None:
        target = source
        target_root = managed_root
        status = "would_adopt" if dry_run else "adopted"
    else:
        target_root = library_root
        target = destination_path(
            library_root=target_root,
            source=source,
            checksum=checksum,
            detected=detected,
            source_timestamp=source_timestamp,
        )
        status = "would_import" if dry_run else "imported"
    if dry_run:
        return _result(
            source=source,
            status=status,
            detected=detected,
            checksum=checksum,
            size_bytes=size_bytes,
            target_path=str(target),
        )

    copied = False
    if target != source:
        copied = copy_verified(
            source=source,
            target=target,
            library_root=target_root,
            expected_checksum=checksum,
            expected_size=size_bytes,
        )
    media_item = _upsert_local_source(
        db_path,
        source=source,
        identity=path_identity,
        detected=detected,
        checksum=checksum,
        source_timestamp=source_timestamp,
        status="downloaded",
    )
    relative_path = target.relative_to(target_root).as_posix()
    file_record = db.upsert_media_file(
        db_path,
        platform="local",
        remote_id=str(media_item["remote_id"]),
        file_key=f"content:{checksum}",
        remote_url=None,
        local_path=str(target),
        mime_type=detected.mime_type,
        size_bytes=size_bytes,
        checksum=checksum,
        status="downloaded",
        library_relative_path=relative_path,
        storage_layout="local-import-v1",
        file_health="valid",
        source_timestamp=source_timestamp,
        verified_at=datetime.now(UTC).isoformat(),
    )
    adoption = library_content.adopt_media_file(db_path, file_id=int(file_record["id"]))
    if adoption.get("suppressed"):
        return _result(
            source=source,
            status="blocked",
            detected=detected,
            checksum=checksum,
            size_bytes=size_bytes,
            asset_id=str(adoption["asset_id"]),
            target_path=None,
            copied=False,
        )
    return _result(
        source=source,
        status=status,
        detected=detected,
        checksum=checksum,
        size_bytes=size_bytes,
        asset_id=str(adoption["asset_id"]),
        target_path=str(adoption.get("target_path") or target),
        copied=copied,
    )


def destination_path(
    *,
    library_root: Path,
    source: Path,
    checksum: str,
    detected: DetectedMedia,
    source_timestamp: str,
) -> Path:
    timestamp = datetime.fromisoformat(source_timestamp)
    checksum_value = checksum.removeprefix("sha256:")
    stem = safe_storage_segment(source.stem, max_length=80)
    filename = f"{checksum_value}__{stem}{detected.extension}"
    target = (
        library_root
        / "local"
        / detected.storage_category
        / f"{timestamp.year:04d}"
        / f"{timestamp.month:02d}"
        / filename
    ).resolve()
    ensure_inside(target, [library_root])
    return target


def copy_verified(
    *,
    source: Path,
    target: Path,
    library_root: Path,
    expected_checksum: str,
    expected_size: int,
    replace_existing_invalid: bool = False,
) -> bool:
    """Copy through a private partial and atomically publish verified bytes."""

    ensure_inside(target, [library_root])
    if target.is_symlink():
        raise PathSafetyError("Local import destination must not be a symbolic link.")
    if target.exists():
        checksum, size_bytes = library_content.sha256_checksum(target)
        if checksum == expected_checksum and size_bytes == expected_size:
            return False
        if not replace_existing_invalid:
            raise FileExistsError(f"Local import destination contains different content: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
    digest = hashlib.sha256()
    copied = 0
    try:
        with source.open("rb") as reader, partial.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        actual_checksum = f"sha256:{digest.hexdigest()}"
        if copied != expected_size or actual_checksum != expected_checksum:
            raise BrokenLocalMediaError("Source content changed while it was being copied.")
        os.replace(partial, target)
        shutil.copystat(source, target, follow_symlinks=False)
        return True
    finally:
        if partial.exists():
            partial.unlink()


def _upsert_local_source(
    db_path: Path,
    *,
    source: Path,
    identity: str,
    detected: DetectedMedia,
    checksum: str,
    source_timestamp: str,
    status: str,
) -> dict[str, Any]:
    metadata = {
        "title": source.name,
        "source_timestamp": source_timestamp,
        "storage_category": detected.storage_category,
        "local": {
            "original_path": str(source),
            "original_name": source.name,
            "checksum": checksum,
        },
    }
    if detected.storage_category == "comic":
        metadata["work_type"] = "comic"
    return db.upsert_media_item(
        db_path,
        {
            "platform": "local",
            "remote_id": identity,
            "source_url": None,
            "media_type": detected.media_type,
            "status": status,
            "source_availability": "available",
            "metadata": metadata,
        },
    )


def _identity_for_checksum(
    db_path: Path,
    checksum: str,
    representation_role: str,
) -> ExistingIdentity | None:
    try:
        with db.connect(db_path) as connection:
            row = connection.execute(
                """
                SELECT ar.asset_id, le.id AS library_entry_id, le.state,
                       le.local_path, le.library_relative_path,
                       cb.mime_type, cb.size_bytes, cb.checksum
                FROM content_blobs cb
                JOIN library_entries le ON le.content_blob_id = cb.id
                JOIN asset_representations ar ON ar.library_entry_id = le.id
                WHERE cb.checksum = ? AND ar.representation_role = ?
                  AND (
                      le.state = 'active'
                      OR NOT EXISTS (
                          SELECT 1 FROM library_entries active
                          WHERE active.content_blob_id = cb.id
                            AND active.state = 'active'
                      )
                  )
                ORDER BY CASE le.state WHEN 'active' THEN 0 ELSE 1 END, le.created_at, le.id
                LIMIT 1
                """,
                (checksum, representation_role),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT ar.asset_id, le.id AS library_entry_id, le.state,
                           le.local_path, le.library_relative_path,
                           cb.mime_type, cb.size_bytes, cb.checksum
                    FROM content_blobs cb
                    JOIN library_entries le ON le.content_blob_id = cb.id
                    JOIN asset_representations ar ON ar.library_entry_id = le.id
                    WHERE cb.checksum = ?
                      AND le.state IN ('removed', 'purged')
                      AND NOT EXISTS (
                          SELECT 1 FROM library_entries active
                          WHERE active.content_blob_id = cb.id
                            AND active.state = 'active'
                      )
                    ORDER BY CASE le.state WHEN 'purged' THEN 0 ELSE 1 END,
                             le.created_at, le.id
                    LIMIT 1
                    """,
                    (checksum,),
                ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return _existing_identity(row)


def _identity_for_path_entry(db_path: Path, path: Path) -> ExistingIdentity | None:
    try:
        with db.connect(db_path) as connection:
            row = connection.execute(
                """
                SELECT ar.asset_id, le.id AS library_entry_id, le.state,
                       le.local_path, le.library_relative_path,
                       cb.mime_type, cb.size_bytes, cb.checksum
                FROM library_entries le
                JOIN content_blobs cb ON cb.id = le.content_blob_id
                JOIN asset_representations ar ON ar.library_entry_id = le.id
                WHERE le.local_path = ?
                LIMIT 1
                """,
                (str(path),),
            ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    return _existing_identity(row)


def _existing_identity(row: Any) -> ExistingIdentity | None:
    if row is None:
        return None
    return ExistingIdentity(
        asset_id=str(row["asset_id"]),
        library_entry_id=str(row["library_entry_id"]),
        state=str(row["state"]),
        local_path=str(row["local_path"]),
        library_relative_path=row["library_relative_path"],
        mime_type=row["mime_type"],
        size_bytes=int(row["size_bytes"]),
        checksum=str(row["checksum"]),
    )


def _result(
    *,
    source: Path,
    status: str,
    detected: DetectedMedia,
    checksum: str,
    size_bytes: int,
    asset_id: str | None = None,
    target_path: str | None = None,
    copied: bool | None = None,
) -> dict[str, Any]:
    return {
        "source_path": str(source),
        "status": status,
        "asset_id": asset_id,
        "target_path": target_path,
        "media_type": "comic" if detected.storage_category == "comic" else detected.media_type,
        "mime_type": detected.mime_type,
        "checksum": checksum,
        "size_bytes": size_bytes,
        "copied": copied,
    }


def _detect_cbz(path: Path) -> DetectedMedia:
    try:
        with ZipFile(path) as archive:
            corrupt = archive.testzip()
            image_members = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and Path(name).suffix.lower() in IMAGE_ARCHIVE_SUFFIXES
            ]
    except (BadZipFile, OSError) as exc:
        raise BrokenLocalMediaError("CBZ archive is unreadable or invalid.") from exc
    if corrupt is not None:
        raise BrokenLocalMediaError("CBZ archive contains corrupt content.")
    if not image_members:
        raise BrokenLocalMediaError("CBZ archive does not contain image pages.")
    return DetectedMedia(
        "photo",
        "comic",
        "application/vnd.comicbook+zip",
        ".cbz",
        "comic_archive",
    )


def _is_audio(header: bytes, suffix: str) -> bool:
    return (
        header.startswith((b"ID3", b"fLaC"))
        or header.startswith(b"OggS") and suffix != ".ogv"
        or header.startswith(b"RIFF") and header[8:12] == b"WAVE"
        or len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        or header[4:8] == b"ftyp" and suffix in {".m4a", ".m4b"}
    )


def _is_video(header: bytes, suffix: str) -> bool:
    return (
        header[4:8] == b"ftyp" and suffix not in {".m4a", ".m4b"}
        or header.startswith(b"\x1aE\xdf\xa3")
        or header.startswith(b"RIFF") and header[8:12] == b"AVI "
        or header.startswith(b"OggS") and suffix == ".ogv"
        or header.startswith(b"\x00\x00\x01\xba")
    )


def _audio_mime(header: bytes, suffix: str) -> str:
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if header.startswith(b"RIFF"):
        return "audio/wav"
    if suffix in {".m4a", ".m4b"}:
        return "audio/mp4"
    return "audio/mpeg"


def _video_mime(header: bytes, suffix: str) -> str:
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm" if suffix == ".webm" else "video/x-matroska"
    if header.startswith(b"RIFF"):
        return "video/x-msvideo"
    if suffix == ".mov":
        return "video/quicktime"
    return "video/mp4"


def _preferred_extension(path: Path, mime_type: str) -> str:
    suffix = path.suffix.lower()
    if suffix in EXPECTED_MEDIA_SUFFIXES:
        return normalize_extension(suffix)
    return extension_from_mime(mime_type) or ".bin"


def _identity_for_path(path: Path, checksum: str) -> str:
    path_digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:24]
    return f"file:{path_digest}:{checksum.removeprefix('sha256:')}"


def _unique_roots(values: Iterable[Path]) -> list[Path]:
    return list({str(value.expanduser().resolve()): value.expanduser().resolve() for value in values}.values())


def _containing_root(path: Path, roots: Iterable[Path]) -> Path | None:
    matches = [root for root in roots if _inside(path, root)]
    return max(matches, key=lambda value: len(value.parts)) if matches else None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False
