"""X API client helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from mediagent.core.http import UrllibHttpClient
from mediagent.platforms.x.auth import ME_URL, _json_response


BOOKMARKS_URL_TEMPLATE = "https://api.x.com/2/users/{user_id}/bookmarks"


def get_me(
    *,
    http_client: Any,
    access_token: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.get_json(
        ME_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    return _json_response(response)


def get_bookmarks(
    *,
    http_client: Any,
    access_token: str,
    user_id: str,
    pagination_token: str | None = None,
    max_results: int = 100,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    params = {
        "max_results": str(max_results),
        "expansions": "attachments.media_keys,author_id",
        "tweet.fields": "author_id,created_at,entities,public_metrics",
        "media.fields": "duration_ms,height,media_key,preview_image_url,type,url,variants,width",
        "user.fields": "id,name,username",
    }
    if pagination_token:
        params["pagination_token"] = pagination_token
    url = f"{BOOKMARKS_URL_TEMPLATE.format(user_id=user_id)}?{urlencode(params)}"
    client = http_client or UrllibHttpClient()
    response = client.get_json(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    return _json_response(response)
