"""Normalize nhentai gallery API payloads into comic manifests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

from mediagent.platforms.nhentai.links import canonical_gallery_url


IMAGE_CDN_BASE = "https://i.nhentai.net"
ALLOWED_IMAGE_HOSTS = {"i.nhentai.net"}
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def parse_gallery(payload: dict[str, Any]) -> dict[str, Any]:
    """Return one provider-neutral comic item with an ordered page manifest."""

    gallery_id = _positive_id(payload.get("id"), label="gallery")
    work_id = f"gallery:{gallery_id}"
    titles = payload.get("title") if isinstance(payload.get("title"), dict) else {}
    title = _first_text(titles.get("pretty"), titles.get("english"), titles.get("japanese"))
    if not title:
        title = f"nhentai {gallery_id}"

    tags = normalize_tags(payload.get("tags"))
    pages = normalize_pages(payload.get("pages"), gallery_id=gallery_id)
    declared_page_count = _optional_int(payload.get("num_pages"))
    source_url = canonical_gallery_url(gallery_id)
    uploaded_at = _timestamp(payload.get("upload_date"))
    artists = [tag["name"] for tag in tags if tag["type"] == "artist" and tag.get("name")]
    languages = [tag["name"] for tag in tags if tag["type"] == "language" and tag.get("name")]

    comic = {
        "provider": "nhentai",
        "provider_work_id": work_id,
        "title": title,
        "series_id": None,
        "series_title": None,
        "chapter_number": None,
        "volume_number": None,
        "total_count": 1,
        "is_one_shot": True,
        "summary": None,
    }
    metadata = {
        "title": title,
        "titles": {
            key: value
            for key, value in (
                ("pretty", _text(titles.get("pretty"))),
                ("english", _text(titles.get("english"))),
                ("japanese", _text(titles.get("japanese"))),
            )
            if value
        },
        "work_type": "comic",
        "storage_category": "comic-pages",
        # Keep the provider's declared count authoritative so packaging can
        # refuse a CBZ when a malformed or missing page was skipped.
        "page_count": declared_page_count if declared_page_count is not None else len(pages),
        "manifest_page_count": len(pages),
        "upload_date": uploaded_at,
        "scanlator": _text(payload.get("scanlator")),
        "languages": languages,
        "tags": tags,
        "comic": comic,
        "files": pages,
    }
    cover = normalize_cover(payload.get("cover"))
    if cover:
        metadata["cover"] = cover

    return {
        "platform": "nhentai",
        "remote_id": work_id,
        "media_type": "photo",
        "source_url": source_url,
        "author_id": None,
        "author_name": ", ".join(artists) or None,
        "source_timestamp": uploaded_at,
        "source_availability": "available",
        "status": "discovered",
        "metadata": metadata,
    }


def normalize_pages(value: Any, *, gallery_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    pages: list[dict[str, Any]] = []
    for fallback_index, page in enumerate(value):
        if not isinstance(page, dict):
            continue
        page_number = _optional_int(page.get("number")) or fallback_index + 1
        if page_number <= 0:
            continue
        url = image_url(page.get("path"))
        if not url:
            continue
        suffix = PurePosixPath(urlparse(url).path).suffix.lower()
        pages.append(
            {
                "url": url,
                "stable_url": canonical_gallery_url(gallery_id) + str(page_number) + "/",
                "kind": "image",
                "page": page_number - 1,
                "page_number": page_number,
                "storage_category": "comic-pages",
                "mime_type": MIME_BY_SUFFIX.get(suffix, "application/octet-stream"),
                "extension": suffix if suffix in MIME_BY_SUFFIX else ".bin",
                "width": _optional_int(page.get("width")),
                "height": _optional_int(page.get("height")),
            }
        )
    pages.sort(key=lambda page: page["page_number"])
    return pages


def normalize_cover(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    url = image_url(value.get("path"))
    if not url:
        return None
    return {
        "url": url,
        "width": _optional_int(value.get("width")),
        "height": _optional_int(value.get("height")),
    }


def normalize_tags(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for tag in value:
        if not isinstance(tag, dict):
            continue
        name = _text(tag.get("name"))
        tag_type = _text(tag.get("type"))
        if not name or not tag_type:
            continue
        result.append(
            {
                "id": str(tag["id"]) if tag.get("id") is not None else None,
                "type": tag_type,
                "name": name,
                "slug": _text(tag.get("slug")),
            }
        )
    return result


def image_url(value: Any) -> str | None:
    path = _text(value)
    if not path:
        return None
    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_IMAGE_HOSTS:
            return None
        return path
    pure = PurePosixPath(path.lstrip("/"))
    if ".." in pure.parts or not pure.parts:
        return None
    return f"{IMAGE_CDN_BASE}/{'/'.join(pure.parts)}"


def favorite_gallery_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract gallery mappings from known favorites response envelopes."""

    candidate: Any = payload
    if isinstance(candidate.get("data"), dict):
        candidate = candidate["data"]
    for key in ("result", "galleries", "items", "favorites"):
        value = candidate.get(key) if isinstance(candidate, dict) else None
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def favorite_targets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return stable exact-download targets from one favorites response page."""

    targets = []
    for gallery in favorite_gallery_payloads(payload):
        try:
            gallery_id = _positive_id(gallery.get("id"), label="gallery")
        except ValueError:
            continue
        targets.append(
            {
                "platform": "nhentai",
                "entity_type": "gallery",
                "provider_work_id": f"gallery:{gallery_id}",
                "source_url": canonical_gallery_url(gallery_id),
                "policy": "exact",
            }
        )
    return targets


def _positive_id(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"nhentai {label} ID must be a positive integer")
    return str(int(text))


def _timestamp(value: Any) -> str | None:
    seconds = _optional_int(value)
    if seconds is None or seconds < 0:
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return text
    return None
