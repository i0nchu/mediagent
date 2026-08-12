"""Deterministic, atomic CBZ packaging helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from mediagent.core.filesystem import ensure_inside
from mediagent.core.storage import safe_storage_segment, source_datetime


CBZ_MIME_TYPE = "application/vnd.comicbook+zip"
CBZ_STORAGE_LAYOUT = "comic-cbz-v1"


def comic_archive_relative_path(
    *,
    item: dict[str, Any],
    include_platform_layer: bool,
) -> Path:
    source_dt, _ = source_datetime(item, {})
    platform = safe_storage_segment(item.get("platform") or "unknown-platform")
    remote_id = safe_storage_segment(item.get("remote_id") or "unknown-id", max_length=64)
    yyyy = f"{source_dt.year:04d}"
    mm = f"{source_dt.month:02d}"
    yyyymmdd = f"{source_dt.year:04d}{source_dt.month:02d}{source_dt.day:02d}"
    filename = f"{yyyymmdd}__{platform}__{remote_id}.cbz"
    if include_platform_layer:
        return Path(platform) / "comic" / yyyy / mm / filename
    return Path("comic") / yyyy / mm / filename


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
    series = pixiv_metadata.get("series") if isinstance(pixiv_metadata.get("series"), dict) else {}
    root = ElementTree.Element("ComicInfo")
    _xml_text(root, "Title", pixiv_metadata.get("title") or metadata.get("title") or item.get("remote_id"))
    _xml_text(root, "Series", series.get("title") or pixiv_metadata.get("series_title") or "Pixiv")
    _xml_text(root, "Number", series.get("order") or item.get("remote_id"))
    _xml_text(root, "Writer", item.get("author_name") or pixiv_metadata.get("author"))
    _xml_text(root, "Web", item.get("source_url"))
    _xml_text(root, "PageCount", page_count)
    _xml_text(root, "Manga", "Yes")
    tags = pixiv_metadata.get("tags") or metadata.get("tags") or []
    tag_names = [str(tag.get("name")) for tag in tags if isinstance(tag, dict) and tag.get("name")]
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
