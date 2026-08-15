"""Deterministic, atomic CBZ packaging helpers."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from mediagent.core.filesystem import ensure_inside
from mediagent.core.storage import safe_storage_segment, source_datetime


CBZ_MIME_TYPE = "application/vnd.comicbook+zip"
CBZ_STORAGE_LAYOUT = "comic-kavita-v2"
IGNORED_COMIC_SPACER_HEALTH = "ignored_spacer"


@dataclass(frozen=True)
class ComicDescriptor:
    title: str
    series: str
    series_sort: str
    number: str
    volume: str | None
    count: str | None
    format: str | None
    provider: str
    provider_work_id: str
    series_directory: str
    archive_filename: str
    is_one_shot: bool


def comic_descriptor(item: dict[str, Any]) -> ComicDescriptor:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    provider_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
    comic = metadata.get("comic") if isinstance(metadata.get("comic"), dict) else {}
    if not comic and isinstance(provider_metadata.get("comic"), dict):
        comic = provider_metadata["comic"]
    series_data = provider_metadata.get("series") if isinstance(provider_metadata.get("series"), dict) else {}
    provider = str(item.get("platform") or comic.get("provider") or "unknown")
    provider_label = provider.replace("_", " ").replace("-", " ").title()
    work_id = str(comic.get("provider_work_id") or item.get("remote_id") or "unknown")
    title = str(comic.get("title") or provider_metadata.get("title") or metadata.get("title") or work_id).strip()
    series_title = str(
        comic.get("series_title")
        or series_data.get("title")
        or provider_metadata.get("series_title")
        or ""
    ).strip()
    series_id = str(comic.get("series_id") or series_data.get("id") or "").strip()
    explicit_one_shot = comic.get("is_one_shot")
    is_one_shot = bool(explicit_one_shot) if explicit_one_shot is not None else not series_title
    if is_one_shot:
        series = f"{title} [{provider_label} {work_id}]"
        number = "1"
        count = "1"
        comic_format = str(comic.get("format") or "One-Shot")
        identity = f"{provider}-{work_id}"
        archive_title = title
    else:
        series_identity = series_id or safe_storage_segment(series_title, max_length=48)
        series = f"{series_title} [{provider_label} series {series_identity}]"
        number = str(
            comic.get("chapter_number")
            or comic.get("number")
            or series_data.get("order")
            or work_id
        )
        count_value = comic.get("total_count") or series_data.get("count")
        count = str(count_value) if count_value not in (None, "") else None
        comic_format = str(comic.get("format")) if comic.get("format") else None
        identity = f"{provider}-series-{series_identity}"
        archive_title = str(comic.get("archive_title") or f"{series_title} - c{number}")
    volume_value = comic.get("volume_number") or comic.get("volume") or series_data.get("volume")
    volume = str(volume_value) if volume_value not in (None, "") else None
    unique_suffix = safe_storage_segment(identity, max_length=64)
    directory_title = str(comic.get("directory_title") or (title if is_one_shot else series_title))
    series_directory = _safe_comic_name_with_identity(directory_title, unique_suffix, max_bytes=180)
    archive_identity = safe_storage_segment(f"{provider}-{work_id}", max_length=64)
    archive_filename = f"{_safe_comic_name_with_identity(archive_title, archive_identity, max_bytes=220)}.cbz"
    return ComicDescriptor(
        title=title,
        series=series,
        series_sort=str(comic.get("series_sort") or series),
        number=number,
        volume=volume,
        count=count,
        format=comic_format,
        provider=provider,
        provider_work_id=work_id,
        series_directory=series_directory,
        archive_filename=archive_filename,
        is_one_shot=is_one_shot,
    )


def comic_archive_relative_path(
    *,
    item: dict[str, Any],
    include_platform_layer: bool,
) -> Path:
    descriptor = comic_descriptor(item)
    platform = safe_storage_segment(item.get("platform") or "unknown-platform")
    if include_platform_layer:
        return Path(platform) / "comic" / descriptor.series_directory / descriptor.archive_filename
    return Path("comic") / descriptor.series_directory / descriptor.archive_filename


def build_cbz_atomic(
    *,
    target_path: Path,
    pages: list[Path],
    item: dict[str, Any],
    allowed_root: Path,
) -> dict[str, Any]:
    if not pages:
        raise ValueError("Comic package requires at least one page.")
    target = target_path.resolve()
    root = allowed_root.resolve()
    ensure_inside(target, [root])
    partial = target.with_name(target.name + ".partial")
    ensure_inside(partial, [root])
    for page in pages:
        ensure_inside(page.resolve(), [root])
        if not page.is_file():
            raise FileNotFoundError(str(page))

    target.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    width = max(3, len(str(len(pages))))
    try:
        with ZipFile(partial, mode="w", compression=ZIP_STORED, allowZip64=True) as archive:
            for index, page in enumerate(pages, start=1):
                extension = page.suffix.lower() or ".bin"
                archive_name = f"{index:0{width}d}{extension}"
                _write_file_entry(archive, archive_name, page)
            _write_bytes_entry(archive, "ComicInfo.xml", comic_info_xml(item=item, page_count=len(pages)))
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    size_bytes = target.stat().st_size
    return {
        "target_path": str(target),
        "size_bytes": size_bytes,
        "checksum": f"sha256:{_sha256(target)}",
        "mime_type": CBZ_MIME_TYPE,
        "pages": len(pages),
    }


def comic_info_xml(*, item: dict[str, Any], page_count: int) -> bytes:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    pixiv_metadata = metadata.get("pixiv") if isinstance(metadata.get("pixiv"), dict) else metadata
    comic = metadata.get("comic") if isinstance(metadata.get("comic"), dict) else {}
    if not comic and isinstance(pixiv_metadata.get("comic"), dict):
        comic = pixiv_metadata["comic"]
    descriptor = comic_descriptor(item)
    root = ElementTree.Element("ComicInfo")
    _xml_text(root, "Title", descriptor.title)
    _xml_text(root, "Series", descriptor.series)
    _xml_text(root, "SeriesSort", descriptor.series_sort)
    _xml_text(root, "Number", descriptor.number)
    _xml_text(root, "Volume", descriptor.volume)
    _xml_text(root, "Count", descriptor.count)
    _xml_text(root, "Format", descriptor.format)
    _xml_text(root, "Summary", comic.get("summary") or pixiv_metadata.get("caption") or metadata.get("summary"))
    _xml_text(root, "Writer", item.get("author_name") or pixiv_metadata.get("author"))
    _xml_text(root, "Publisher", comic.get("publisher") or descriptor.provider.replace("_", " ").title())
    _xml_text(root, "Web", item.get("source_url"))
    _xml_text(root, "PageCount", page_count)
    _xml_text(root, "Manga", "Yes")
    _xml_text(root, "LanguageISO", comic.get("language") or pixiv_metadata.get("language"))
    age_rating = comic.get("age_rating")
    if not age_rating and _positive_int(pixiv_metadata.get("x_restrict")):
        age_rating = "Adults Only 18+"
    _xml_text(root, "AgeRating", age_rating)
    tags = comic.get("tags") or pixiv_metadata.get("tags") or metadata.get("tags") or []
    tag_names = [
        str(tag.get("name")) if isinstance(tag, dict) else str(tag)
        for tag in tags
        if (isinstance(tag, dict) and tag.get("name")) or (isinstance(tag, str) and tag.strip())
    ]
    if tag_names:
        _xml_text(root, "Tags", ", ".join(tag_names))
    source_dt, date_source = source_datetime(item, {})
    if date_source == "source":
        _xml_text(root, "Year", source_dt.year)
        _xml_text(root, "Month", source_dt.month)
        _xml_text(root, "Day", source_dt.day)
    pages_element = ElementTree.SubElement(root, "Pages")
    for index in range(page_count):
        attributes = {"Image": str(index)}
        if index == 0:
            attributes["Type"] = "FrontCover"
        ElementTree.SubElement(pages_element, "Page", attributes)
    ElementTree.indent(root, space="  ")
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _write_file_entry(archive: ZipFile, archive_name: str, source: Path) -> None:
    info = _zip_info(archive_name)
    with source.open("rb") as input_stream, archive.open(info, mode="w", force_zip64=True) as output_stream:
        shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)


def _write_bytes_entry(archive: ZipFile, archive_name: str, content: bytes) -> None:
    archive.writestr(_zip_info(archive_name), content)


def _zip_info(name: str) -> ZipInfo:
    info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.external_attr = 0o100644 << 16
    return info


def _xml_text(parent: ElementTree.Element, name: str, value: Any) -> None:
    if value is None or str(value).strip() == "":
        return
    ElementTree.SubElement(parent, name).text = str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_comic_name(value: Any, *, fallback: str, max_bytes: int = 140) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    if not text:
        text = fallback
    return _truncate_utf8(text, max_bytes=max_bytes).rstrip(" .-") or fallback


def _safe_comic_name_with_identity(title: Any, identity: str, *, max_bytes: int) -> str:
    safe_identity = _safe_comic_name(identity, fallback="unknown", max_bytes=72)
    suffix = f" [{safe_identity}]"
    suffix_bytes = len(suffix.encode("utf-8"))
    safe_title = _safe_comic_name(
        title,
        fallback=safe_identity,
        max_bytes=max(1, max_bytes - suffix_bytes),
    )
    return f"{safe_title}{suffix}"


def _truncate_utf8(value: str, *, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _positive_int(value: Any) -> bool:
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False
