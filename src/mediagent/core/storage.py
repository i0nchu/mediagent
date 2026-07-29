"""Storage planning for scanner-friendly media libraries."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mediagent.core.filesystem import PathSafetyError, ensure_inside


LAYOUT_SCANNER_FRIENDLY_V1 = "scanner-friendly-v1"
LAYOUT_SCANNER_FRIENDLY_V2 = "scanner-friendly-v2"
SUPPORTED_MEDIA_TYPES = {"photo", "video", "audio"}
FILE_HEALTH_VALUES = {"valid", "missing", "corrupt", "unknown"}
SOURCE_AVAILABILITY_VALUES = {"available", "deleted", "restricted", "unavailable", "unknown"}
PART_PREFIX_BY_MEDIA_TYPE = {
    "photo": "p",
    "video": "v",
    "audio": "a",
}


@dataclass(frozen=True)
class StoragePlan:
    library_root: Path
    relative_path: Path
    final_path: Path
    partial_path: Path
    layout: str
    platform_layer_included: bool
    date_source: str
    source_timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "library_root": str(self.library_root),
            "relative_path": self.relative_path.as_posix(),
            "final_path": str(self.final_path),
            "partial_path": str(self.partial_path),
            "layout": self.layout,
            "platform_layer_included": self.platform_layer_included,
            "date_source": self.date_source,
            "source_timestamp": self.source_timestamp,
        }


def default_library_root(*, data_dir: Path | None, library_dir: Path | None) -> Path:
    if library_dir:
        return library_dir.expanduser().resolve()
    if data_dir:
        return (data_dir / "library").expanduser().resolve()
    raise PathSafetyError("Provide MEDIAGENT_LIBRARY_DIR or MEDIAGENT_DATA_DIR.")


def platform_library_env_name(platform: Any) -> str:
    segment = safe_storage_segment(platform, max_length=40).replace("-", "_").upper()
    return f"MEDIAGENT_{segment}_LIBRARY_DIR"


def plan_storage_path(
    *,
    library_root: Path,
    item: dict[str, Any],
    file_info: dict[str, Any],
    include_platform_layer: bool = True,
    now: datetime | None = None,
) -> StoragePlan:
    root = library_root.expanduser().resolve()
    media_type = str(item.get("media_type") or "").strip()
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise PathSafetyError(f"Unsupported media type: {media_type or '<empty>'}")

    platform = safe_storage_segment(item.get("platform") or "unknown-platform")
    remote_id = safe_storage_segment(item.get("remote_id") or "unknown-id")
    source_dt, date_source = source_datetime(item, file_info, now=now)
    yyyy = f"{source_dt.year:04d}"
    mm = f"{source_dt.month:02d}"
    yyyymmdd = f"{source_dt.year:04d}{source_dt.month:02d}{source_dt.day:02d}"
    part = file_part_key(item=item, file_info=file_info)
    extension = file_extension(file_info)
    filename = f"{yyyymmdd}__{platform}__{remote_id}__{part}{extension}"
    if include_platform_layer:
        relative_path = Path(platform) / media_type / yyyy / mm / filename
        layout = LAYOUT_SCANNER_FRIENDLY_V2
    else:
        relative_path = Path(media_type) / yyyy / mm / filename
        layout = LAYOUT_SCANNER_FRIENDLY_V1
    final_path = (root / relative_path).resolve()
    ensure_inside(final_path, [root])
    return StoragePlan(
        library_root=root,
        relative_path=relative_path,
        final_path=final_path,
        partial_path=final_path.with_name(final_path.name + ".partial"),
        layout=layout,
        platform_layer_included=include_platform_layer,
        date_source=date_source,
        source_timestamp=source_dt.isoformat(),
    )


def source_datetime(
    item: dict[str, Any],
    file_info: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[datetime, str]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    candidates = (
        file_info.get("source_timestamp"),
        item.get("source_timestamp"),
        metadata.get("source_timestamp"),
        metadata.get("create_date"),
        metadata.get("created_at"),
        metadata.get("published_at"),
        metadata.get("date"),
    )
    for candidate in candidates:
        parsed = parse_datetime(candidate)
        if parsed:
            return parsed, "source"
    fallback = now or datetime.now(UTC)
    if fallback.tzinfo is None:
        fallback = fallback.replace(tzinfo=UTC)
    return fallback, "fallback"


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def file_part_key(*, item: dict[str, Any], file_info: dict[str, Any]) -> str:
    explicit = file_info.get("part") or file_info.get("part_key") or file_info.get("file_key")
    if explicit:
        return safe_storage_segment(explicit, max_length=32)
    media_type = str(file_info.get("media_type") or item.get("media_type") or "photo")
    prefix = PART_PREFIX_BY_MEDIA_TYPE.get(media_type, "f")
    page = file_info.get("page", 0)
    try:
        index = int(page or 0)
    except (TypeError, ValueError):
        index = 0
    return f"{prefix}{index}"


def file_extension(file_info: dict[str, Any]) -> str:
    for key in ("extension", "ext"):
        value = file_info.get(key)
        if value:
            return normalize_extension(str(value))
    mime_type = file_info.get("mime_type") or file_info.get("content_type")
    if mime_type:
        guessed = extension_from_mime(str(mime_type))
        if guessed:
            return guessed
    url = str(file_info.get("url") or file_info.get("remote_url") or "")
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and 1 < len(suffix) <= 10:
        return normalize_extension(suffix)
    return ".bin"


def extension_from_mime(mime_type: str) -> str | None:
    clean = mime_type.split(";", 1)[0].strip().lower()
    if clean == "image/jpeg":
        return ".jpg"
    guessed = mimetypes.guess_extension(clean)
    if not guessed:
        return None
    return normalize_extension(guessed)


def normalize_extension(value: str) -> str:
    text = value.strip().lower()
    if not text:
        return ".bin"
    if not text.startswith("."):
        text = "." + text
    text = re.sub(r"[^a-z0-9.]+", "", text)
    if text in (".jpeg", ".jpe"):
        return ".jpg"
    if 1 < len(text) <= 10:
        return text
    return ".bin"


def safe_storage_segment(value: Any, *, max_length: int = 80) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text)
    text = text.strip("._- ")
    if not text:
        return "unknown"
    return text[:max_length]
