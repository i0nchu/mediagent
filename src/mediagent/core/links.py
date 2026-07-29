"""URL intake, safety, and resolver helpers for experimental link workflows."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from urllib.parse import ParseResult

from mediagent.core.http import HttpResponse, UrllibHttpClient
from mediagent.core.storage import extension_from_mime, safe_storage_segment
from mediagent.platforms.reddit import links as reddit_links


ALLOWED_MEDIA_MIME_TYPES = {
    "image/jpeg": "photo",
    "image/png": "photo",
    "image/webp": "photo",
    "image/gif": "photo",
    "video/mp4": "video",
    "video/webm": "video",
    "video/quicktime": "video",
}
ALLOWED_DIRECT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".mp4",
    ".webm",
    ".mov",
}
URL_PATTERN = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
TELEGRAM_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}
PIXIV_HOSTS = {"pixiv.net", "www.pixiv.net"}
IMGUR_HOSTS = {"imgur.com", "www.imgur.com"}
REDDIT_HOSTS = tuple(
    sorted(
        reddit_links.REDDIT_PAGE_HOSTS
        | reddit_links.REDDIT_DIRECT_IMAGE_HOSTS
        | reddit_links.REDDIT_DIRECT_VIDEO_HOSTS
    )
)
HTML_MIME_TYPES = {"text/html", "application/xhtml+xml"}
HTML_META_MEDIA_NAMES = {
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "twitter:image",
    "twitter:image:src",
    "twitter:player:stream",
}
HTML_MEDIA_VALUE_KEYS = {
    "contenturl",
    "embedurl",
    "imageurl",
    "mediaurl",
    "preview_image_url",
    "thumbnailurl",
    "url",
}
DECORATIVE_MEDIA_MARKERS = {
    "apple-touch-icon",
    "avatar",
    "favicon",
    "profile_images",
    "thumbnail_",
    "/thumbnail",
    "/thumbnails/",
    "/rweb/ssr/default/",
}
X_REQUIRES_LOGIN_MARKERS = (
    "age-restricted adult content",
    "to view this media",
    "log in to x",
)


@dataclass(frozen=True)
class LinkSafetyPolicy:
    max_redirects: int = 3
    timeout_seconds: float = 30.0
    max_html_bytes: int = 1024 * 1024
    max_media_bytes: int = 1024 * 1024 * 1024


@dataclass(frozen=True)
class SafeURL:
    original_url: str
    normalized_url: str
    host: str


@dataclass(frozen=True)
class ResolverSpec:
    name: str
    allowed_domains: tuple[str, ...]
    matching_rules: str
    supports_auth: bool = False
    max_media_items: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "allowed_domains": list(self.allowed_domains),
            "matching_rules": self.matching_rules,
            "supports_auth": self.supports_auth,
            "max_media_items": self.max_media_items,
        }


class URLSafetyError(ValueError):
    def __init__(self, reason: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


class Resolver:
    spec: ResolverSpec

    def matches(self, safe_url: SafeURL) -> bool:
        raise NotImplementedError

    def resolve(self, safe_url: SafeURL, request: "ResolveRequest") -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class ResolveRequest:
    http_client: Any | None = None
    policy: LinkSafetyPolicy = LinkSafetyPolicy()
    host_resolver: Callable[[str], list[str]] | None = None


class LinkResolverRegistry:
    def __init__(self, resolvers: list[Resolver]) -> None:
        self._resolvers = resolvers

    def specs(self) -> list[dict[str, Any]]:
        return [resolver.spec.to_dict() for resolver in self._resolvers]

    def resolve(self, raw_url: str, request: ResolveRequest | None = None) -> dict[str, Any]:
        request = request or ResolveRequest()
        try:
            safe_url = validate_url_safety(
                raw_url,
                host_resolver=request.host_resolver,
            )
        except URLSafetyError as exc:
            normalized = normalize_url(raw_url)
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=normalized,
                resolver=None,
                skip_reason="unsafe_url",
                details={"reason": exc.reason, **exc.details},
            )

        for resolver in self._resolvers:
            if not resolver.matches(safe_url):
                continue
            result = resolver.resolve(safe_url, request)
            if result.get("status") == "resolved":
                return result
            if result.get("details", {}).get("continue_resolving"):
                continue
            return result
        return skipped_resolution(
            original_url=raw_url,
            normalized_url=safe_url.normalized_url,
            resolver=None,
            skip_reason="unsupported_domain",
            details={"host": safe_url.host},
        )


class PixivArtworkLinkResolver(Resolver):
    spec = ResolverSpec(
        name="pixiv_artwork_link",
        allowed_domains=tuple(sorted(PIXIV_HOSTS)),
        matching_rules="Pixiv artwork URLs with /artworks/<id> or illust_id=<id>.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return safe_url.host in PIXIV_HOSTS and pixiv_artwork_id(safe_url.normalized_url) is not None

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        artwork_id = pixiv_artwork_id(safe_url.normalized_url)
        return skipped_resolution(
            original_url=safe_url.original_url,
            normalized_url=safe_url.normalized_url,
            resolver=self.spec.name,
            skip_reason="requires_auth",
            origin_source="pixiv",
            remote_id=artwork_id,
            details={
                "platform": "pixiv",
                "remote_id": artwork_id,
                "reason": "Pixiv artwork pages require platform credentials and an artwork-detail source tool.",
                "candidate_tool": "pixiv.bookmarks.sync",
            },
        )


class ImgurSingleResolver(Resolver):
    spec = ResolverSpec(
        name="imgur_single",
        allowed_domains=tuple(sorted(IMGUR_HOSTS)),
        matching_rules="Public Imgur single-image pages that expose exactly one Open Graph media URL.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        if safe_url.host not in IMGUR_HOSTS:
            return False
        path = urlparse(safe_url.normalized_url).path.strip("/")
        return bool(path) and not path.startswith(("a/", "gallery/"))

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        response, final_url = fetch_limited_follow_redirects(
            safe_url.normalized_url,
            request=request,
            max_bytes=request.policy.max_html_bytes,
        )
        if response.status_code in (401, 403):
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source="imgur",
                details={"status_code": response.status_code},
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="imgur",
                details={"status_code": response.status_code},
            )
        media_urls = html_media_urls(response.content, base_url=final_url)
        if len(media_urls) > 1:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="ambiguous",
                origin_source="imgur",
                details={"media_count": len(media_urls)},
            )
        if not media_urls:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="imgur",
                details={"media_count": 0},
            )
        direct = DirectMediaResolver().resolve_url(
            media_urls[0],
            request=request,
            source_url=safe_url.normalized_url,
            resolver_name=self.spec.name,
            origin_source="imgur",
            remote_id=imgur_remote_id(safe_url.normalized_url, media_urls[0]),
        )
        if direct.get("status") == "resolved":
            direct["normalized_url"] = safe_url.normalized_url
            direct["source_url"] = safe_url.normalized_url
            direct["details"] = {**direct.get("details", {}), "resolved_page_url": final_url}
        return direct


class RedditMediaLinkResolver(Resolver):
    spec = ResolverSpec(
        name="reddit_media_link",
        allowed_domains=REDDIT_HOSTS,
        matching_rules=(
            "Public Reddit post links, redd.it short links, direct i.redd.it image URLs, "
            "and direct v.redd.it MP4 URLs that resolve to exactly one supported media file."
        ),
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return safe_url.host in REDDIT_HOSTS

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        if reddit_links.is_reddit_direct_video_url(safe_url.normalized_url):
            if reddit_links.reddit_video_url_score(safe_url.normalized_url) < 0:
                return skipped_resolution(
                    original_url=safe_url.original_url,
                    normalized_url=safe_url.normalized_url,
                    resolver=self.spec.name,
                    skip_reason="unsupported_media_type",
                    origin_source="reddit",
                    details={"reason": "video_audio_track_unsupported", "host": safe_url.host},
                )
            return self._resolve_media_url(
                media_url=safe_url.normalized_url,
                source_url=safe_url.normalized_url,
                request=request,
                remote_id=reddit_links.direct_video_remote_id(safe_url.normalized_url),
                reddit_metadata={
                    "source_kind": "direct_video",
                    "audio_status": reddit_links.reddit_video_audio_status(safe_url.normalized_url),
                    "mux_required": reddit_links.reddit_video_audio_status(safe_url.normalized_url) == "not_merged",
                },
                details={"html_source": "direct_video"},
            )
        if safe_url.host in reddit_links.REDDIT_DIRECT_VIDEO_HOSTS:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="reddit",
                details={"reason": "video_manifest_unsupported", "host": safe_url.host},
            )
        if reddit_links.is_reddit_direct_image_url(safe_url.normalized_url):
            return self._resolve_media_url(
                media_url=safe_url.normalized_url,
                source_url=safe_url.normalized_url,
                request=request,
                remote_id=reddit_links.direct_image_remote_id(safe_url.normalized_url),
                reddit_metadata={"source_kind": "direct_image"},
                details={"html_source": "direct_image"},
            )

        try:
            response, final_url = fetch_limited_follow_redirects(
                safe_url.normalized_url,
                request=request,
                max_bytes=request.policy.max_html_bytes,
            )
        except URLSafetyError as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsafe_url",
                origin_source="reddit",
                details={"reason": exc.reason, **exc.details},
            )
        except Exception as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="reddit",
                details={"exception_type": type(exc).__name__},
            )

        final_host = (urlparse(final_url).hostname or "").lower()
        if reddit_links.is_reddit_direct_video_url(final_url):
            if reddit_links.reddit_video_url_score(final_url) < 0:
                return skipped_resolution(
                    original_url=safe_url.original_url,
                    normalized_url=safe_url.normalized_url,
                    resolver=self.spec.name,
                    skip_reason="unsupported_media_type",
                    origin_source="reddit",
                    details={"final_url": final_url, "reason": "video_audio_track_unsupported"},
                )
            return self._resolve_media_url(
                media_url=final_url,
                source_url=safe_url.normalized_url,
                request=request,
                remote_id=reddit_links.direct_video_remote_id(final_url),
                reddit_metadata={
                    "source_kind": "redirected_direct_video",
                    "audio_status": reddit_links.reddit_video_audio_status(final_url),
                    "mux_required": reddit_links.reddit_video_audio_status(final_url) == "not_merged",
                },
                details={"html_source": "redirect"},
            )
        if reddit_links.is_reddit_direct_image_url(final_url):
            return self._resolve_media_url(
                media_url=final_url,
                source_url=safe_url.normalized_url,
                request=request,
                remote_id=reddit_links.direct_image_remote_id(final_url),
                reddit_metadata={"source_kind": "redirected_direct_image"},
                details={"html_source": "redirect"},
            )
        if final_host not in reddit_links.REDDIT_PAGE_HOSTS:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_domain",
                origin_source="reddit",
                details={"final_url": final_url, "final_host": final_host},
            )

        content_type = clean_mime(header_value(response.headers, "content-type"))
        if response.status_code in (401, 403):
            extraction = reddit_links.extract_single_media_from_html(response.content, base_url=final_url)
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source="reddit",
                remote_id=extraction.remote_id,
                details={
                    "status_code": response.status_code,
                    "resolved_page_url": final_url,
                    **(extraction.details or {}),
                },
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="reddit",
                details={"status_code": response.status_code, "resolved_page_url": final_url},
            )
        if content_type not in HTML_MIME_TYPES:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="reddit",
                details={"content_type": content_type, "resolved_page_url": final_url},
            )

        extraction = reddit_links.extract_single_media_from_html(response.content, base_url=final_url)
        if extraction.media_url:
            return self._resolve_media_url(
                media_url=extraction.media_url,
                source_url=final_url,
                request=request,
                remote_id=extraction.remote_id,
                reddit_metadata={**extraction.metadata, "source_kind": "post"},
                details={
                    **(extraction.details or {}),
                    "html_source": "reddit",
                    "resolved_page_url": final_url,
                },
            )

        legacy_url = reddit_links.legacy_post_url(final_url)
        if legacy_url and legacy_url != final_url:
            legacy_result = self._resolve_from_legacy_page(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                legacy_url=legacy_url,
                request=request,
            )
            if legacy_result is not None:
                return legacy_result

        return skipped_resolution(
            original_url=safe_url.original_url,
            normalized_url=safe_url.normalized_url,
            resolver=self.spec.name,
            skip_reason=extraction.skip_reason or "unsupported_media_type",
            origin_source="reddit",
            remote_id=extraction.remote_id,
            details={"resolved_page_url": final_url, **(extraction.details or {})},
        )

    def _resolve_from_legacy_page(
        self,
        *,
        original_url: str,
        normalized_url: str,
        legacy_url: str,
        request: ResolveRequest,
    ) -> dict[str, Any] | None:
        try:
            response, final_url = fetch_limited_follow_redirects(
                legacy_url,
                request=request,
                max_bytes=request.policy.max_html_bytes,
                headers={"Cookie": "over18=1"},
            )
        except URLSafetyError as exc:
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason="unsafe_url",
                origin_source="reddit",
                details={"legacy_url": legacy_url, "reason": exc.reason, **exc.details},
            )
        except Exception as exc:
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="reddit",
                details={"legacy_url": legacy_url, "exception_type": type(exc).__name__},
            )
        if response.status_code in (401, 403):
            extraction = reddit_links.extract_single_media_from_html(response.content, base_url=final_url)
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source="reddit",
                remote_id=extraction.remote_id,
                details={
                    "status_code": response.status_code,
                    "legacy_url": legacy_url,
                    "resolved_page_url": final_url,
                    **(extraction.details or {}),
                },
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="reddit",
                details={
                    "status_code": response.status_code,
                    "legacy_url": legacy_url,
                    "resolved_page_url": final_url,
                },
            )
        extraction = reddit_links.extract_single_media_from_html(response.content, base_url=final_url)
        if not extraction.media_url:
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason=extraction.skip_reason or "unsupported_media_type",
                origin_source="reddit",
                remote_id=extraction.remote_id,
                details={
                    "legacy_url": legacy_url,
                    "resolved_page_url": final_url,
                    **(extraction.details or {}),
                },
            )
        return self._resolve_media_url(
            media_url=extraction.media_url,
            source_url=final_url,
            request=request,
            remote_id=extraction.remote_id,
            reddit_metadata={**extraction.metadata, "source_kind": "post"},
            details={
                **(extraction.details or {}),
                "html_source": "old_reddit",
                "legacy_url": legacy_url,
                "resolved_page_url": final_url,
            },
        )

    def _resolve_media_url(
        self,
        *,
        media_url: str,
        source_url: str,
        request: ResolveRequest,
        remote_id: str | None,
        reddit_metadata: dict[str, Any],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        direct = DirectMediaResolver().resolve_url(
            media_url,
            request=request,
            source_url=source_url,
            resolver_name=self.spec.name,
            origin_source="reddit",
            remote_id=remote_id,
        )
        if direct.get("status") == "resolved":
            source_timestamp = reddit_metadata.get("created_at")
            direct["source_url"] = source_url
            if direct.get("media_type") == "video" and reddit_metadata.get("audio_status") == "not_merged":
                direct.setdefault("warnings", []).append(
                    "Reddit video may be video-only; audio muxing is not implemented yet."
                )
            direct["details"] = {
                **direct.get("details", {}),
                **details,
                "reddit": reddit_metadata,
                "source_timestamp": source_timestamp,
            }
        return direct


class GenericHTMLMediaResolver(Resolver):
    spec = ResolverSpec(
        name="generic_html_media",
        allowed_domains=("*",),
        matching_rules="Public HTTPS HTML pages that expose exactly one safe primary media URL.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return True

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        try:
            response, final_url = fetch_limited_follow_redirects(
                safe_url.normalized_url,
                request=request,
                max_bytes=request.policy.max_html_bytes,
            )
        except URLSafetyError as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsafe_url",
                origin_source=origin_source_from_host(safe_url.host),
                details={"reason": exc.reason, **exc.details},
            )
        except Exception as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source=origin_source_from_host(safe_url.host),
                details={"exception_type": type(exc).__name__},
            )
        origin_source = origin_source_from_host(urlparse(final_url).hostname or safe_url.host)
        if response.status_code in (401, 403):
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source=origin_source,
                details={"status_code": response.status_code},
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source=origin_source,
                details={"status_code": response.status_code},
            )
        content_type = clean_mime(header_value(response.headers, "content-type"))
        if content_type not in HTML_MIME_TYPES:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source=origin_source,
                details={"content_type": content_type},
            )
        if is_x_login_wall(response.content):
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source=origin_source,
                details={"reason": "login_or_age_gate"},
            )
        media_urls = html_media_urls(response.content, base_url=final_url)
        media_url = choose_primary_html_media_url(media_urls)
        if len(media_urls) > 1 and media_url is None:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="ambiguous",
                origin_source=origin_source,
                details={"media_count": len(media_urls)},
            )
        if media_url is None:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source=origin_source,
                details={"media_count": 0},
            )
        direct = DirectMediaResolver().resolve_url(
            media_url,
            request=request,
            source_url=safe_url.normalized_url,
            resolver_name=self.spec.name,
            origin_source=origin_source,
            remote_id=normalized_url_hash(safe_url.normalized_url),
        )
        if direct.get("status") == "resolved":
            direct["normalized_url"] = safe_url.normalized_url
            direct["source_url"] = safe_url.normalized_url
            direct["details"] = {
                **direct.get("details", {}),
                "resolved_page_url": final_url,
                "html_media_candidates": len(media_urls),
            }
        return direct


class DirectMediaResolver(Resolver):
    spec = ResolverSpec(
        name="direct_media",
        allowed_domains=("*",),
        matching_rules="Safe HTTPS URLs that return one allowed image/video/GIF/MOV media response.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        path = urlparse(safe_url.normalized_url).path
        suffix = Path(path).suffix.lower()
        return suffix in ALLOWED_DIRECT_EXTENSIONS or True

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        return self.resolve_url(
            safe_url.normalized_url,
            request=request,
            source_url=safe_url.normalized_url,
            resolver_name=self.spec.name,
            origin_source=origin_source_from_host(safe_url.host),
            remote_id=None,
        )

    def resolve_url(
        self,
        raw_url: str,
        *,
        request: ResolveRequest,
        source_url: str,
        resolver_name: str,
        origin_source: str,
        remote_id: str | None,
    ) -> dict[str, Any]:
        try:
            safe_url = validate_url_safety(raw_url, host_resolver=request.host_resolver)
            response, final_url = fetch_head_follow_redirects(safe_url.normalized_url, request=request)
        except URLSafetyError as exc:
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=normalize_url(raw_url),
                resolver=resolver_name,
                skip_reason="unsafe_url",
                origin_source=origin_source,
                details={"reason": exc.reason, **exc.details},
            )
        except Exception as exc:
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=normalize_url(raw_url),
                resolver=resolver_name,
                skip_reason="resolver_error",
                origin_source=origin_source,
                details={"exception_type": type(exc).__name__},
            )
        if response.status_code in (401, 403):
            if should_continue_to_html_resolver(safe_url, response):
                return skipped_resolution(
                    original_url=raw_url,
                    normalized_url=safe_url.normalized_url,
                    resolver=resolver_name,
                    skip_reason="unsupported_media_type",
                    origin_source=origin_source,
                    details={"status_code": response.status_code, "continue_resolving": True},
                )
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=safe_url.normalized_url,
                resolver=resolver_name,
                skip_reason="requires_auth",
                origin_source=origin_source,
                details={"status_code": response.status_code},
            )
        if response.status_code in (405, 501) and should_continue_to_html_resolver(safe_url, response):
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=safe_url.normalized_url,
                resolver=resolver_name,
                skip_reason="unsupported_media_type",
                origin_source=origin_source,
                details={"status_code": response.status_code, "continue_resolving": True},
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=safe_url.normalized_url,
                resolver=resolver_name,
                skip_reason="resolver_error",
                origin_source=origin_source,
                details={"status_code": response.status_code},
            )
        content_type = clean_mime(header_value(response.headers, "content-type"))
        size_bytes = int_header(response.headers, "content-length")
        if size_bytes is not None and size_bytes > request.policy.max_media_bytes:
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=safe_url.normalized_url,
                resolver=resolver_name,
                skip_reason="too_large",
                origin_source=origin_source,
                details={"size_bytes": size_bytes, "max_media_bytes": request.policy.max_media_bytes},
            )
        path_suffix = Path(urlparse(final_url).path).suffix.lower()
        warnings: list[str] = []
        if content_type in ALLOWED_MEDIA_MIME_TYPES:
            mime_type = content_type
            media_type = ALLOWED_MEDIA_MIME_TYPES[mime_type]
        elif path_suffix == ".mov" and size_bytes is not None:
            mime_type = "video/quicktime"
            media_type = "video"
            warnings.append("Accepted .mov by extension fallback because Content-Type was unavailable or generic.")
        else:
            if should_continue_to_html_resolver(safe_url, response):
                return skipped_resolution(
                    original_url=raw_url,
                    normalized_url=safe_url.normalized_url,
                    resolver=resolver_name,
                    skip_reason="unsupported_media_type",
                    origin_source=origin_source,
                    details={
                        "content_type": content_type,
                        "extension": path_suffix,
                        "continue_resolving": True,
                    },
                )
            return skipped_resolution(
                original_url=raw_url,
                normalized_url=safe_url.normalized_url,
                resolver=resolver_name,
                skip_reason="unsupported_media_type",
                origin_source=origin_source,
                details={"content_type": content_type, "extension": path_suffix},
            )
        extension = extension_from_mime(mime_type) or (".mov" if mime_type == "video/quicktime" else path_suffix)
        remote_id = remote_id or normalized_url_hash(safe_url.normalized_url)
        return {
            "status": "resolved",
            "original_url": raw_url,
            "normalized_url": safe_url.normalized_url,
            "source_url": source_url,
            "resolved_media_url": final_url,
            "resolver": resolver_name,
            "origin_source": origin_source,
            "remote_id": remote_id,
            "media_type": media_type,
            "mime_type": mime_type,
            "extension": extension,
            "size_bytes": size_bytes,
            "media_count": 1,
            "warnings": warnings,
            "details": {
                "content_type": content_type,
                "final_url": final_url,
                "validation": "extension_fallback" if warnings else "content_type",
            },
        }


def default_link_resolver_registry() -> LinkResolverRegistry:
    return LinkResolverRegistry(
        [
            PixivArtworkLinkResolver(),
            ImgurSingleResolver(),
            RedditMediaLinkResolver(),
            DirectMediaResolver(),
            GenericHTMLMediaResolver(),
        ]
    )


def normalize_url(raw_url: str) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    try:
        parsed = parse_url(text)
    except URLSafetyError:
        return text
    return normalize_parsed_url(parsed)


def normalize_parsed_url(parsed: ParseResult) -> str:
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if hostname:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            pass
    netloc = hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse((scheme, netloc, parsed.path or "", parsed.params, parsed.query, ""))


def parse_url(raw_url: str) -> ParseResult:
    try:
        parsed = urlparse(str(raw_url or "").strip())
        parsed.port
    except ValueError as exc:
        raise URLSafetyError("malformed_url", "URL is malformed.", details={"error": str(exc)}) from exc
    return parsed


def validate_url_safety(
    raw_url: str,
    *,
    host_resolver: Callable[[str], list[str]] | None = None,
) -> SafeURL:
    raw_parsed = parse_url(raw_url)
    if raw_parsed.username or raw_parsed.password:
        raise URLSafetyError("userinfo_not_allowed", "URL userinfo is not allowed.")
    normalized = normalize_parsed_url(raw_parsed)
    parsed = parse_url(normalized)
    if parsed.scheme != "https":
        raise URLSafetyError("unsupported_scheme", "Only https URLs are accepted.", details={"scheme": parsed.scheme})
    if parsed.username or parsed.password:
        raise URLSafetyError("userinfo_not_allowed", "URL userinfo is not allowed.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise URLSafetyError("missing_host", "URL host is required.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise URLSafetyError("localhost", "Localhost URLs are not allowed.", details={"host": host})
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        resolver = host_resolver or resolve_host_ips
        ips = resolver(host)
        if not ips:
            raise URLSafetyError("unresolved_host", "URL host could not be resolved.", details={"host": host})
        for value in ips:
            ip = ipaddress.ip_address(value)
            if unsafe_ip(ip):
                raise URLSafetyError("unsafe_host_ip", "URL host resolves to a private or unsafe IP.", details={"host": host})
    else:
        if unsafe_ip(ip):
            raise URLSafetyError("unsafe_host_ip", "URL host is a private or unsafe IP.", details={"host": host})
    return SafeURL(original_url=raw_url, normalized_url=normalized, host=host)


def unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def resolve_host_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    ips: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in ips:
            ips.append(address)
    return ips


def extract_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def extract_external_links_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        for field in ("text", "caption"):
            value = message.get(field)
            if not isinstance(value, str):
                continue
            for original_url in extract_urls(value):
                try:
                    parsed = parse_url(original_url)
                except URLSafetyError:
                    continue
                if parsed.username or parsed.password:
                    continue
                normalized = normalize_parsed_url(parsed)
                host = (urlparse(normalized).hostname or "").lower()
                if host in TELEGRAM_HOSTS:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                links.append(
                    {
                        "ingest_platform": "telegram",
                        "original_url": original_url,
                        "normalized_url": normalized,
                        "source_chat_id": str(chat.get("id") or message.get("chat_id") or ""),
                        "source_message_id": str(message.get("id") or message.get("message_id") or ""),
                        "source_message_date": message.get("date") or message.get("timestamp"),
                    }
                )
    return links


def fetch_head_follow_redirects(url: str, *, request: ResolveRequest) -> tuple[HttpResponse, str]:
    return fetch_follow_redirects(url, request=request, method="HEAD", max_bytes=0)


def fetch_limited_follow_redirects(
    url: str,
    *,
    request: ResolveRequest,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> tuple[HttpResponse, str]:
    return fetch_follow_redirects(url, request=request, method="GET", max_bytes=max_bytes, headers=headers)


def fetch_follow_redirects(
    url: str,
    *,
    request: ResolveRequest,
    method: str,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> tuple[HttpResponse, str]:
    current_url = url
    client = request.http_client or UrllibHttpClient()
    for redirect_count in range(request.policy.max_redirects + 1):
        safe = validate_url_safety(current_url, host_resolver=request.host_resolver)
        response = request_url(
            client,
            safe.normalized_url,
            method=method,
            timeout=request.policy.timeout_seconds,
            max_bytes=max_bytes,
            headers=headers,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            if redirect_count >= request.policy.max_redirects:
                raise URLSafetyError("too_many_redirects", "URL exceeded the redirect limit.")
            location = header_value(response.headers, "location")
            if not location:
                raise URLSafetyError("missing_redirect_location", "Redirect response did not include Location.")
            current_url = urljoin(safe.normalized_url, location)
            continue
        return response, response.url or safe.normalized_url
    raise URLSafetyError("too_many_redirects", "URL exceeded the redirect limit.")


def request_url(
    client: Any,
    url: str,
    *,
    method: str,
    timeout: float,
    max_bytes: int,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    request_headers = headers or {}
    if method == "HEAD" and hasattr(client, "head"):
        return client.head(url, headers=request_headers, timeout=timeout)
    if method == "GET" and hasattr(client, "get_limited"):
        return client.get_limited(url, headers=request_headers, timeout=timeout, max_bytes=max_bytes)
    range_headers = {"Range": f"bytes=0-{max(0, max_bytes - 1)}"} if max_bytes > 0 else {"Range": "bytes=0-0"}
    return client.get(url, headers={**request_headers, **range_headers}, timeout=timeout)


def skipped_resolution(
    *,
    original_url: str,
    normalized_url: str,
    resolver: str | None,
    skip_reason: str,
    origin_source: str | None = None,
    remote_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "skipped",
        "original_url": original_url,
        "normalized_url": normalized_url,
        "source_url": normalized_url,
        "resolved_media_url": None,
        "resolver": resolver,
        "origin_source": origin_source,
        "remote_id": remote_id,
        "media_type": None,
        "mime_type": None,
        "extension": None,
        "size_bytes": None,
        "media_count": 0,
        "skip_reason": skip_reason,
        "warnings": [],
        "details": details or {},
    }


def resolution_to_media_item(
    resolution: dict[str, Any],
    *,
    ingest_provenance: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if resolution.get("status") != "resolved":
        return None
    origin_source = str(resolution["origin_source"])
    remote_id = str(resolution["remote_id"])
    media_type = str(resolution["media_type"])
    details = resolution.get("details") if isinstance(resolution.get("details"), dict) else {}
    platform_details = details.get(origin_source) if isinstance(details.get(origin_source), dict) else {}
    source_timestamp = (
        resolution.get("source_timestamp")
        or details.get("source_timestamp")
        or (ingest_provenance or {}).get("message_date")
    )
    part = "v0" if media_type == "video" else "p0" if media_type == "photo" else "a0"
    file_info = {
        "url": resolution["resolved_media_url"],
        "remote_url": resolution["resolved_media_url"],
        "kind": media_type,
        "page": 0,
        "part": part,
        "media_type": media_type,
        "mime_type": resolution.get("mime_type"),
        "extension": resolution.get("extension"),
        "size_bytes": resolution.get("size_bytes"),
        "source_timestamp": source_timestamp,
    }
    return {
        "platform": origin_source,
        "remote_id": remote_id,
        "media_type": media_type,
        "source_url": resolution.get("source_url") or resolution.get("normalized_url"),
        "author_id": platform_details.get("author_id"),
        "author_name": platform_details.get("author"),
        "metadata": {
            "origin_source": origin_source,
            "source_timestamp": source_timestamp,
            "resolved_media_url": resolution.get("resolved_media_url"),
            "resolver": {
                "name": resolution.get("resolver"),
                "normalized_url": resolution.get("normalized_url"),
                "media_count": resolution.get("media_count"),
                "validation": (resolution.get("details") or {}).get("validation"),
            },
            origin_source: platform_details,
            "ingested_from": ingest_provenance or {},
            "files": [file_info],
        },
    }


def html_media_urls(content: bytes, *, base_url: str) -> list[str]:
    text = content.decode("utf-8", errors="replace")
    parser = HTMLMediaParser(base_url=base_url)
    parser.feed(text)
    parser.close()
    for priority in sorted(parser.candidates):
        urls = parser.candidates[priority]
        if urls:
            return urls
    return []


def choose_primary_html_media_url(urls: list[str]) -> str | None:
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    scored = [(html_media_url_score(url), url) for url in urls]
    top_score = max(score for score, _url in scored)
    top_urls = [url for score, url in scored if score == top_score]
    if len(top_urls) == 1 and top_score > 0:
        return top_urls[0]
    return None


def html_media_url_score(url: str) -> int:
    parsed = urlparse(url)
    path = html.unescape(parsed.path).lower()
    query = parsed.query.lower()
    text = f"{path}?{query}"
    score = 0
    if "/original/" in path or "original" in path:
        score += 80
    if "download=1" in query:
        score += 40
    if "/large/" in path or "large" in path or "/full/" in path or "full" in path:
        score += 25
    if "/sample/" in path or "sample-" in path or "sample_" in path:
        score -= 40
    if re.search(r"/(?:120|180|240|360|720)x(?:120|180|240|360|720)/", path):
        score -= 40
    if "thumb" in text or "preview" in text:
        score -= 20
    return score


class HTMLMediaParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value is not None}
        lower_tag = tag.lower()
        if lower_tag == "meta":
            name = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
            if name in HTML_META_MEDIA_NAMES:
                self.add(values.get("content"), priority=0)
            return
        if lower_tag in {"video", "source"}:
            self.add(values.get("src"), priority=1)
            return
        if lower_tag == "a":
            self.add(values.get("href"), priority=2)
            return
        if lower_tag == "img":
            self.add(values.get("src") or values.get("data-src"), priority=3)

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or "https://" not in text:
            return
        for url in extract_urls(text):
            self.add(url, priority=3)

    def add(self, raw_url: str | None, *, priority: int) -> None:
        if not raw_url:
            return
        resolved = urljoin(self.base_url, html.unescape(raw_url.strip()))
        if not resolved.startswith("https://"):
            return
        if not looks_like_media_reference(resolved):
            return
        if is_decorative_media_url(resolved):
            return
        normalized = normalize_url(resolved)
        if not normalized or normalized in self._seen:
            return
        self._seen.add(normalized)
        self.candidates.setdefault(priority, []).append(resolved)


def looks_like_media_reference(url: str) -> bool:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in ALLOWED_DIRECT_EXTENSIONS:
        return True
    format_values = [value.lower() for value in parse_qs(parsed.query).get("format", [])]
    return any(f".{value}" in ALLOWED_DIRECT_EXTENSIONS for value in format_values)


def is_decorative_media_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "abs.twimg.com":
        return True
    text = html.unescape(url).lower()
    if any(marker in text for marker in DECORATIVE_MEDIA_MARKERS):
        return True
    query = parse_qs(parsed.query)
    name_values = {value.lower() for value in query.get("name", [])}
    if name_values & {"120x120", "240x240", "normal"}:
        return True
    return False


def is_x_login_wall(content: bytes) -> bool:
    text = content.decode("utf-8", errors="replace").lower()
    return all(marker in text for marker in X_REQUIRES_LOGIN_MARKERS)


def should_continue_to_html_resolver(safe_url: SafeURL, response: HttpResponse) -> bool:
    parsed = urlparse(safe_url.normalized_url)
    suffix = Path(parsed.path).suffix.lower()
    content_type = clean_mime(header_value(response.headers, "content-type"))
    if content_type in HTML_MIME_TYPES:
        return True
    if response.status_code in (401, 403, 405, 501) and suffix not in ALLOWED_DIRECT_EXTENSIONS:
        return True
    return False


def header_value(headers: dict[str, str], name: str) -> str | None:
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value
    return None


def int_header(headers: dict[str, str], name: str) -> int | None:
    value = header_value(headers, name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def clean_mime(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def origin_source_from_host(host: str) -> str:
    text = host.lower()
    if text.startswith("www."):
        text = text[4:]
    return safe_storage_segment(text.replace(".", "_"), max_length=60)


def normalized_url_hash(normalized_url: str) -> str:
    return "url_" + hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]


def pixiv_artwork_id(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "artworks" and index + 1 < len(parts) and parts[index + 1].isdigit():
            return parts[index + 1]
    values = parse_qs(parsed.query).get("illust_id", [])
    return values[0] if values and values[0].isdigit() else None


def imgur_remote_id(page_url: str, media_url: str) -> str:
    page_path = urlparse(page_url).path.strip("/")
    if page_path:
        return safe_storage_segment(page_path.split("/")[-1], max_length=80)
    media_stem = Path(urlparse(media_url).path).stem
    return safe_storage_segment(media_stem or normalized_url_hash(media_url), max_length=80)


def extension_for_mime_or_url(mime_type: str | None, url: str) -> str:
    if mime_type:
        guessed = extension_from_mime(mime_type)
        if guessed:
            return guessed
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in ALLOWED_DIRECT_EXTENSIONS:
        if suffix == ".jpeg":
            return ".jpg"
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return guessed or ".bin"
