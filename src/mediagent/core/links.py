"""URL intake, safety, and resolver helpers for experimental link workflows."""

from __future__ import annotations

import hashlib
import html
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from urllib.parse import ParseResult

from mediagent.core.http import HttpResponse, UrllibHttpClient
from mediagent.core.storage import extension_from_mime, safe_storage_segment
from mediagent.platforms.instagram import links as instagram_links
from mediagent.platforms.pixiv import links as pixiv_links
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
INSTAGRAM_HOSTS = instagram_links.INSTAGRAM_HOSTS
IMGUR_HOSTS = {"imgur.com", "www.imgur.com"}
REDGIFS_HOSTS = {"redgifs.com", "www.redgifs.com"}
REDGIFS_MEDIA_HOSTS = {"media.redgifs.com"}
REDDIT_HOSTS = tuple(
    sorted(
        reddit_links.REDDIT_PAGE_HOSTS
        | reddit_links.REDDIT_DIRECT_IMAGE_HOSTS
        | reddit_links.REDDIT_DIRECT_VIDEO_HOSTS
    )
)
RESERVED_PLATFORM_PAGE_HOSTS = PIXIV_HOSTS | INSTAGRAM_HOSTS | IMGUR_HOSTS
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
REDGIFS_MP4_RE = re.compile(r"https:\\?/\\?/media\.redgifs\.com/[^\"'<>\\\s]+?\.mp4", re.IGNORECASE)


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
    env: Mapping[str, str] | None = None
    cwd: Path | None = None
    allowed_write_roots: tuple[Path, ...] | None = None
    dry_run: bool = False
    platform_options: dict[str, Any] = field(default_factory=dict)


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
        return safe_url.host in PIXIV_HOSTS and pixiv_links.pixiv_artwork_id(safe_url.normalized_url) is not None

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        artwork_id = pixiv_links.pixiv_artwork_id(safe_url.normalized_url)
        canonical_url = pixiv_links.pixiv_canonical_artwork_url(str(artwork_id)) if artwork_id else safe_url.normalized_url
        try:
            item = pixiv_links.resolve_artwork_from_url(
                safe_url.normalized_url,
                env=request.env or {},
                cwd=request.cwd or Path.cwd(),
                http_client=request.http_client,
                allowed_write_roots=request.allowed_write_roots,
                allow_credential_write=not request.dry_run,
                timeout=request.policy.timeout_seconds,
                include_ugoira_metadata=bool(
                    (request.platform_options.get("pixiv") or {}).get("include_ugoira_metadata", True)
                ),
            )
        except Exception as exc:
            code = getattr(exc, "code", "pixiv_artwork_resolve_failed")
            details = getattr(exc, "public_details", lambda: {"error_code": code})()
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason=_pixiv_skip_reason(str(code)),
                origin_source="pixiv",
                remote_id=artwork_id,
                details=details,
            )
        candidates = _pixiv_candidates(item)
        if not candidates:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="pixiv",
                remote_id=item.get("remote_id"),
                details={
                    "error_code": "pixiv_artwork_unsupported_media",
                    "reason": "empty_files",
                    "retryable": False,
                    "user_action_required": False,
                },
            )
        canonical_url = str(item.get("source_url") or canonical_url)
        return {
            "status": "resolved",
            "original_url": safe_url.original_url,
            "normalized_url": safe_url.normalized_url,
            "canonical_url": canonical_url,
            "aliases": _resolution_aliases(
                canonical_url,
                [safe_url.normalized_url, pixiv_links.pixiv_localized_artwork_url(str(item["remote_id"]))],
            ),
            "source_url": canonical_url,
            "resolved_media_url": candidates[0]["url"],
            "resolver": self.spec.name,
            "origin_source": "pixiv",
            "remote_id": str(item["remote_id"]),
            "media_type": str(item.get("media_type") or candidates[0].get("media_type") or "photo"),
            "mime_type": candidates[0].get("mime_type"),
            "extension": candidates[0].get("extension"),
            "size_bytes": None,
            "media_count": len(candidates),
            "media_candidates": candidates,
            "selected_candidate": candidates[0],
            "warnings": [],
            "source_timestamp": (item.get("metadata") or {}).get("create_date"),
            "details": {
                "pixiv": {
                    "remote_id": str(item["remote_id"]),
                    "author_id": item.get("author_id"),
                    "author": item.get("author_name"),
                    **_pixiv_public_metadata(item.get("metadata") or {}),
                },
                "source_timestamp": (item.get("metadata") or {}).get("create_date"),
                "validation": "platform_session",
                "post_scope": "all_resources",
            },
        }


class InstagramMediaLinkResolver(Resolver):
    spec = ResolverSpec(
        name="instagram_media_link",
        allowed_domains=tuple(sorted(INSTAGRAM_HOSTS)),
        matching_rules="Instagram post, reel, or tv URLs resolved through a configured saved session.",
        supports_auth=True,
        max_media_items=20,
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return safe_url.host in INSTAGRAM_HOSTS and instagram_links.instagram_shortcode(safe_url.normalized_url) is not None

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        try:
            post = instagram_links.resolve_post_from_url(
                safe_url.normalized_url,
                env=request.env or {},
                cwd=request.cwd or Path.cwd(),
                http_client=request.http_client,
                session_file=((request.platform_options.get("instagram") or {}).get("session_file")),
                allowed_write_roots=request.allowed_write_roots,
                timeout=request.policy.timeout_seconds,
            )
        except Exception as exc:
            code = getattr(exc, "code", "instagram_resolve_failed")
            details = getattr(exc, "public_details", lambda: {"error_code": code})()
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason=_instagram_skip_reason(str(code)),
                origin_source="instagram",
                remote_id=instagram_links.instagram_shortcode(safe_url.normalized_url),
                details=details,
            )
        candidates = _instagram_candidates(post)
        if not candidates:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="instagram",
                remote_id=post.shortcode,
                details={
                    "error_code": "instagram_media_unsupported",
                    "reason": "empty_resources",
                    "retryable": False,
                    "user_action_required": False,
                },
            )
        return {
            "status": "resolved",
            "original_url": safe_url.original_url,
            "normalized_url": safe_url.normalized_url,
            "canonical_url": post.canonical_url,
            "aliases": _resolution_aliases(post.canonical_url, [safe_url.normalized_url, post.source_url]),
            "source_url": post.source_url,
            "resolved_media_url": candidates[0]["url"],
            "resolver": self.spec.name,
            "origin_source": "instagram",
            "remote_id": post.shortcode,
            "media_type": post.media_type,
            "mime_type": candidates[0].get("mime_type"),
            "extension": candidates[0].get("extension"),
            "size_bytes": None,
            "media_count": len(candidates),
            "media_candidates": candidates,
            "selected_candidate": candidates[0],
            "warnings": [],
            "details": {
                "instagram": post.metadata,
                "source_timestamp": post.source_timestamp,
                "validation": "platform_session",
                "post_scope": "all_resources",
            },
        }


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
                headers=_reddit_page_headers(),
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
        if extraction.media_urls and len(extraction.media_urls) > 1:
            return self._resolve_media_urls(
                media_urls=list(extraction.media_urls),
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
        if extraction.external_urls:
            delegated = self._delegate_external_urls(
                external_urls=list(extraction.external_urls),
                source_url=final_url,
                request=request,
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                remote_id=extraction.remote_id,
                reddit_metadata={**extraction.metadata, "source_kind": "post"},
                details={
                    **(extraction.details or {}),
                    "html_source": "reddit",
                    "resolved_page_url": final_url,
                },
            )
            if delegated is not None:
                return delegated

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
                headers=_reddit_page_headers(),
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
            if extraction.external_urls:
                delegated = self._delegate_external_urls(
                    external_urls=list(extraction.external_urls),
                    source_url=final_url,
                    request=request,
                    original_url=original_url,
                    normalized_url=normalized_url,
                    remote_id=extraction.remote_id,
                    reddit_metadata={**extraction.metadata, "source_kind": "post"},
                    details={
                        "legacy_url": legacy_url,
                        "resolved_page_url": final_url,
                        **(extraction.details or {}),
                    },
                )
                if delegated is not None:
                    return delegated
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
        if extraction.media_urls and len(extraction.media_urls) > 1:
            return self._resolve_media_urls(
                media_urls=list(extraction.media_urls),
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

    def _delegate_external_urls(
        self,
        *,
        external_urls: list[str],
        source_url: str,
        request: ResolveRequest,
        original_url: str,
        normalized_url: str,
        remote_id: str | None,
        reddit_metadata: dict[str, Any],
        details: dict[str, Any],
    ) -> dict[str, Any] | None:
        unique_urls = list(dict.fromkeys(external_urls))
        if not unique_urls:
            return None
        if len(unique_urls) > 1:
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason="ambiguous_candidates",
                origin_source="reddit",
                remote_id=remote_id,
                details={**details, "external_urls": unique_urls, "reason": "ambiguous_external_urls"},
            )
        external_url = unique_urls[0]
        delegated = default_link_resolver_registry().resolve(external_url, request=request)
        if delegated.get("status") != "resolved":
            return skipped_resolution(
                original_url=original_url,
                normalized_url=normalized_url,
                resolver=self.spec.name,
                skip_reason=delegated.get("skip_reason") or "external_source_hidden",
                origin_source="reddit",
                remote_id=remote_id,
                details={
                    **details,
                    "external_url": external_url,
                    "delegated_resolution": delegated,
                    "reason": "external_delegation_failed",
                },
            )
        canonical_url = delegated.get("canonical_url") or delegated.get("normalized_url")
        delegated["aliases"] = _merge_resolution_aliases(
            delegated.get("aliases") or [],
            _resolution_aliases(
                canonical_url,
                [source_url, original_url, normalized_url, external_url, delegated.get("resolved_media_url")],
            ),
        )
        delegated["details"] = {
            **(delegated.get("details") or {}),
            "delegated_from": {
                "resolver": self.spec.name,
                "source_url": source_url,
                "original_url": original_url,
                "remote_id": remote_id,
            },
            "reddit": reddit_metadata,
            "reddit_delegate": details,
        }
        return delegated

    def _resolve_media_urls(
        self,
        *,
        media_urls: list[str],
        source_url: str,
        request: ResolveRequest,
        remote_id: str | None,
        reddit_metadata: dict[str, Any],
        details: dict[str, Any],
    ) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        warnings: list[str] = []
        for index, media_url in enumerate(media_urls):
            direct = DirectMediaResolver().resolve_url(
                media_url,
                request=request,
                source_url=source_url,
                resolver_name=self.spec.name,
                origin_source="reddit",
                remote_id=remote_id,
            )
            if direct.get("status") != "resolved":
                return skipped_resolution(
                    original_url=source_url,
                    normalized_url=normalize_url(source_url),
                    resolver=self.spec.name,
                    skip_reason=direct.get("skip_reason") or "resolver_error",
                    origin_source="reddit",
                    remote_id=remote_id,
                    details={
                        **details,
                        "failed_candidate_url": media_url,
                        "failed_candidate_index": index,
                        "failed_candidate": direct,
                    },
                )
            candidate = candidate_from_resolution(direct, file_index=index)
            candidate["group_id"] = remote_id or normalized_url_hash(source_url)
            candidate["required"] = True
            candidates.append(candidate)
            warnings.extend(direct.get("warnings") or [])
        media_type = candidates[0]["media_type"] if candidates else "photo"
        return {
            "status": "resolved",
            "original_url": source_url,
            "normalized_url": normalize_url(source_url),
            "canonical_url": reddit_links.legacy_post_url(source_url) or normalize_url(source_url),
            "aliases": _resolution_aliases(source_url, [candidate["url"] for candidate in candidates]),
            "source_url": source_url,
            "resolved_media_url": candidates[0]["url"] if candidates else None,
            "resolver": self.spec.name,
            "origin_source": "reddit",
            "remote_id": remote_id or normalized_url_hash(source_url),
            "media_type": media_type,
            "mime_type": candidates[0].get("mime_type") if candidates else None,
            "extension": candidates[0].get("extension") if candidates else None,
            "size_bytes": sum(
                int(candidate["size_bytes"] or 0) for candidate in candidates if candidate.get("size_bytes") is not None
            )
            or None,
            "media_count": len(candidates),
            "media_candidates": candidates,
            "selected_candidate": candidates[0] if candidates else None,
            "warnings": warnings,
            "details": {
                **details,
                "reddit": reddit_metadata,
                "source_timestamp": reddit_metadata.get("created_at"),
            },
        }

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


def _reddit_page_headers() -> dict[str, str]:
    return {"Cookie": "over18=1"}


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


class RedgifsResolver(Resolver):
    spec = ResolverSpec(
        name="redgifs",
        allowed_domains=tuple(sorted(REDGIFS_HOSTS | REDGIFS_MEDIA_HOSTS)),
        matching_rules="Public Redgifs watch pages or direct media.redgifs.com MP4 files.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return safe_url.host in REDGIFS_HOSTS or safe_url.host in REDGIFS_MEDIA_HOSTS

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        if safe_url.host in REDGIFS_MEDIA_HOSTS:
            return self._resolve_video_url(
                media_url=safe_url.normalized_url,
                source_url=safe_url.normalized_url,
                request=request,
                details={"html_source": "direct_media"},
            )

        redgifs_id = redgifs_watch_id(safe_url.normalized_url)
        if redgifs_id is None:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="unsupported_media_type",
                origin_source="redgifs",
                details={"reason": "unsupported_redgifs_url"},
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
                origin_source="redgifs",
                remote_id=redgifs_id,
                details={"reason": exc.reason, **exc.details},
            )
        except Exception as exc:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="redgifs",
                remote_id=redgifs_id,
                details={"exception_type": type(exc).__name__},
            )
        if response.status_code in (401, 403):
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="requires_auth",
                origin_source="redgifs",
                remote_id=redgifs_id,
                details={"status_code": response.status_code},
            )
        if not 200 <= response.status_code < 300:
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason="resolver_error",
                origin_source="redgifs",
                remote_id=redgifs_id,
                details={"status_code": response.status_code},
            )
        media_urls = redgifs_media_urls(response.content, base_url=final_url)
        media_url = choose_primary_redgifs_video_url(media_urls, redgifs_id=redgifs_id)
        if media_url is None:
            reason = "javascript_required" if not media_urls else "ambiguous_candidates"
            return skipped_resolution(
                original_url=safe_url.original_url,
                normalized_url=safe_url.normalized_url,
                resolver=self.spec.name,
                skip_reason=reason,
                origin_source="redgifs",
                remote_id=redgifs_id,
                details={
                    "resolved_page_url": final_url,
                    "candidate_count": len(media_urls),
                    "reason": "no_static_mp4_candidate" if not media_urls else "ambiguous_redgifs_candidates",
                },
            )
        result = self._resolve_video_url(
            media_url=media_url,
            source_url=safe_url.normalized_url,
            request=request,
            details={
                "html_source": "redgifs",
                "resolved_page_url": final_url,
                "candidate_count": len(media_urls),
            },
        )
        if result.get("status") == "resolved":
            canonical_url = f"https://www.redgifs.com/watch/{redgifs_id}"
            result["normalized_url"] = safe_url.normalized_url
            result["canonical_url"] = canonical_url
            result["aliases"] = _resolution_aliases(canonical_url, [safe_url.normalized_url, media_url])
            result["source_url"] = canonical_url
            result["remote_id"] = redgifs_id
            result["details"] = {
                **result.get("details", {}),
                "redgifs": {
                    "id": redgifs_id,
                    "watch_url": canonical_url,
                    "audio_status": redgifs_audio_status(media_url),
                },
            }
        return result

    def _resolve_video_url(
        self,
        *,
        media_url: str,
        source_url: str,
        request: ResolveRequest,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        redgifs_id = redgifs_media_id(media_url) or redgifs_watch_id(source_url) or normalized_url_hash(media_url)
        direct = DirectMediaResolver().resolve_url(
            media_url,
            request=request,
            source_url=source_url,
            resolver_name=self.spec.name,
            origin_source="redgifs",
            remote_id=redgifs_id,
        )
        if direct.get("status") == "resolved":
            direct["canonical_url"] = f"https://www.redgifs.com/watch/{redgifs_id}"
            direct["aliases"] = _resolution_aliases(direct["canonical_url"], [source_url, media_url])
            direct["details"] = {
                **direct.get("details", {}),
                **details,
                "redgifs": {
                    "id": redgifs_id,
                    "watch_url": f"https://www.redgifs.com/watch/{redgifs_id}",
                    "audio_status": redgifs_audio_status(media_url),
                },
            }
        return direct


class ReservedPlatformPageResolver(Resolver):
    spec = ResolverSpec(
        name="reserved_platform_page",
        allowed_domains=tuple(sorted(RESERVED_PLATFORM_PAGE_HOSTS)),
        matching_rules="Known platform page domains that must not fall through to generic direct or HTML resolution.",
    )

    def matches(self, safe_url: SafeURL) -> bool:
        return safe_url.host in RESERVED_PLATFORM_PAGE_HOSTS

    def resolve(self, safe_url: SafeURL, request: ResolveRequest) -> dict[str, Any]:
        platform = _reserved_platform_name(safe_url.host)
        return skipped_resolution(
            original_url=safe_url.original_url,
            normalized_url=safe_url.normalized_url,
            resolver=self.spec.name,
            skip_reason=f"{platform}_url_unsupported",
            origin_source=platform,
            details={
                "host": safe_url.host,
                "reason": _reserved_platform_reason(platform, safe_url.normalized_url),
                "retryable": False,
                "user_action_required": False,
            },
        )


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
        result = {
            "status": "resolved",
            "original_url": raw_url,
            "normalized_url": safe_url.normalized_url,
            "canonical_url": safe_url.normalized_url,
            "aliases": _resolution_aliases(safe_url.normalized_url, [source_url, final_url]),
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
            "media_candidates": [],
            "selected_candidate": None,
            "warnings": warnings,
            "details": {
                "content_type": content_type,
                "final_url": final_url,
                "validation": "extension_fallback" if warnings else "content_type",
            },
        }
        candidate = candidate_from_resolution(result, file_index=0)
        result["media_candidates"] = [candidate]
        result["selected_candidate"] = candidate
        return result


def default_link_resolver_registry() -> LinkResolverRegistry:
    return LinkResolverRegistry(
        [
            PixivArtworkLinkResolver(),
            InstagramMediaLinkResolver(),
            ImgurSingleResolver(),
            RedgifsResolver(),
            RedditMediaLinkResolver(),
            ReservedPlatformPageResolver(),
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
        "canonical_url": normalized_url,
        "aliases": _resolution_aliases(normalized_url, [original_url]),
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
        "media_candidates": [],
        "selected_candidate": None,
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
    candidates = resolution_media_candidates(resolution)
    files: list[dict[str, Any]] = []
    runtime_files: list[dict[str, Any]] = []
    group_id = None
    required_files = 0
    optional_files = 0
    for index, candidate in enumerate(candidates):
        candidate_media_type = str(candidate.get("media_type") or media_type)
        part = candidate.get("part") or default_part(candidate_media_type, index)
        candidate_group_id = candidate.get("group_id") or remote_id
        group_id = group_id or candidate_group_id
        required = bool(candidate.get("required", True))
        if required:
            required_files += 1
        else:
            optional_files += 1
        file_record = {
            "url": candidate["url"],
            "remote_url": candidate["url"],
            "kind": candidate_media_type,
            "page": index,
            "part": part,
            "media_type": candidate_media_type,
            "mime_type": candidate.get("mime_type"),
            "extension": candidate.get("extension"),
            "size_bytes": candidate.get("size_bytes"),
            "source_timestamp": candidate.get("source_timestamp") or source_timestamp,
            "content_identity": candidate.get("content_identity"),
            "quality_rank": candidate.get("quality_rank"),
            "group_id": candidate_group_id,
            "required": required,
        }
        if candidate.get("storage_category"):
            file_record["storage_category"] = candidate["storage_category"]
        files.append(file_record)
        runtime_file = dict(file_record)
        if isinstance(candidate.get("download_context"), dict):
            runtime_file["download_context"] = candidate["download_context"]
        if isinstance(candidate.get("runtime_headers"), dict):
            runtime_file["runtime_headers"] = candidate["runtime_headers"]
        runtime_files.append(runtime_file)
    metadata = {
        "origin_source": origin_source,
        "source_timestamp": source_timestamp,
        "resolved_media_url": resolution.get("resolved_media_url"),
        "resolver": {
            "name": resolution.get("resolver"),
            "normalized_url": resolution.get("normalized_url"),
            "canonical_url": resolution.get("canonical_url"),
            "aliases": resolution.get("aliases") or [],
            "media_count": resolution.get("media_count"),
            "validation": (resolution.get("details") or {}).get("validation"),
        },
        origin_source: platform_details,
        "ingested_from": ingest_provenance or {},
        "files": files,
    }
    for key in ("pixiv_type", "work_type", "storage_category", "availability_reason"):
        if platform_details.get(key) is not None:
            metadata[key] = platform_details[key]
    if files:
        metadata["candidate_group"] = {
            "id": group_id,
            "ordering": "file_index",
            "required_files": required_files,
            "optional_files": optional_files,
            "partial_success": "item_status_partial_when_any_required_file_fails",
        }
    if isinstance(details.get("delegated_from"), dict):
        metadata["delegated_from"] = details["delegated_from"]
    if origin_source != "reddit" and isinstance(details.get("reddit"), dict):
        metadata["reddit"] = details["reddit"]
    item = {
        "platform": origin_source,
        "remote_id": remote_id,
        "media_type": media_type,
        "source_url": resolution.get("source_url") or resolution.get("normalized_url"),
        "author_id": platform_details.get("author_id"),
        "author_name": platform_details.get("author"),
        "metadata": metadata,
    }
    if runtime_files != files:
        item["_runtime"] = {"files": runtime_files}
    return item


def resolution_media_candidates(resolution: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = resolution.get("media_candidates")
    if isinstance(candidates, list) and candidates:
        return [candidate for candidate in candidates if isinstance(candidate, dict) and candidate.get("url")]
    if not resolution.get("resolved_media_url"):
        return []
    return [candidate_from_resolution(resolution, file_index=0)]


def candidate_from_resolution(resolution: dict[str, Any], *, file_index: int) -> dict[str, Any]:
    url = str(resolution.get("resolved_media_url") or "")
    media_type = str(resolution.get("media_type") or "")
    candidate = {
        "url": url,
        "media_type": media_type,
        "mime_type": resolution.get("mime_type"),
        "extension": resolution.get("extension"),
        "size_bytes": resolution.get("size_bytes"),
        "source": resolution.get("origin_source"),
        "quality_rank": resolution.get("quality_rank", 0),
        "file_index": file_index,
        "part": default_part(media_type, file_index),
        "content_identity": resolution.get("content_identity") or normalized_url_hash(url),
        "persistable_headers": {},
        "download_context_ref": None,
        "details": {
            "resolver": resolution.get("resolver"),
            "source_url": resolution.get("source_url"),
        },
    }
    if isinstance(resolution.get("download_context"), dict):
        candidate["download_context"] = resolution["download_context"]
    if isinstance(resolution.get("runtime_headers"), dict):
        candidate["runtime_headers"] = resolution["runtime_headers"]
    return candidate


def sanitize_link_resolution_for_output(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_link_resolution_for_output(item) for item in value]
    if not isinstance(value, dict):
        return value
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if lowered in {"headers", "persistable_headers"} and isinstance(item, dict):
            sanitized[key] = {
                header: header_value
                for header, header_value in item.items()
                if header.lower()
                not in {
                    "authorization",
                    "cookie",
                    "proxy-authorization",
                    "set-cookie",
                    "x-csrf-token",
                    "x-xsrf-token",
                }
            }
            continue
        if lowered in {"runtime_headers", "download_context"}:
            sanitized[key] = None
            continue
        sanitized[key] = sanitize_link_resolution_for_output(item)
    return sanitized


def _instagram_candidates(post: instagram_links.InstagramPost) -> list[dict[str, Any]]:
    type_counts: dict[str, int] = {"photo": 0, "video": 0, "audio": 0}
    candidates: list[dict[str, Any]] = []
    for resource in post.resources:
        media_type = resource.media_type
        part = default_part(media_type, type_counts.get(media_type, resource.index))
        type_counts[media_type] = type_counts.get(media_type, 0) + 1
        candidates.append(
            {
                "url": resource.stable_url,
                "media_type": media_type,
                "mime_type": resource.mime_type,
                "extension": resource.extension,
                "size_bytes": None,
                "source": "instagram",
                "quality_rank": 0,
                "file_index": resource.index,
                "part": part,
                "content_identity": resource.content_identity,
                "group_id": post.shortcode,
                "required": True,
                "persistable_headers": {},
                "download_context_ref": None,
                "download_context": {"url": resource.download_url},
                "source_timestamp": resource.source_timestamp or post.source_timestamp,
                "details": {
                    "resolver": "instagram_media_link",
                    "source_url": post.source_url,
                    "resource_id": resource.resource_id,
                },
            }
        )
    return candidates


def _instagram_skip_reason(code: str) -> str:
    if code in {
        "instagram_session_missing",
        "instagram_session_invalid",
        "instagram_login_required",
        "instagram_checkpoint_required",
        "instagram_two_factor_required",
    }:
        return "requires_auth"
    if code in {"instagram_rate_limited", "instagram_temporarily_blocked"}:
        return "rate_limited"
    if code == "unsafe_credential_path":
        return "unsafe_credential_path"
    if code == "instagram_media_not_found":
        return "deleted_or_removed"
    if code in {"instagram_media_private", "instagram_media_unsupported"}:
        return "unsupported_media_type"
    return "resolver_error"


def _pixiv_skip_reason(code: str) -> str:
    if code in {
        "pixiv_auth_missing_credentials",
        "pixiv_auth_refresh_failed",
        "pixiv_auth_failed",
        "pixiv_access_token_missing",
    }:
        return "requires_auth"
    if code == "unsafe_credential_path":
        return "unsafe_credential_path"
    if code == "pixiv_rate_limited":
        return "rate_limited"
    if code in {"pixiv_artwork_not_found", "pixiv_artwork_unavailable"}:
        return "deleted_or_removed"
    if code in {"pixiv_artwork_private", "pixiv_artwork_unsupported_media"}:
        return "unsupported_media_type"
    return "resolver_error"


def _pixiv_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    files = metadata.get("files") if isinstance(metadata.get("files"), list) else []
    candidates: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {"photo": 0, "video": 0, "audio": 0}
    for index, file_info in enumerate(files):
        if not isinstance(file_info, dict) or not file_info.get("url"):
            continue
        kind = str(file_info.get("kind") or "")
        media_type = "video" if kind == "ugoira_zip" else "photo"
        part = default_part(media_type, type_counts.get(media_type, index))
        type_counts[media_type] = type_counts.get(media_type, 0) + 1
        url = str(file_info["url"])
        mime_type = pixiv_links.mime_type_for_file(file_info)
        candidates.append(
            {
                "url": url,
                "media_type": media_type,
                "mime_type": mime_type,
                "extension": pixiv_links.extension_for_file(file_info),
                "size_bytes": None,
                "source": "pixiv",
                "quality_rank": 0,
                "file_index": index,
                "page": file_info.get("page", index),
                "part": part,
                "content_identity": pixiv_links.content_identity(item, file_info),
                "group_id": str(item.get("remote_id")),
                "required": True,
                "persistable_headers": {},
                "download_context_ref": None,
                "runtime_headers": {"Referer": "https://www.pixiv.net/"},
                "source_timestamp": metadata.get("create_date"),
                "storage_category": metadata.get("storage_category"),
                "details": {
                    "resolver": "pixiv_artwork_link",
                    "source_url": item.get("source_url"),
                    "kind": kind,
                    "page": file_info.get("page", index),
                },
            }
        )
    return candidates


def _pixiv_public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "title",
        "caption",
        "pixiv_type",
        "work_type",
        "storage_category",
        "availability_reason",
        "create_date",
        "page_count",
        "width",
        "height",
        "sanity_level",
        "x_restrict",
        "total_bookmarks",
        "total_view",
        "visible",
        "is_muted",
        "tools",
        "tags",
        "ugoira_metadata",
    }
    return {key: value for key, value in metadata.items() if key in allowed_keys}


def default_part(media_type: str, index: int) -> str:
    if media_type == "video":
        return f"v{index}"
    if media_type == "audio":
        return f"a{index}"
    return f"p{index}"


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


def _reserved_platform_name(host: str) -> str:
    if host in INSTAGRAM_HOSTS:
        return "instagram"
    if host in PIXIV_HOSTS:
        return "pixiv"
    if host in IMGUR_HOSTS:
        return "imgur"
    return origin_source_from_host(host)


def _reserved_platform_reason(platform: str, url: str) -> str:
    if platform == "instagram":
        kind = instagram_links.instagram_post_kind(url)
        return f"{kind}_url_not_supported" if kind else "unsupported_instagram_url"
    if platform == "pixiv":
        return "unsupported_pixiv_url"
    if platform == "imgur":
        return "unsupported_imgur_url"
    return "reserved_platform_url_not_supported"


def normalized_url_hash(normalized_url: str) -> str:
    return "url_" + hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]


def _resolution_aliases(canonical_url: str | None, urls: list[str]) -> list[dict[str, str]]:
    aliases: list[dict[str, str]] = []
    if canonical_url:
        aliases.append({"kind": "canonical", "url": normalize_url(canonical_url)})
    for url in urls:
        if not url:
            continue
        aliases.append({"kind": "url", "url": normalize_url(url)})
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for alias in aliases:
        key = f"{alias['kind']}:{alias['url']}"
        if key in seen or not alias["url"]:
            continue
        seen.add(key)
        unique.append(alias)
    return unique


def _merge_resolution_aliases(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for group in groups:
        for alias in group:
            if not isinstance(alias, dict):
                continue
            kind = str(alias.get("kind") or "url")
            url = alias.get("url")
            if not isinstance(url, str) or not url:
                continue
            key = f"{kind}:{url}"
            if key in seen:
                continue
            seen.add(key)
            merged.append({"kind": kind, "url": url})
    return merged


def pixiv_artwork_id(url: str) -> str | None:
    return pixiv_links.pixiv_artwork_id(url)


def imgur_remote_id(page_url: str, media_url: str) -> str:
    page_path = urlparse(page_url).path.strip("/")
    if page_path:
        return safe_storage_segment(page_path.split("/")[-1], max_length=80)
    media_stem = Path(urlparse(media_url).path).stem
    return safe_storage_segment(media_stem or normalized_url_hash(media_url), max_length=80)


def redgifs_watch_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in REDGIFS_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "watch":
        return safe_storage_segment(parts[1], max_length=80)
    return None


def redgifs_media_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in REDGIFS_MEDIA_HOSTS:
        return None
    stem = Path(parsed.path).stem
    for suffix in ("-silent", "-mobile", "-hd", "-sd"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return safe_storage_segment(stem, max_length=80) if stem else None


def redgifs_audio_status(media_url: str) -> str:
    stem = Path(urlparse(media_url).path).stem.lower()
    if stem.endswith("-silent") or "-silent-" in stem:
        return "silent"
    return "unknown"


def redgifs_media_urls(content: bytes, *, base_url: str) -> list[str]:
    text = html.unescape(content.decode("utf-8", errors="replace")).replace("\\/", "/")
    urls: list[str] = []
    seen: set[str] = set()
    for match in REDGIFS_MP4_RE.finditer(text):
        normalized = normalize_url(urljoin(base_url, match.group(0).replace("\\/", "/")))
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
    return urls


def choose_primary_redgifs_video_url(urls: list[str], *, redgifs_id: str) -> str | None:
    if not urls:
        return None
    scored = [(redgifs_video_score(url, redgifs_id=redgifs_id), url) for url in urls]
    top_score = max(score for score, _url in scored)
    top_urls = [url for score, url in scored if score == top_score]
    if len(top_urls) == 1 and top_score >= 0:
        return top_urls[0]
    return None


def redgifs_video_score(url: str, *, redgifs_id: str) -> int:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in REDGIFS_MEDIA_HOSTS:
        return -1000
    stem = Path(parsed.path).stem
    lower_stem = stem.lower()
    lower_id = redgifs_id.lower()
    score = 0
    if lower_stem == lower_id:
        score += 100
    elif lower_stem.startswith(lower_id):
        score += 60
    if lower_stem.endswith("-silent"):
        score -= 5
    if "mobile" in lower_stem or "mini" in lower_stem:
        score -= 20
    if parsed.path.lower().endswith(".mp4"):
        score += 10
    return score


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
