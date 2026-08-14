"""Canonical nhentai gallery-link parsing."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


NHENTAI_HOSTS = {"nhentai.net", "www.nhentai.net"}


@dataclass(frozen=True)
class NhentaiGalleryLink:
    gallery_id: str
    canonical_url: str


def parse_gallery_link(url: str) -> NhentaiGalleryLink | None:
    """Return the gallery identity for a canonicalizable ``/g/{id}`` URL."""

    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"}:
        return None
    if (parsed.hostname or "").lower() not in NHENTAI_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "g" or not parts[1].isdigit():
        return None
    gallery_id = str(int(parts[1]))
    if gallery_id == "0":
        return None
    return NhentaiGalleryLink(
        gallery_id=gallery_id,
        canonical_url=f"https://nhentai.net/g/{gallery_id}/",
    )


def canonical_gallery_url(value: str | int) -> str:
    gallery_id = str(value).strip()
    if not gallery_id.isdigit() or int(gallery_id) <= 0:
        raise ValueError("nhentai gallery ID must be a positive integer")
    return f"https://nhentai.net/g/{int(gallery_id)}/"
