"""Strict JMComic/18comic URL recognition and canonicalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlparse


SITE_HOSTS = frozenset({"18comic.vip", "www.18comic.vip"})
TRUSTED_COVER_HOSTS = frozenset(
    {
        "cdn-msp.jmapiproxy1.cc",
        "cdn-msp.jmapiproxy2.cc",
        "cdn-msp2.jmapiproxy2.cc",
        "cdn-msp3.jmapiproxy2.cc",
        "cdn-msp.jmapinodeudzn.net",
        "cdn-msp3.jmapinodeudzn.net",
    }
)
_ENTITY_RE = re.compile(r"^/(album|photo)/(\d+)(?:/.*)?$")
_COVER_RE = re.compile(r"^/media/albums/(\d+)(?:_[^/]*)?\.(?:jpe?g|png|webp)$", re.IGNORECASE)


class JMComicLinkError(ValueError):
    """Raised when a URL is not a supported, trusted JMComic comic URL."""


@dataclass(frozen=True)
class JMComicLink:
    entity_type: Literal["album", "photo", "cover"]
    remote_id: str
    canonical_url: str
    source_url: str

    @property
    def provider_work_id(self) -> str:
        kind = "album" if self.entity_type == "cover" else self.entity_type
        return f"{kind}:{self.remote_id}"

    @property
    def exact_scope(self) -> Literal["album", "photo"]:
        return "photo" if self.entity_type == "photo" else "album"


def parse_jmcomic_link(url: str) -> JMComicLink:
    parsed = urlparse(str(url).strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise JMComicLinkError("JMComic URL must use HTTP or HTTPS.")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path)

    if host in SITE_HOSTS:
        match = _ENTITY_RE.fullmatch(path)
        if not match:
            raise JMComicLinkError("JMComic URL is not a supported album or photo link.")
        entity_type, remote_id = match.groups()
        canonical = f"https://18comic.vip/{entity_type}/{remote_id}/"
        return JMComicLink(entity_type, remote_id, canonical, url)

    if host in TRUSTED_COVER_HOSTS:
        match = _COVER_RE.fullmatch(path)
        if not match:
            raise JMComicLinkError("Trusted JMComic CDN URL is not an album cover URL.")
        remote_id = match.group(1)
        return JMComicLink(
            "cover",
            remote_id,
            f"https://18comic.vip/album/{remote_id}/",
            url,
        )

    raise JMComicLinkError("URL host is not a trusted JMComic site or cover CDN.")
