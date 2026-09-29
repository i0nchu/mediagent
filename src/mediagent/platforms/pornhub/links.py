"""Strict Pornhub exact-video URL parsing and canonicalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


PORNHUB_HOSTS = frozenset({"pornhub.com", "www.pornhub.com", "m.pornhub.com"})
_VIEWKEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_QUERY_PATHS = frozenset({"/view_video.php", "/video/show"})


@dataclass(frozen=True)
class PornhubVideoLink:
    viewkey: str
    canonical_url: str
    normalized_url: str


def parse_exact_video_link(url: str) -> PornhubVideoLink | None:
    """Return one canonical exact-video identity or ``None`` for other pages."""

    try:
        parsed = urlparse(str(url or "").strip())
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").lower()
    if host not in PORNHUB_HOSTS:
        return None

    path = parsed.path.rstrip("/") or "/"
    viewkey: str | None = None
    if path in _QUERY_PATHS:
        values = parse_qs(parsed.query, keep_blank_values=False).get("viewkey") or []
        if len(values) == 1:
            viewkey = values[0]
    elif path.startswith("/embed/"):
        parts = [part for part in path.split("/") if part]
        if len(parts) == 2 and parts[0] == "embed":
            viewkey = parts[1]
    if viewkey is None or _VIEWKEY_RE.fullmatch(viewkey) is None:
        return None

    canonical = urlunparse(
        (
            "https",
            "www.pornhub.com",
            "/view_video.php",
            "",
            urlencode({"viewkey": viewkey}),
            "",
        )
    )
    normalized = urlunparse(("https", host, parsed.path or "/", "", parsed.query, ""))
    return PornhubVideoLink(viewkey=viewkey, canonical_url=canonical, normalized_url=normalized)
