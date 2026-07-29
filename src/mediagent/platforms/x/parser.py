"""Normalize X API responses into Mediagent media items."""

from __future__ import annotations

from typing import Any


def parse_bookmarks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    media_by_key = {
        media["media_key"]: media
        for media in payload.get("includes", {}).get("media", [])
        if media.get("media_key")
    }
    users_by_id = {
        user["id"]: user
        for user in payload.get("includes", {}).get("users", [])
        if user.get("id")
    }
    items: list[dict[str, Any]] = []
    for post in payload.get("data", []):
        media_keys = post.get("attachments", {}).get("media_keys", [])
        author = users_by_id.get(post.get("author_id"), {})
        files = []
        media_type = None
        for media_key in media_keys:
            media = media_by_key.get(media_key)
            if not media:
                continue
            file_entry = _media_file(media)
            if file_entry:
                files.append(file_entry)
                media_type = media_type or _normalize_media_type(media.get("type"))
        if not files:
            continue
        items.append(
            {
                "platform": "x",
                "remote_id": post["id"],
                "source_url": _source_url(author.get("username"), post["id"]),
                "author_id": post.get("author_id"),
                "author_name": author.get("username") or author.get("name"),
                "media_type": media_type or "photo",
                "metadata": {
                    "post": post,
                    "author": author,
                    "files": files,
                },
            }
        )
    return items


def _media_file(media: dict[str, Any]) -> dict[str, Any] | None:
    media_type = media.get("type")
    if media_type == "photo":
        url = media.get("url")
    elif media_type in ("video", "animated_gif"):
        url = _best_variant_url(media.get("variants", []))
    else:
        url = media.get("url") or media.get("preview_image_url")
    if not url:
        return None
    return {
        "media_key": media.get("media_key"),
        "media_type": _normalize_media_type(media_type),
        "url": url,
        "mime_type": _best_variant_mime(media.get("variants", [])) if media_type != "photo" else None,
        "width": media.get("width"),
        "height": media.get("height"),
        "duration_ms": media.get("duration_ms"),
    }


def _best_variant_url(variants: list[dict[str, Any]]) -> str | None:
    mp4_variants = [
        variant
        for variant in variants
        if variant.get("url") and variant.get("content_type") == "video/mp4"
    ]
    if mp4_variants:
        return max(mp4_variants, key=lambda item: item.get("bit_rate", 0)).get("url")
    for variant in variants:
        if variant.get("url"):
            return variant["url"]
    return None


def _best_variant_mime(variants: list[dict[str, Any]]) -> str | None:
    for variant in variants:
        if variant.get("content_type") == "video/mp4":
            return "video/mp4"
    return variants[0].get("content_type") if variants else None


def _normalize_media_type(value: str | None) -> str:
    if value in ("video", "animated_gif"):
        return "video"
    return "photo"


def _source_url(username: str | None, post_id: str) -> str:
    if username:
        return f"https://x.com/{username}/status/{post_id}"
    return f"https://x.com/i/web/status/{post_id}"
