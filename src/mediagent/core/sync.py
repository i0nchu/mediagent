"""Deterministic sync helpers shared by platform sync tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MEDIA_ITEM_STATUSES = (
    "discovered",
    "queued",
    "downloading",
    "downloaded",
    "partial",
    "failed",
    "skipped",
)

TERMINAL_ITEM_STATUSES = {"downloaded", "failed", "skipped"}


def item_status_from_file_counts(*, total: int, downloaded: int, failed: int, skipped: int = 0) -> str:
    if total <= 0:
        return "failed"
    if downloaded == total:
        return "downloaded"
    if downloaded > 0:
        return "partial"
    if failed > 0:
        return "failed"
    if skipped == total:
        return "skipped"
    return "queued"


def build_media_target_path(*, target_dir: Path, item: dict[str, Any], file_info: dict[str, Any]) -> Path:
    platform = safe_path_segment(item.get("platform") or "unknown-platform")
    author = safe_path_segment(item.get("author_id") or item.get("author_name") or "unknown-author")
    remote_id = safe_path_segment(item["remote_id"])
    title = safe_path_segment((item.get("metadata") or {}).get("title") or "untitled")
    page = int(file_info.get("page", 0) or 0)
    extension = extension_from_url(str(file_info.get("url") or ""))
    work_dir = target_dir / platform / author / f"{remote_id}_{title}"
    return (work_dir / f"{remote_id}_p{page}{extension}").resolve()


def sidecar_metadata_path(media_path: Path) -> Path:
    return media_path.with_suffix(".json")


def safe_path_segment(value: Any, *, max_length: int = 96) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._ ")
    if not text:
        return "unknown"
    return text[:max_length]


def extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix and len(suffix) <= 10:
        return suffix
    return ".bin"
