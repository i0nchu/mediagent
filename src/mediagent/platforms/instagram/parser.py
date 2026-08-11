"""Normalize Instagram saved posts without persisting runtime download URLs."""

from __future__ import annotations

from typing import Any

from mediagent.core.links import resolution_to_media_item
from mediagent.platforms.instagram import links


def parse_saved_posts(posts: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for value in posts:
        post = _post(value)
        resolution = _resolution(post)
        item = resolution_to_media_item(resolution)
        if item is not None:
            items.append(item)
    return items


def _post(value: Any) -> links.InstagramPost:
    if isinstance(value, dict):
        shortcode = str(value.get("shortcode") or value.get("code") or "")
        kind = str(value.get("kind") or ("reel" if value.get("product_type") == "clips" else "p"))
        url = links.instagram_canonical_url(shortcode, kind=kind)
        return links._post_from_mapping(value, url=url, shortcode=shortcode, kind=kind)
    shortcode = str(getattr(value, "code", "") or "")
    kind = "reel" if getattr(value, "product_type", None) == "clips" else "p"
    url = links.instagram_canonical_url(shortcode, kind=kind)
    return links._post_from_instagrapi_media(value, url=url, shortcode=shortcode, kind=kind)


def _resolution(post: links.InstagramPost) -> dict[str, Any]:
    candidates = []
    counts = {"photo": 0, "video": 0, "audio": 0}
    for resource in post.resources:
        prefix = {"photo": "p", "video": "v", "audio": "a"}.get(resource.media_type, "f")
        part = f"{prefix}{counts.get(resource.media_type, 0)}"
        counts[resource.media_type] = counts.get(resource.media_type, 0) + 1
        candidates.append({
            "url": resource.stable_url,
            "media_type": resource.media_type,
            "mime_type": resource.mime_type,
            "extension": resource.extension,
            "file_index": resource.index,
            "part": part,
            "content_identity": resource.content_identity,
            "group_id": post.shortcode,
            "required": True,
            "download_context": {"url": resource.download_url},
            "source_timestamp": resource.source_timestamp or post.source_timestamp,
        })
    return {
        "status": "resolved", "original_url": post.source_url, "normalized_url": post.canonical_url,
        "canonical_url": post.canonical_url, "source_url": post.canonical_url,
        "resolved_media_url": candidates[0]["url"], "resolver": "instagram_saved_feed",
        "origin_source": "instagram", "remote_id": post.shortcode, "media_type": post.media_type,
        "mime_type": candidates[0]["mime_type"], "extension": candidates[0]["extension"],
        "media_count": len(candidates), "media_candidates": candidates,
        "source_timestamp": post.source_timestamp,
        "details": {"instagram": post.metadata, "source_timestamp": post.source_timestamp},
    }
