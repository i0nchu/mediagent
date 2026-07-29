"""Reddit public-link parsing helpers."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

from mediagent.core.storage import safe_storage_segment


REDDIT_PAGE_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "sh.reddit.com",
    "redd.it",
}
REDDIT_DIRECT_IMAGE_HOSTS = {"i.redd.it"}
REDDIT_DIRECT_VIDEO_HOSTS = {"v.redd.it"}
REDDIT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
REDDIT_VIDEO_EXTENSIONS = {".mp4"}
REDDIT_MEDIA_URL_RE = re.compile(r"https://(?:i|v)\.redd\.it/[^\s\"'<>]+", re.IGNORECASE)


@dataclass(frozen=True)
class RedditMediaExtraction:
    media_url: str | None
    media_type: str | None
    remote_id: str | None
    metadata: dict[str, Any]
    skip_reason: str | None = None
    details: dict[str, Any] | None = None


def is_reddit_page_host(host: str) -> bool:
    return host.lower() in REDDIT_PAGE_HOSTS


def is_reddit_direct_image_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    suffix = Path(parsed.path).suffix.lower()
    return host in REDDIT_DIRECT_IMAGE_HOSTS and suffix in REDDIT_IMAGE_EXTENSIONS


def is_reddit_direct_video_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    suffix = Path(parsed.path).suffix.lower()
    return host in REDDIT_DIRECT_VIDEO_HOSTS and suffix in REDDIT_VIDEO_EXTENSIONS


def direct_image_remote_id(url: str) -> str:
    stem = Path(urlparse(url).path).stem
    return safe_storage_segment(f"i_{stem}", max_length=80)


def direct_video_remote_id(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and Path(parts[0]).suffix.lower() not in REDDIT_VIDEO_EXTENSIONS:
        value = parts[0]
    else:
        value = Path(parsed.path).stem
    return safe_storage_segment(f"v_{value}", max_length=80)


def reddit_video_audio_status(url: str) -> str:
    path = urlparse(url).path.lower()
    if "dash_" in path:
        return "not_merged"
    return "unknown"


def legacy_post_url(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if host == "old.reddit.com":
        return urlunparse(("https", "old.reddit.com", parsed.path or "/", "", "", ""))
    if host == "redd.it" and parts:
        return f"https://old.reddit.com/comments/{safe_storage_segment(parts[0], max_length=32)}/"
    if len(parts) >= 4 and parts[0] == "r" and parts[2] == "comments":
        subreddit = safe_storage_segment(parts[1], max_length=80)
        post_id = safe_storage_segment(parts[3], max_length=32)
        slug = safe_storage_segment(parts[4], max_length=120) if len(parts) >= 5 else ""
        path = f"/r/{subreddit}/comments/{post_id}/"
        if slug:
            path += f"{slug}/"
        return f"https://old.reddit.com{path}"
    if len(parts) >= 2 and parts[0] == "comments":
        post_id = safe_storage_segment(parts[1], max_length=32)
        slug = safe_storage_segment(parts[2], max_length=120) if len(parts) >= 3 else ""
        path = f"/comments/{post_id}/"
        if slug:
            path += f"{slug}/"
        return f"https://old.reddit.com{path}"
    return None


def extract_single_media_from_html(content: bytes, *, base_url: str) -> RedditMediaExtraction:
    text = content.decode("utf-8", errors="replace")
    parser = RedditMediaHTMLParser(base_url=base_url)
    parser.feed(text)
    parser.close()
    metadata = parser.metadata
    details = {
        "markers": reddit_html_markers(text),
        "candidate_count": len(parser.image_urls) + len(parser.video_urls),
        "image_candidate_count": len(parser.image_urls),
        "video_candidate_count": len(parser.video_urls),
    }
    if parser.gallery_hint:
        return RedditMediaExtraction(
            media_url=None,
            media_type=None,
            remote_id=parser.remote_id,
            metadata=metadata,
            skip_reason="unsupported_media_type",
            details={**details, "reason": "gallery_unsupported"},
        )
    if parser.video_urls:
        video_url = choose_primary_reddit_video_url(parser.video_urls)
        if video_url is None:
            return RedditMediaExtraction(
                media_url=None,
                media_type=None,
                remote_id=parser.remote_id,
                metadata=metadata,
                skip_reason="ambiguous",
                details={**details, "media_count": len(parser.video_urls), "reason": "ambiguous_video_candidates"},
            )
        video_metadata = {
            **metadata,
            "audio_status": reddit_video_audio_status(video_url),
            "mux_required": reddit_video_audio_status(video_url) == "not_merged",
        }
        return RedditMediaExtraction(
            media_url=video_url,
            media_type="video",
            remote_id=parser.remote_id or direct_video_remote_id(video_url),
            metadata=video_metadata,
            details={**details, "media_count": 1},
        )
    if parser.image_urls and parser.video_hint:
        return RedditMediaExtraction(
            media_url=None,
            media_type=None,
            remote_id=parser.remote_id,
            metadata=metadata,
            skip_reason="unsupported_media_type",
            details={**details, "reason": "video_manifest_unsupported"},
        )
    if parser.video_hint:
        return RedditMediaExtraction(
            media_url=None,
            media_type=None,
            remote_id=parser.remote_id,
            metadata=metadata,
            skip_reason="unsupported_media_type",
            details={**details, "reason": "video_manifest_unsupported"},
        )
    if len(parser.image_urls) > 1:
        return RedditMediaExtraction(
            media_url=None,
            media_type=None,
            remote_id=parser.remote_id,
            metadata=metadata,
            skip_reason="ambiguous",
            details={**details, "media_count": len(parser.image_urls)},
        )
    if not parser.image_urls:
        markers = details["markers"]
        if "network_security_blocked" in markers:
            skip_reason = "requires_auth"
            reason = "network_security_blocked"
        elif "login_required" in markers:
            skip_reason = "requires_auth"
            reason = "login_required"
        elif "javascript_verification" in markers:
            skip_reason = "requires_auth"
            reason = "javascript_verification"
        elif "over18_gate" in markers:
            skip_reason = "requires_auth"
            reason = "over18_gate"
        else:
            skip_reason = "unsupported_media_type"
            reason = "no_supported_reddit_media"
        return RedditMediaExtraction(
            media_url=None,
            media_type=None,
            remote_id=parser.remote_id,
            metadata=metadata,
            skip_reason=skip_reason,
            details={**details, "reason": reason},
        )
    media_url = parser.image_urls[0]
    resolved_markers = [
        marker for marker in details["markers"] if marker not in {"login_required", "over18_gate"}
    ]
    return RedditMediaExtraction(
        media_url=media_url,
        media_type="photo",
        remote_id=parser.remote_id or direct_image_remote_id(media_url),
        metadata=metadata,
        details={**details, "markers": resolved_markers, "media_count": 1},
    )


def extract_single_image_from_html(content: bytes, *, base_url: str) -> RedditMediaExtraction:
    return extract_single_media_from_html(content, base_url=base_url)


def choose_primary_reddit_video_url(urls: list[str]) -> str | None:
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    scored = [(reddit_video_url_score(url), url) for url in urls]
    top_score = max(score for score, _url in scored)
    top_urls = [url for score, url in scored if score == top_score]
    return top_urls[0] if len(top_urls) == 1 and top_score >= 0 else None


def reddit_video_url_score(url: str) -> int:
    path = urlparse(url).path.lower()
    if "audio" in path:
        return -1000
    match = re.search(r"dash_(\d+)", path)
    if match:
        return int(match.group(1))
    if path.endswith(".mp4"):
        return 1
    return -1


def reddit_html_markers(text: str) -> list[str]:
    lowered = text.lower()
    markers: list[str] = []
    if "please wait for verification" in lowered or "js_challenge" in lowered:
        markers.append("javascript_verification")
    if "blocked by network security" in lowered:
        markers.append("network_security_blocked")
    if "log in to reddit" in lowered or "login-required" in lowered:
        markers.append("login_required")
    if "you must be 18+" in lowered or "over18" in lowered and "continue" in lowered:
        markers.append("over18_gate")
    if "[deleted]" in lowered or "[removed]" in lowered:
        markers.append("deleted_or_removed")
    return markers


class RedditMediaHTMLParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.image_urls: list[str] = []
        self.video_urls: list[str] = []
        self._seen_image_urls: set[str] = set()
        self._seen_video_urls: set[str] = set()
        self.metadata: dict[str, Any] = {}
        self.remote_id: str | None = None
        self.gallery_hint = False
        self.video_hint = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        lower_tag = tag.lower()
        if lower_tag == "shreddit-screenview-data":
            self._capture_json_data(values.get("data"))
        if lower_tag == "shreddit-post":
            self._capture_shreddit_post(values)
        self._capture_old_reddit_thing(values)
        self._capture_media_hints(values)
        for key in ("content-href", "data-url", "href", "src"):
            self._add_media_url(values.get(key))

    def handle_data(self, data: str) -> None:
        if "i.redd.it" not in data and "v.redd.it" not in data:
            return
        for match in REDDIT_MEDIA_URL_RE.finditer(data.replace("\\/", "/")):
            self._add_media_url(match.group(0))

    def _capture_json_data(self, raw_data: str | None) -> None:
        if not raw_data:
            return
        try:
            payload = json.loads(html.unescape(raw_data))
        except json.JSONDecodeError:
            return
        self._scan_json(payload)

    def _scan_json(self, value: Any) -> None:
        if isinstance(value, dict):
            post = value.get("post")
            if isinstance(post, dict):
                self._capture_json_post(post)
            for key, item in value.items():
                if isinstance(item, str) and key.lower() in {
                    "content_url",
                    "contenthref",
                    "dashurl",
                    "fallback_url",
                    "fallbackurl",
                    "mediaurl",
                    "url",
                }:
                    self._add_media_url(item)
                    self._capture_unsupported_hints(item)
                else:
                    self._scan_json(item)
            return
        if isinstance(value, list):
            for item in value:
                self._scan_json(item)

    def _capture_json_post(self, post: dict[str, Any]) -> None:
        remote_id = post.get("id") or post.get("name")
        self._set_remote_id(remote_id)
        self._set_metadata("id", remote_id)
        self._set_metadata("title", post.get("title"))
        self._set_metadata("author", post.get("author") or post.get("authorName"))
        subreddit = post.get("subreddit")
        if isinstance(subreddit, dict):
            self._set_metadata("subreddit", subreddit.get("name") or subreddit.get("displayName"))
            self._set_metadata("subreddit_id", subreddit.get("id"))
        else:
            self._set_metadata("subreddit", subreddit)
        self._set_metadata("created_at", _iso_timestamp(post.get("created")) or _iso_timestamp(post.get("createdUtc")))
        post_type = str(post.get("postType") or post.get("post_type") or "").lower()
        if post_type in {"video", "hosted:video"} or post.get("is_video") is True:
            self.video_hint = True
        self._add_media_url(post.get("url"))
        self._capture_unsupported_hints(str(post.get("url") or ""))
        media = post.get("media")
        if isinstance(media, dict):
            reddit_video = media.get("reddit_video")
            if isinstance(reddit_video, dict):
                self.video_hint = True
                self._add_media_url(reddit_video.get("fallback_url"))

    def _capture_shreddit_post(self, values: dict[str, str]) -> None:
        self._set_remote_id(values.get("id"))
        self._set_metadata("id", values.get("id"))
        self._set_metadata("title", values.get("post-title") or values.get("title"))
        self._set_metadata("author", values.get("author"))
        self._set_metadata("subreddit", _strip_subreddit_prefix(values.get("subreddit-prefixed-name")))
        self._set_metadata("created_at", values.get("created-timestamp"))
        self._set_metadata("over_18", _bool_value(values.get("nsfw") or values.get("over-18")))
        post_type = (values.get("post-type") or "").lower()
        if post_type == "gallery":
            self.gallery_hint = True
        if post_type in {"video", "hosted:video"}:
            self.video_hint = True

    def _capture_old_reddit_thing(self, values: dict[str, str]) -> None:
        if "data-fullname" not in values and "data-url" not in values:
            return
        self._set_remote_id(values.get("data-fullname"))
        self._set_metadata("id", values.get("data-fullname"))
        self._set_metadata("subreddit", values.get("data-subreddit"))
        self._set_metadata("author", values.get("data-author"))
        self._set_metadata("permalink", values.get("data-permalink"))
        self._set_metadata("over_18", _bool_value(values.get("data-nsfw")))
        created_at = _iso_timestamp(values.get("data-timestamp"))
        self._set_metadata("created_at", created_at)
        domain = (values.get("data-domain") or "").lower()
        if domain == "v.redd.it":
            self.video_hint = True
        if domain == "reddit.com" and "/gallery/" in (values.get("data-url") or ""):
            self.gallery_hint = True

    def _capture_media_hints(self, values: dict[str, str]) -> None:
        if _bool_value(values.get("data-is-gallery")) is True:
            self.gallery_hint = True
        for key in ("content-href", "data-url", "href", "src"):
            self._capture_unsupported_hints(values.get(key) or "")

    def _capture_unsupported_hints(self, raw_url: str) -> None:
        if not raw_url:
            return
        parsed = urlparse(html.unescape(raw_url))
        host = (parsed.hostname or "").lower()
        if host in REDDIT_DIRECT_VIDEO_HOSTS:
            self.video_hint = True
        if parsed.path.startswith("/gallery/") or "/gallery/" in parsed.path:
            self.gallery_hint = True

    def _add_media_url(self, raw_url: str | None) -> None:
        if not raw_url:
            return
        resolved = urljoin(self.base_url, html.unescape(raw_url.strip()).replace("\\/", "/"))
        parsed = urlparse(resolved)
        host = (parsed.hostname or "").lower()
        suffix = Path(parsed.path).suffix.lower()
        if host in REDDIT_DIRECT_IMAGE_HOSTS and suffix in REDDIT_IMAGE_EXTENSIONS:
            normalized = urlunparse(("https", parsed.netloc.lower(), parsed.path, "", parsed.query, ""))
            if normalized in self._seen_image_urls:
                return
            self._seen_image_urls.add(normalized)
            self.image_urls.append(normalized)
            return
        if host != "v.redd.it":
            return
        self.video_hint = True
        if suffix not in REDDIT_VIDEO_EXTENSIONS:
            return
        normalized = urlunparse(("https", parsed.netloc.lower(), parsed.path, "", parsed.query, ""))
        if normalized in self._seen_video_urls:
            return
        self._seen_video_urls.add(normalized)
        self.video_urls.append(normalized)

    def _set_remote_id(self, value: Any) -> None:
        if self.remote_id:
            return
        if not isinstance(value, str) or not value.strip():
            return
        self.remote_id = safe_storage_segment(value.strip(), max_length=80)

    def _set_metadata(self, key: str, value: Any) -> None:
        if value is None or value == "":
            return
        if isinstance(value, str):
            value = html.unescape(value)
        self.metadata.setdefault(key, value)


def _strip_subreddit_prefix(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    return text[2:] if text.lower().startswith("r/") else text


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def _iso_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z") or "+" in text[-6:]:
            return text
        try:
            timestamp = float(text)
        except ValueError:
            return text
    else:
        return None
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None
