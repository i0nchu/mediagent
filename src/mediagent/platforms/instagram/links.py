"""Instagram explicit-link resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.storage import extension_from_mime, safe_storage_segment
from mediagent.platforms.instagram import auth as instagram_auth


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
SUPPORTED_POST_KINDS = {"p", "reel", "tv"}


@dataclass(frozen=True)
class InstagramResource:
    index: int
    media_type: str
    mime_type: str
    extension: str
    download_url: str
    stable_url: str
    content_identity: str
    source_timestamp: str | None = None
    resource_id: str | None = None


@dataclass(frozen=True)
class InstagramPost:
    shortcode: str
    kind: str
    canonical_url: str
    source_url: str
    media_type: str
    source_timestamp: str | None
    resources: tuple[InstagramResource, ...]
    metadata: dict[str, Any]


def instagram_shortcode(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in INSTAGRAM_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in SUPPORTED_POST_KINDS:
        return None
    return safe_storage_segment(parts[1], max_length=64)


def instagram_post_kind(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in SUPPORTED_POST_KINDS:
        return parts[0]
    return None


def instagram_requested_img_index(url: str) -> int | None:
    query = parse_qs(urlparse(url).query)
    value = (query.get("img_index") or [None])[0]
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def instagram_canonical_url(shortcode: str, *, kind: str | None = None) -> str:
    canonical_kind = kind if kind in SUPPORTED_POST_KINDS else "p"
    return f"https://www.instagram.com/{canonical_kind}/{safe_storage_segment(shortcode, max_length=64)}/"


def resolve_post_from_url(
    url: str,
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None = None,
    session_file: str | None = None,
    allowed_write_roots: tuple[Path, ...] | list[Path] | None = None,
    timeout: float = 30.0,
) -> InstagramPost:
    shortcode = instagram_shortcode(url)
    kind = instagram_post_kind(url)
    if not shortcode or not kind:
        raise instagram_auth.InstagramPlatformError(
            "instagram_media_unsupported",
            "Instagram URL is not a supported post, reel, or tv URL.",
            details={"reason": "unsupported_instagram_url"},
        )
    path = instagram_auth.session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is not None and allowed_write_roots is not None:
        try:
            ensure_inside(path, list(allowed_write_roots))
        except PathSafetyError as exc:
            raise instagram_auth.InstagramPlatformError(
                "unsafe_credential_path",
                str(exc),
                details={"session_file": str(path)},
                cause=exc,
            ) from exc
    if path is None or not path.exists():
        raise instagram_auth.InstagramPlatformError(
            "instagram_session_missing",
            "Instagram saved session is missing.",
            details={"session_file": str(path) if path else None},
        )
    if hasattr(http_client, "instagram_resolve_media"):
        value = http_client.instagram_resolve_media(
            url=url,
            shortcode=shortcode,
            session_file=str(path),
            timeout=timeout,
        )
        return _post_from_mapping(value, url=url, shortcode=shortcode, kind=kind)
    try:
        client = _client_from_session(path, timeout=timeout)
        media_pk = client.media_pk_from_code(shortcode)
        media = client.media_info(media_pk)
    except Exception as exc:  # pragma: no cover - covered through fake clients
        code = instagram_auth.classify_exception(exc, default_code="instagram_resolve_failed")
        raise instagram_auth.InstagramPlatformError(
            code,
            "Instagram media resolution failed.",
            details={"exception_type": type(exc).__name__},
            cause=exc,
        ) from exc
    return _post_from_instagrapi_media(media, url=url, shortcode=shortcode, kind=kind)


def _post_from_mapping(value: Any, *, url: str, shortcode: str, kind: str) -> InstagramPost:
    if not isinstance(value, dict):
        raise instagram_auth.InstagramPlatformError(
            "instagram_resolve_failed",
            "Instagram fake client returned an invalid resolution payload.",
            details={"reason": "invalid_client_payload"},
        )
    status = str(value.get("status") or "resolved")
    if status != "resolved":
        code = str(value.get("error_code") or value.get("code") or "instagram_resolve_failed")
        raise instagram_auth.InstagramPlatformError(
            code,
            "Instagram media resolution failed.",
            details=value.get("details") if isinstance(value.get("details"), dict) else {},
        )
    resources = []
    for index, resource in enumerate(value.get("resources") or []):
        if not isinstance(resource, dict):
            continue
        media_type = str(resource.get("media_type") or "photo")
        mime_type = str(resource.get("mime_type") or _mime_for_media_type(media_type))
        extension = str(resource.get("extension") or extension_from_mime(mime_type) or ".bin")
        download_url = str(resource.get("download_url") or resource.get("url") or "")
        if not download_url:
            continue
        resource_index = int(resource.get("index", index))
        resources.append(
            InstagramResource(
                index=resource_index,
                media_type=media_type,
                mime_type=mime_type,
                extension=extension,
                download_url=download_url,
                stable_url=str(
                    resource.get("stable_url")
                    or _stable_resource_url(shortcode=shortcode, kind=kind, index=resource_index)
                ),
                content_identity=str(
                    resource.get("content_identity")
                    or _content_identity(shortcode=shortcode, index=resource_index, resource_id=resource.get("resource_id"))
                ),
                source_timestamp=resource.get("source_timestamp") or value.get("source_timestamp"),
                resource_id=str(resource.get("resource_id")) if resource.get("resource_id") is not None else None,
            )
        )
    if not resources:
        raise instagram_auth.InstagramPlatformError(
            "instagram_media_unsupported",
            "Instagram media did not expose downloadable photo or video resources.",
            details={"reason": "empty_resources"},
        )
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    canonical = instagram_canonical_url(shortcode, kind=kind)
    return InstagramPost(
        shortcode=shortcode,
        kind=kind,
        canonical_url=str(value.get("canonical_url") or canonical),
        source_url=_source_url_without_tracking(url),
        media_type=str(value.get("media_type") or resources[0].media_type),
        source_timestamp=value.get("source_timestamp"),
        resources=tuple(resources),
        metadata={
            "shortcode": shortcode,
            "kind": kind,
            "requested_img_index": instagram_requested_img_index(url),
            **metadata,
        },
    )


def _post_from_instagrapi_media(media: Any, *, url: str, shortcode: str, kind: str) -> InstagramPost:
    source_timestamp = _iso_timestamp(getattr(media, "taken_at", None))
    canonical = instagram_canonical_url(shortcode, kind=kind)
    resources = _resources_from_media(media, shortcode=shortcode, kind=kind, source_timestamp=source_timestamp)
    if not resources:
        raise instagram_auth.InstagramPlatformError(
            "instagram_media_unsupported",
            "Instagram media did not expose downloadable photo or video resources.",
            details={"reason": "empty_resources"},
        )
    user = getattr(media, "user", None)
    metadata = {
        "shortcode": shortcode,
        "kind": kind,
        "media_pk": str(getattr(media, "pk", "") or "") or None,
        "media_id": str(getattr(media, "id", "") or "") or None,
        "product_type": getattr(media, "product_type", None),
        "caption_text": getattr(media, "caption_text", None),
        "requested_img_index": instagram_requested_img_index(url),
        "author_id": str(getattr(user, "pk", "") or "") or None,
        "author": getattr(user, "username", None),
        "has_audio": getattr(media, "has_audio", None),
        "resource_count": len(resources),
    }
    return InstagramPost(
        shortcode=shortcode,
        kind=kind,
        canonical_url=canonical,
        source_url=_source_url_without_tracking(url),
        media_type=resources[0].media_type,
        source_timestamp=source_timestamp,
        resources=tuple(resources),
        metadata={key: value for key, value in metadata.items() if value not in (None, "", [])},
    )


def _resources_from_media(
    media: Any,
    *,
    shortcode: str,
    kind: str,
    source_timestamp: str | None,
) -> list[InstagramResource]:
    raw_resources = list(getattr(media, "resources", None) or [])
    if not raw_resources:
        raw_resources = [media]
    resources: list[InstagramResource] = []
    media_type_counts = {"photo": 0, "video": 0, "audio": 0}
    for raw_index, raw in enumerate(raw_resources):
        media_type, download_url = _resource_media_type_and_url(raw)
        if not media_type or not download_url:
            continue
        mime_type = _mime_for_media_type(media_type, download_url=download_url)
        extension = extension_from_mime(mime_type) or ".bin"
        part_index = media_type_counts[media_type]
        media_type_counts[media_type] = part_index + 1
        resource_id = str(getattr(raw, "pk", "") or getattr(media, "pk", "") or raw_index)
        resources.append(
            InstagramResource(
                index=raw_index,
                media_type=media_type,
                mime_type=mime_type,
                extension=extension,
                download_url=str(download_url),
                stable_url=_stable_resource_url(shortcode=shortcode, kind=kind, index=raw_index),
                content_identity=_content_identity(shortcode=shortcode, index=raw_index, resource_id=resource_id),
                source_timestamp=source_timestamp,
                resource_id=resource_id,
            )
        )
    return resources


def _resource_media_type_and_url(resource: Any) -> tuple[str | None, str | None]:
    media_type_code = getattr(resource, "media_type", None)
    if media_type_code == 2:
        video_url = getattr(resource, "video_url", None)
        if video_url:
            return "video", str(video_url)
    image_url = _image_url(resource)
    if image_url:
        return "photo", image_url
    if getattr(resource, "video_url", None):
        return "video", str(getattr(resource, "video_url"))
    return None, None


def _image_url(resource: Any) -> str | None:
    image_versions = getattr(resource, "image_versions2", None)
    candidates = getattr(image_versions, "candidates", None)
    if candidates:
        first = candidates[0]
        value = getattr(first, "url", None)
        if value:
            return str(value)
        if isinstance(first, dict) and first.get("url"):
            return str(first["url"])
    value = getattr(resource, "thumbnail_url", None)
    return str(value) if value else None


def _client_from_session(path: Path, *, timeout: float) -> Any:
    try:
        from instagrapi import Client
    except ImportError as exc:  # pragma: no cover - dependency is present in normal installs
        raise instagram_auth.InstagramPlatformError(
            "instagram_resolve_failed",
            "The instagrapi dependency is required for Instagram support.",
            details={"missing_dependency": "instagrapi"},
            cause=exc,
        ) from exc
    client = Client()
    if hasattr(client, "request_timeout"):
        client.request_timeout = timeout
    if hasattr(client, "delay_range"):
        client.delay_range = [1, 3]
    client.load_settings(path)
    return client


def _source_url_without_tracking(url: str) -> str:
    parsed = urlparse(url)
    shortcode = instagram_shortcode(url)
    kind = instagram_post_kind(url)
    if shortcode and kind:
        return instagram_canonical_url(shortcode, kind=kind)
    return urlunparse((parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.path or "", "", "", ""))


def _stable_resource_url(*, shortcode: str, kind: str, index: int) -> str:
    return f"{instagram_canonical_url(shortcode, kind=kind)}?mediagent_resource={index}"


def _content_identity(*, shortcode: str, index: int, resource_id: Any = None) -> str:
    suffix = safe_storage_segment(resource_id if resource_id is not None else index, max_length=80)
    return f"instagram:{safe_storage_segment(shortcode, max_length=64)}:{suffix}"


def _mime_for_media_type(media_type: str, *, download_url: str | None = None) -> str:
    if media_type == "video":
        suffix = Path(urlparse(download_url or "").path).suffix.lower()
        if suffix == ".mov":
            return "video/quicktime"
        return "video/mp4"
    if media_type == "audio":
        return "audio/mpeg"
    return "image/jpeg"


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime):
        parsed = value if value.tzinfo else value.replace(tzinfo=None)
        return parsed.isoformat()
    return None
