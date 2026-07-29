"""Pixiv App API client helpers."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

from mediagent.core.http import UrllibHttpClient
from mediagent.core.rate_limit import extract_rate_limit


API_BASE = "https://app-api.pixiv.net"


def get_user_detail(
    *,
    http_client: Any,
    access_token: str,
    user_id: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    return _get_json(
        http_client=http_client,
        access_token=access_token,
        path="/v1/user/detail",
        params={"user_id": user_id},
        timeout=timeout,
    )


def get_user_bookmarks(
    *,
    http_client: Any,
    access_token: str,
    user_id: str,
    restrict: str = "public",
    max_bookmark_id: str | None = None,
    tag: str | None = None,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    params = {
        "user_id": user_id,
        "restrict": restrict,
    }
    if max_bookmark_id:
        params["max_bookmark_id"] = max_bookmark_id
    if tag:
        params["tag"] = tag
    return _get_json(
        http_client=http_client,
        access_token=access_token,
        path="/v1/user/bookmarks/illust",
        params=params,
        timeout=timeout,
    )


def get_ugoira_metadata(
    *,
    http_client: Any,
    access_token: str,
    illust_id: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    return _get_json(
        http_client=http_client,
        access_token=access_token,
        path="/v1/ugoira/metadata",
        params={"illust_id": illust_id},
        timeout=timeout,
    )


def _get_json(
    *,
    http_client: Any,
    access_token: str,
    path: str,
    params: dict[str, str],
    timeout: float,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    url = f"{API_BASE}{path}?{urlencode(params)}"
    response = client.get_json(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; mediagent)",
            "App-OS": "android",
            "App-OS-Version": "11",
            "App-Version": "5.0.234",
        },
        timeout=timeout,
    )
    try:
        payload = json.loads(response.content.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": response.content.decode("utf-8", errors="replace")}
    return payload, extract_rate_limit(response.headers), response.status_code
