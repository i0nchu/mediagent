"""Normalize Pixiv App API responses into Mediagent media items."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse


PIXIV_WORK_TYPES = {
    "illust": "illustration",
    "manga": "comic",
    "ugoira": "animation",
}
PIXIV_STORAGE_CATEGORIES = {
    "illustration": "photo",
    "comic": "comic-pages",
    "animation": "video",
}


def parse_bookmarks(
    payload: dict[str, Any],
    *,
    ugoira_metadata_by_id: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    metadata_by_id = ugoira_metadata_by_id or {}
    items = [
        parse_illust(illust, ugoira_metadata=metadata_by_id.get(str(illust.get("id"))))
        for illust in payload.get("illusts", [])
        if isinstance(illust, dict)
    ]
    return items, next_max_bookmark_id(payload.get("next_url"))


def parse_illust(
    illust: dict[str, Any],
    *,
    ugoira_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    remote_id = str(illust["id"])
    user = illust.get("user") if isinstance(illust.get("user"), dict) else {}
    pixiv_type = illust.get("type")
    work_type = pixiv_work_type(pixiv_type)
    storage_category = PIXIV_STORAGE_CATEGORIES[work_type]
    series = illust.get("series") if isinstance(illust.get("series"), dict) else None
    unavailable_reason = pixiv_unavailable_reason(illust)
    files = [] if unavailable_reason else _files_for_illust(illust, ugoira_metadata=ugoira_metadata)
    for file_info in files:
        file_info["storage_category"] = storage_category
    media_type = "video" if pixiv_type == "ugoira" else "photo"
    metadata = {
        "title": illust.get("title"),
        "caption": illust.get("caption"),
        "pixiv_type": pixiv_type,
        "work_type": work_type,
        "storage_category": storage_category,
        "create_date": illust.get("create_date"),
        "page_count": illust.get("page_count"),
        "width": illust.get("width"),
        "height": illust.get("height"),
        "sanity_level": illust.get("sanity_level"),
        "x_restrict": illust.get("x_restrict"),
        "total_bookmarks": illust.get("total_bookmarks"),
        "total_view": illust.get("total_view"),
        "visible": illust.get("visible"),
        "is_muted": illust.get("is_muted"),
        "tools": illust.get("tools", []),
        "tags": _tags(illust.get("tags", [])),
        "series": series,
        "files": files,
    }
    if work_type == "comic":
        metadata["comic"] = pixiv_comic_metadata(remote_id=remote_id, metadata=illust)
    if unavailable_reason:
        metadata["availability_reason"] = unavailable_reason
    if ugoira_metadata:
        metadata["ugoira_metadata"] = ugoira_metadata
    return {
        "platform": "pixiv",
        "remote_id": remote_id,
        "media_type": media_type,
        "source_url": f"https://www.pixiv.net/artworks/{remote_id}",
        "author_id": str(user.get("id")) if user.get("id") is not None else None,
        "author_name": user.get("name"),
        "source_availability": "unavailable" if unavailable_reason else "available",
        "status": "skipped" if unavailable_reason else "discovered",
        "metadata": metadata,
    }


def pixiv_work_type(pixiv_type: Any) -> str:
    return PIXIV_WORK_TYPES.get(str(pixiv_type or "").strip().lower(), "illustration")


def pixiv_comic_metadata(*, remote_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
    series = metadata.get("series") if isinstance(metadata.get("series"), dict) else None
    return {
        "provider": "pixiv",
        "provider_work_id": str(remote_id),
        "title": metadata.get("title"),
        "series_id": series.get("id") if series else None,
        "series_title": series.get("title") if series else None,
        "chapter_number": series.get("order") if series else None,
        "volume_number": series.get("volume") if series else None,
        "total_count": series.get("count") if series else None,
        "is_one_shot": not bool(series and series.get("title")),
        "summary": metadata.get("caption"),
    }


def pixiv_unavailable_reason(illust: dict[str, Any]) -> str | None:
    if illust.get("visible") is False:
        return "visible_false"
    urls = _candidate_image_urls(illust)
    if urls and all(is_pixiv_placeholder_url(url) for url in urls):
        return "placeholder_asset"
    return None


def is_pixiv_placeholder_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    basename = parsed.path.rsplit("/", 1)[-1].lower()
    return (parsed.hostname or "").lower() == "s.pximg.net" and basename.startswith("limit_")


def _candidate_image_urls(illust: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    pages = illust.get("meta_pages")
    if isinstance(pages, list):
        for page in pages:
            image_urls = page.get("image_urls") if isinstance(page, dict) else None
            if isinstance(image_urls, dict):
                urls.extend(str(url) for url in image_urls.values() if url)
    single_page = illust.get("meta_single_page")
    if isinstance(single_page, dict):
        urls.extend(str(url) for url in single_page.values() if url)
    image_urls = illust.get("image_urls")
    if isinstance(image_urls, dict):
        urls.extend(str(url) for url in image_urls.values() if url)
    return urls


def next_max_bookmark_id(next_url: str | None) -> str | None:
    if not next_url:
        return None
    values = parse_qs(urlparse(next_url).query).get("max_bookmark_id", [])
    return values[0] if values else None


def _files_for_illust(
    illust: dict[str, Any],
    *,
    ugoira_metadata: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if illust.get("type") == "ugoira" and ugoira_metadata:
        zip_urls = ugoira_metadata.get("ugoira_metadata", {}).get("zip_urls", {})
        original = zip_urls.get("medium") or zip_urls.get("original")
        if original:
            return [
                {
                    "url": original,
                    "kind": "ugoira_zip",
                    "page": 0,
                }
            ]

    pages = illust.get("meta_pages")
    if isinstance(pages, list) and pages:
        files = []
        for index, page in enumerate(pages):
            image_urls = page.get("image_urls", {}) if isinstance(page, dict) else {}
            url = image_urls.get("original") or image_urls.get("large") or image_urls.get("medium")
            if url:
                files.append({"url": url, "kind": "image", "page": index})
        return files

    single_page = illust.get("meta_single_page")
    if isinstance(single_page, dict):
        original = single_page.get("original_image_url")
        if original:
            return [{"url": original, "kind": "image", "page": 0}]

    image_urls = illust.get("image_urls", {})
    if isinstance(image_urls, dict):
        url = image_urls.get("large") or image_urls.get("medium") or image_urls.get("square_medium")
        if url:
            return [{"url": url, "kind": "image", "page": 0}]
    return []


def _tags(tags: list[Any]) -> list[dict[str, Any]]:
    normalized = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        translated = tag.get("translated_name")
        normalized.append(
            {
                "name": tag.get("name"),
                "translated_name": translated,
            }
        )
    return normalized
