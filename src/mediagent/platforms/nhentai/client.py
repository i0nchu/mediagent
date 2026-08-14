"""nhentai gallery and favorites API client helpers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from mediagent.core.http import UrllibHttpClient
from mediagent.platforms.nhentai import auth, parser
from mediagent.platforms.nhentai.links import parse_gallery_link


API_BASE = "https://nhentai.net/api/v2"


class NhentaiApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def get_gallery(
    gallery_id: str | int,
    *,
    http_client: Any | None = None,
    session: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    gallery_id = _gallery_id(gallery_id)
    return _get_json(
        f"{API_BASE}/galleries/{gallery_id}",
        http_client=http_client,
        session=session,
        timeout=timeout,
    )


def resolve_gallery(
    gallery_id: str | int,
    *,
    http_client: Any | None = None,
    session: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    return parser.parse_gallery(
        get_gallery(gallery_id, http_client=http_client, session=session, timeout=timeout)
    )


def resolve_exact(
    url: str,
    *,
    http_client: Any | None = None,
    session: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> list[dict[str, Any]]:
    """Resolve one direct gallery link to exactly one normalized comic item."""

    link = parse_gallery_link(url)
    if link is None:
        raise ValueError("URL is not a supported nhentai gallery link")
    return [
        resolve_gallery(
            link.gallery_id,
            http_client=http_client,
            session=session,
            timeout=timeout,
        )
    ]


def get_favorites_page(
    *,
    page: int,
    http_client: Any | None = None,
    session: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    if page <= 0:
        raise ValueError("favorites page must be positive")
    query = urlencode({"page": page})
    return _get_json(
        f"{API_BASE}/favorites?{query}",
        http_client=http_client,
        session=session,
        timeout=timeout,
        auth_required=True,
    )


def collect_favorites(
    *,
    http_client: Any | None = None,
    session: dict[str, Any],
    start_page: int = 1,
    max_pages: int | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Collect a complete, deduplicated favorites snapshot.

    ``complete`` is false when a caller intentionally bounds pagination. A
    consumer must not interpret that result as evidence that older favorites
    were removed.
    """

    if start_page <= 0:
        raise ValueError("favorites start page must be positive")
    if max_pages is not None and max_pages <= 0:
        raise ValueError("favorites max_pages must be positive")
    page = start_page
    pages_fetched = 0
    galleries: dict[str, dict[str, Any]] = {}
    targets: dict[str, dict[str, Any]] = {}
    expected_total: int | None = None
    while True:
        payload = get_favorites_page(
            page=page,
            http_client=http_client,
            session=session,
            timeout=timeout,
        )
        entries = parser.favorite_gallery_payloads(payload)
        for entry in entries:
            try:
                gallery_id = _gallery_id(entry.get("id"))
            except (TypeError, ValueError):
                continue
            galleries.setdefault(gallery_id, entry)
        for target in parser.favorite_targets(payload):
            targets.setdefault(target["provider_work_id"], target)
        pages_fetched += 1
        expected_total = _expected_total(payload) if expected_total is None else expected_total
        next_page = _next_page(payload, current_page=page, entries_count=len(entries), expected_total=expected_total)
        if next_page is None:
            return {
                "provider": "nhentai",
                "collection": "favorites",
                "target_policy": "exact",
                "complete": True,
                "pages_fetched": pages_fetched,
                "expected_total": expected_total,
                "galleries": list(galleries.values()),
                "targets": list(targets.values()),
                "next_page": None,
            }
        if max_pages is not None and pages_fetched >= max_pages:
            return {
                "provider": "nhentai",
                "collection": "favorites",
                "target_policy": "exact",
                "complete": False,
                "pages_fetched": pages_fetched,
                "expected_total": expected_total,
                "galleries": list(galleries.values()),
                "targets": list(targets.values()),
                "next_page": next_page,
            }
        page = next_page


def _get_json(
    url: str,
    *,
    http_client: Any | None,
    session: dict[str, Any] | None,
    timeout: float,
    auth_required: bool = False,
) -> dict[str, Any]:
    client = http_client or UrllibHttpClient()
    headers = {"Accept": "application/json", "Referer": "https://nhentai.net/"}
    if session:
        headers.update(auth.session_headers(session, url=url))
    response = client.get_json(url, headers=headers, timeout=timeout)
    if response.status_code in {401, 403}:
        code = "nhentai_auth_required" if auth_required else "nhentai_access_denied"
        raise NhentaiApiError(code, f"nhentai request failed with HTTP {response.status_code}.", status_code=response.status_code)
    if response.status_code == 404:
        raise NhentaiApiError("nhentai_gallery_unavailable", "nhentai gallery is unavailable.", status_code=404)
    if response.status_code == 429:
        raise NhentaiApiError("nhentai_rate_limited", "nhentai request was rate limited.", status_code=429)
    if response.status_code < 200 or response.status_code >= 300:
        raise NhentaiApiError("nhentai_request_failed", f"nhentai request failed with HTTP {response.status_code}.", status_code=response.status_code)
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NhentaiApiError("nhentai_response_invalid", "nhentai returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise NhentaiApiError("nhentai_response_invalid", "nhentai returned an invalid response envelope.")
    return payload


def _gallery_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError("nhentai gallery ID must be a positive integer")
    return str(int(text))


def _expected_total(payload: dict[str, Any]) -> int | None:
    candidates = [payload.get("total"), payload.get("count"), payload.get("num_results")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend((data.get("total"), data.get("count"), data.get("num_results")))
    for candidate in candidates:
        try:
            if candidate is not None and int(candidate) >= 0:
                return int(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _next_page(
    payload: dict[str, Any],
    *,
    current_page: int,
    entries_count: int,
    expected_total: int | None,
) -> int | None:
    envelope = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    value = envelope.get("next_page") if isinstance(envelope, dict) else None
    if value not in (None, "", False):
        try:
            page = int(value)
            return page if page > current_page else None
        except (TypeError, ValueError):
            pass
    has_next = envelope.get("has_next") if isinstance(envelope, dict) else None
    if has_next is True:
        return current_page + 1
    total_pages = None
    if isinstance(envelope, dict):
        total_pages = envelope.get("total_pages", envelope.get("num_pages"))
    try:
        if total_pages is not None and current_page < int(total_pages):
            return current_page + 1
    except (TypeError, ValueError):
        pass
    if expected_total is not None and expected_total > 0:
        page_size = envelope.get("per_page") if isinstance(envelope, dict) else None
        try:
            if page_size and current_page * int(page_size) < expected_total:
                return current_page + 1
        except (TypeError, ValueError):
            pass
    if has_next is False or entries_count == 0:
        return None
    return None
