"""Normalize Reddit saved listings into Mediagent media items."""

from __future__ import annotations

import html
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
URL_RE = re.compile(r"https?://[^\s<>\"]+")


def parse_saved_listing(
    payload: dict[str, Any],
    *,
    media_types: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    allowed_types = set(media_types or ["photo", "video", "audio"])
    children = payload.get("data", {}).get("children", [])
    summary = {
        "entries": len(children),
        "items": 0,
        "skipped_comments": 0,
        "unsupported_media": 0,
        "skipped_media_type": 0,
        "unsupported_entries": 0,
    }
    items: list[dict[str, Any]] = []

    for child in children:
        kind = child.get("kind")
        data = child.get("data", {})
        if kind == "t3":
            item = _submission_item(data, kind=kind)
            if item is None:
                summary["unsupported_media"] += 1
                continue
        elif kind == "t1":
            item = _comment_item(data, kind=kind)
            if item is None:
                summary["skipped_comments"] += 1
                continue
        else:
            summary["unsupported_entries"] += 1
            continue

        files = [
            file_info
            for file_info in item["metadata"]["files"]
            if file_info.get("media_type") in allowed_types
        ]
        if not files:
            summary["skipped_media_type"] += 1
            continue
        item["metadata"]["files"] = files
        item["media_type"] = _item_media_type(files)
        items.append(item)

    summary["items"] = len(items)
    summary["next_after"] = payload.get("data", {}).get("after")
    summary["before"] = payload.get("data", {}).get("before")
    return items, summary


def _submission_item(data: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    files = _submission_files(data)
    if not files:
        return None
    return _media_item(data, kind=kind, source_kind="submission", files=files)


def _comment_item(data: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    files = _comment_files(data)
    if not files:
        return None
    return _media_item(data, kind=kind, source_kind="comment", files=files)


def _media_item(
    data: dict[str, Any],
    *,
    kind: str,
    source_kind: str,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    remote_id = _fullname(data, kind=kind)
    return {
        "platform": "reddit",
        "remote_id": remote_id,
        "source_url": _source_url(data),
        "author_id": data.get("author_fullname"),
        "author_name": data.get("author"),
        "media_type": _item_media_type(files),
        "metadata": {
            "reddit": {
                "source_kind": source_kind,
                "name": remote_id,
                "id": data.get("id"),
                "subreddit": data.get("subreddit"),
                "subreddit_id": data.get("subreddit_id"),
                "title": data.get("title"),
                "permalink": data.get("permalink"),
                "author": data.get("author"),
                "author_fullname": data.get("author_fullname"),
                "created_utc": data.get("created_utc"),
                "over_18": bool(data.get("over_18")),
            },
            "files": files,
        },
    }


def _submission_files(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("is_gallery"):
        return _gallery_files(data)

    reddit_video = _reddit_video(data)
    if reddit_video:
        return [reddit_video]

    url = data.get("url_overridden_by_dest") or data.get("url")
    if isinstance(url, str):
        direct_file = _direct_url_file(
            url,
            page=0,
            file_id=f"{_fullname(data, kind='t3')}-0",
        )
        if direct_file:
            return [direct_file]

    preview_url = (
        data.get("preview", {})
        .get("images", [{}])[0]
        .get("source", {})
        .get("url")
    )
    if isinstance(preview_url, str) and data.get("post_hint") == "image":
        direct_file = _direct_url_file(
            html.unescape(preview_url),
            page=0,
            file_id=f"{_fullname(data, kind='t3')}-preview",
        )
        if direct_file:
            return [direct_file]
    return []


def _gallery_files(data: dict[str, Any]) -> list[dict[str, Any]]:
    media_metadata = data.get("media_metadata", {})
    gallery_items = data.get("gallery_data", {}).get("items", [])
    files: list[dict[str, Any]] = []
    for index, item in enumerate(gallery_items):
        media_id = item.get("media_id") or item.get("id")
        metadata = media_metadata.get(media_id, {})
        if metadata.get("status") not in (None, "valid"):
            continue
        mime_type = metadata.get("m")
        if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
            continue
        source = metadata.get("s", {})
        url = source.get("u")
        if not isinstance(url, str):
            previews = metadata.get("p", [])
            if previews:
                url = previews[-1].get("u")
        if not isinstance(url, str):
            continue
        files.append(
            {
                "id": media_id,
                "page": index,
                "kind": "photo",
                "media_type": "photo",
                "url": html.unescape(url),
                "mime_type": mime_type,
                "width": source.get("x"),
                "height": source.get("y"),
            }
        )
    return files


def _reddit_video(data: dict[str, Any]) -> dict[str, Any] | None:
    media = data.get("secure_media") or data.get("media") or {}
    reddit_video = media.get("reddit_video") if isinstance(media, dict) else None
    if not isinstance(reddit_video, dict):
        return None
    url = reddit_video.get("fallback_url")
    if not isinstance(url, str):
        return None
    return {
        "id": f"{_fullname(data, kind='t3')}-video-0",
        "page": 0,
        "kind": "video",
        "media_type": "video",
        "url": html.unescape(url),
        "mime_type": "video/mp4",
        "width": reddit_video.get("width"),
        "height": reddit_video.get("height"),
        "duration_seconds": reddit_video.get("duration"),
        "is_gif": bool(reddit_video.get("is_gif")),
    }


def _comment_files(data: dict[str, Any]) -> list[dict[str, Any]]:
    body = data.get("body") or data.get("body_html") or ""
    if not isinstance(body, str):
        return []
    for match in URL_RE.finditer(html.unescape(body)):
        url = match.group(0).rstrip(").,]")
        direct_file = _direct_url_file(
            url,
            page=0,
            file_id=f"{_fullname(data, kind='t1')}-0",
        )
        if direct_file:
            return [direct_file]
    return []


def _direct_url_file(url: str, *, page: int, file_id: str) -> dict[str, Any] | None:
    clean_url = html.unescape(url)
    parsed = urlparse(clean_url)
    extension = Path(parsed.path).suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        media_type = "photo"
    elif extension in VIDEO_EXTENSIONS:
        media_type = "video"
    else:
        return None
    mime_type = mimetypes.guess_type(parsed.path)[0]
    return {
        "id": file_id,
        "page": page,
        "kind": media_type,
        "media_type": media_type,
        "url": clean_url,
        "mime_type": mime_type,
    }


def _item_media_type(files: list[dict[str, Any]]) -> str:
    if any(file_info.get("media_type") == "video" for file_info in files):
        return "video"
    if any(file_info.get("media_type") == "audio" for file_info in files):
        return "audio"
    return "photo"


def _fullname(data: dict[str, Any], *, kind: str) -> str:
    name = data.get("name")
    if isinstance(name, str) and name:
        return name
    item_id = data.get("id")
    return f"{kind}_{item_id}" if item_id else kind


def _source_url(data: dict[str, Any]) -> str | None:
    permalink = data.get("permalink")
    if isinstance(permalink, str) and permalink:
        if permalink.startswith("http://") or permalink.startswith("https://"):
            return permalink
        return f"https://www.reddit.com{permalink}"
    url = data.get("url")
    return str(url) if url else None

