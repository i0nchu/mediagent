"""Bounded FxEmbed v2 exact-post intake; account sessions stay on the endpoint."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from mediagent.core.http import UrllibHttpClient


X_HOSTS = frozenset(
    {
        "x.com",
        "www.x.com",
        "mobile.x.com",
        "twitter.com",
        "www.twitter.com",
        "mobile.twitter.com",
    }
)
PUBLIC_ENDPOINT = "https://api.fxtwitter.com/2"
MAX_METADATA_BYTES = 2 * 1024 * 1024
_POST_PATH = re.compile(
    r"^/(?:[A-Za-z0-9_]{1,15}|i/web|i)/status/([1-9][0-9]{0,19})(?:/(?:photo|video)/[1-4])?/?$"
)


class XEndpointError(ValueError):
    def __init__(
        self,
        code: str,
        *,
        category: str = "validation",
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.category = category
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class XPostLink:
    post_id: str
    canonical_url: str


def parse_post_link(url: str) -> XPostLink | None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in X_HOSTS
        or port not in {None, 443}
    ):
        return None
    if parsed.username or parsed.password or parsed.params:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if not match:
        return None
    post_id = match[1]
    return XPostLink(post_id, f"https://x.com/i/web/status/{post_id}")


def validate_media_url(url: str) -> str:
    """Allow native X CDN attachments only, including every download redirect."""
    from mediagent.core.links import URLSafetyError

    if not isinstance(url, str):
        raise URLSafetyError("x_media_url_unsafe", "X attachment URL is invalid.")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (ValueError, TypeError):
        raise URLSafetyError(
            "x_media_url_unsafe", "X attachment URL is invalid."
        ) from None
    valid = (
        parsed.scheme == "https"
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and not parsed.params
        and not parsed.fragment
        and not re.search(r"[\x00-\x20\\%]", parsed.path)
        and ".." not in parsed.path.split("/")
        and (
            (parsed.hostname == "pbs.twimg.com" and parsed.path.startswith("/media/"))
            or (parsed.hostname == "video.twimg.com" and parsed.path.endswith(".mp4"))
        )
    )
    if not valid:
        raise URLSafetyError(
            "x_media_url_unsafe", "X attachments must use native HTTPS media CDN URLs."
        )
    return url


def _endpoint(
    env: Mapping[str, str], host_resolver: Callable[[str], list[str]] | None
) -> tuple[str, str]:
    from mediagent.core.links import resolve_host_ips, validate_url_safety

    mode = env.get("MEDIAGENT_X_ENDPOINT_MODE", "public").strip()
    configured = env.get("MEDIAGENT_X_ENDPOINT_URL", "").strip()
    if mode == "public":
        if configured:
            raise XEndpointError("x_endpoint_config_invalid", retryable=True)
        validate_url_safety(PUBLIC_ENDPOINT, host_resolver=host_resolver)
        return mode, PUBLIC_ENDPOINT
    if mode != "self_hosted" or not configured:
        raise XEndpointError("x_endpoint_config_invalid", retryable=True)
    try:
        parsed = urlparse(configured)
        parsed.port
    except ValueError:
        raise XEndpointError("x_endpoint_config_invalid", retryable=True) from None
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.params
        or re.search(r"[\x00-\x20\\%]", configured)
        or ".." in parsed.path.split("/")
        or parsed.path.rstrip("/")
        not in {"/2", "/2/twitter", "/api/2", "/atmosphere/2/twitter"}
        or parsed.hostname in {"api.fxtwitter.com", "api.fixupx.com"}
    ):
        raise XEndpointError("x_endpoint_config_invalid", retryable=True)
    # The operator explicitly trusts this one service origin. Private addresses
    # are permitted here only, never in user input, media URLs, or redirects.
    ips = (host_resolver or resolve_host_ips)(parsed.hostname)
    if not ips:
        raise XEndpointError(
            "x_endpoint_unreachable", category="network", retryable=True
        )
    try:
        addresses = [ipaddress.ip_address(value) for value in ips]
    except ValueError:
        raise XEndpointError("x_endpoint_config_invalid", retryable=True) from None
    for address in addresses:
        private_service = (
            address.is_loopback
            or address in ipaddress.ip_network("10.0.0.0/8")
            or address in ipaddress.ip_network("172.16.0.0/12")
            or address in ipaddress.ip_network("192.168.0.0/16")
            or address in ipaddress.ip_network("fc00::/7")
        )
        if (
            address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (address.is_reserved and not address.is_loopback)
        ):
            raise XEndpointError("x_endpoint_config_invalid", retryable=True)
        if parsed.scheme == "http" and not private_service:
            raise XEndpointError("x_endpoint_config_invalid", retryable=True)
    return mode, configured.rstrip("/")


def resolve_post(
    link: XPostLink,
    *,
    env: Mapping[str, str],
    http_client: Any = None,
    host_resolver: Callable[[str], list[str]] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    from mediagent.core.links import URLSafetyError, clean_mime, header_value

    try:
        mode, endpoint = _endpoint(env, host_resolver)
        url = f"{endpoint}/status/{link.post_id}"
        client = http_client or UrllibHttpClient()
        response = client.get_limited(
            url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            timeout=min(max(timeout, 0.1), 30.0),
            max_bytes=MAX_METADATA_BYTES + 1,
        )
    except XEndpointError:
        raise
    except URLSafetyError:
        raise XEndpointError("x_endpoint_unsafe", retryable=True) from None
    except Exception:
        raise XEndpointError(
            "x_endpoint_unreachable", category="network", retryable=True
        ) from None
    # Never follow endpoint redirects, including redirects to another private
    # service. A failed private endpoint never falls back to a public endpoint.
    if response.url and response.url != url:
        raise XEndpointError("x_endpoint_redirect_rejected")
    if response.status_code in {301, 302, 303, 307, 308}:
        raise XEndpointError(
            "x_endpoint_redirect_rejected", status_code=response.status_code
        )
    _check_code(response.status_code)
    if len(response.content) > MAX_METADATA_BYTES:
        raise XEndpointError("x_metadata_too_large")
    if clean_mime(header_value(response.headers, "content-type")) != "application/json":
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    try:
        payload = json.loads(response.content)
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ValueError
        _check_code(payload["code"])
        return parse_post(payload, link, mode=mode)
    except XEndpointError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise XEndpointError(
            "x_payload_invalid", category="network", retryable=True
        ) from None


def _check_code(code: int) -> None:
    if code == 200:
        return
    if code in {401, 403}:
        raise XEndpointError(
            "x_post_requires_auth", category="auth", retryable=True, status_code=code
        )
    if code == 404:
        raise XEndpointError("x_post_unavailable", status_code=code)
    if code == 429:
        raise XEndpointError(
            "x_rate_limited", category="rate_limit", retryable=True, status_code=code
        )
    raise XEndpointError(
        "x_endpoint_failed", category="network", retryable=code >= 500, status_code=code
    )


def parse_post(
    payload: dict[str, Any], link: XPostLink, *, mode: str
) -> dict[str, Any]:
    """Whitelist post metadata and all native attachments; ignore quotes/cards."""
    post = payload.get("status")
    if not isinstance(post, dict) or str(post.get("id")) != link.post_id:
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    if post.get("type") == "tombstone":
        raise XEndpointError("x_post_unavailable")
    if post.get("type") != "status" or type(post.get("possibly_sensitive")) is not bool:
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    author = post.get("author")
    if not isinstance(author, dict) or type(author.get("protected")) is not bool:
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    if author["protected"] and mode != "self_hosted":
        raise XEndpointError("x_post_requires_auth", category="auth", retryable=True)
    media = post.get("media") or {}
    if not isinstance(media, dict):
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    entries = media.get("all")
    if entries is None:
        entries = (media.get("photos") or []) + (media.get("videos") or [])
    if not isinstance(entries, list) or len(entries) > 4:
        raise XEndpointError("x_payload_invalid", category="network", retryable=True)
    if not entries:
        raise XEndpointError("x_no_native_media")
    candidates = [
        _candidate(entry, link.post_id, index) for index, entry in enumerate(entries)
    ]
    return {
        "candidates": candidates,
        "metadata": {
            "provider": "fxembed_v2",
            "endpoint_mode": mode,
            "post_id": link.post_id,
            "author_id": _text(author.get("id"), 30),
            "author": _text(author.get("screen_name"), 100),
            "description": _text(post.get("text"), 4000),
            "source_protected": author["protected"],
            "possibly_sensitive": post.get("possibly_sensitive") is True,
            "source_timestamp": _timestamp(post.get("created_timestamp")),
        },
    }


def _text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _timestamp(value: Any) -> str | None:
    if type(value) not in {int, float} or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _candidate(entry: Any, post_id: str, index: int) -> dict[str, Any]:
    from mediagent.core.links import URLSafetyError

    if not isinstance(entry, dict) or entry.get("type") not in {
        "photo",
        "video",
        "gif",
    }:
        raise XEndpointError("x_media_unsupported")
    kind = "photo" if entry["type"] == "photo" else "video"
    url = entry.get("url")
    if kind == "video" and entry.get("formats"):
        if not isinstance(entry["formats"], list):
            raise XEndpointError("x_payload_invalid", retryable=True)
        variants = [
            v
            for v in entry["formats"]
            if isinstance(v, dict)
            and v.get("container") == "mp4"
            and v.get("codec", "h264") == "h264"
            and isinstance(v.get("url"), str)
        ]
        if not variants:
            raise XEndpointError("x_media_unsupported")
        url = max(variants, key=lambda v: _rank(v.get("bitrate"))).get("url")
    try:
        validate_media_url(url)
    except URLSafetyError:
        raise XEndpointError("x_media_url_unsafe") from None
    parsed = urlparse(url)
    if kind == "video" and parsed.hostname != "video.twimg.com":
        raise XEndpointError("x_media_unsupported")
    if kind == "photo":
        if parsed.hostname != "pbs.twimg.com":
            raise XEndpointError("x_media_unsupported")
        query = parse_qs(parsed.query)
        image_format = (query.get("format") or [parsed.path.rsplit(".", 1)[-1]])[
            0
        ].lower()
        mime = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(image_format)
        if not mime:
            raise XEndpointError("x_media_unsupported")
        url = urlunparse(
            (
                "https",
                "pbs.twimg.com",
                parsed.path,
                "",
                urlencode({"format": image_format, "name": "orig"}),
                "",
            )
        )
        extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[
            mime
        ]
    else:
        mime, extension = "video/mp4", ".mp4"
    return {
        "url": url,
        "media_type": kind,
        "mime_type": mime,
        "extension": extension,
        "file_index": index,
        "part": f"p{index}",
        "source": "x",
        "content_identity": f"x:{post_id}:{index}",
        "quality_rank": 0,
        "group_id": post_id,
        "required": True,
        "download_context": {"strategy": "x_cdn", "mime_type": mime},
    }


def _rank(value: Any) -> float:
    return (
        float(value)
        if type(value) in {int, float} and math.isfinite(value) and value >= 0
        else 0
    )


def valid_media_bytes(content: bytes, mime: str) -> bool:
    """Reject empty, truncated, or substituted HTML bodies before finalization."""
    if mime == "image/jpeg":
        return (
            len(content) >= 4
            and content.startswith(b"\xff\xd8\xff")
            and content.endswith(b"\xff\xd9")
        )
    if mime == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n") and content.endswith(
            b"IEND\xaeB`\x82"
        )
    if mime == "image/webp":
        return (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    if mime != "video/mp4":
        return False
    offset, boxes = 0, set()
    while offset < len(content):
        if len(content) - offset < 8:
            return False
        size = int.from_bytes(content[offset : offset + 4], "big")
        kind = content[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            header_size = 16
            if len(content) - offset < header_size:
                return False
            size = int.from_bytes(content[offset + 8 : offset + 16], "big")
        elif size == 0:
            size = len(content) - offset
        if size < header_size or offset + size > len(content):
            return False
        boxes.add(kind)
        offset += size
    return {b"ftyp", b"moov", b"mdat"} <= boxes
