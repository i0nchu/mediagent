"""Reddit API client helpers."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from mediagent.core.http import UrllibHttpClient
from mediagent.platforms.reddit import auth as reddit_auth


def get_me(
    *,
    http_client: Any,
    access_token: str,
    user_agent: str,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    response = client.get_json(
        reddit_auth.ME_URL,
        headers=reddit_auth.bearer_headers(access_token=access_token, user_agent=user_agent),
        timeout=timeout,
    )
    return reddit_auth._json_response(response)


def get_saved(
    *,
    http_client: Any,
    access_token: str,
    user_agent: str,
    username: str,
    after: str | None = None,
    limit: int = 100,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
    client = http_client or UrllibHttpClient()
    query = {
        "raw_json": "1",
        "limit": str(max(1, min(100, int(limit)))),
    }
    if after:
        query["after"] = after
    url = f"{reddit_auth.OAUTH_BASE_URL}/user/{quote(username)}/saved?{urlencode(query)}"
    response = client.get_json(
        url,
        headers=reddit_auth.bearer_headers(access_token=access_token, user_agent=user_agent),
        timeout=timeout,
    )
    return reddit_auth._json_response(response)

