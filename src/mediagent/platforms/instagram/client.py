"""One-page Instagram saved-feed client boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mediagent.platforms.instagram import auth


def get_saved_page(
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None = None,
    session_file: str | None = None,
    cursor: str | None = None,
    amount: int = 50,
    timeout: float = 30.0,
) -> tuple[list[Any], str | None]:
    """Read exactly one saved-feed page and preserve its opaque cursor."""
    path = auth.session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is None or not path.exists():
        raise auth.InstagramPlatformError("instagram_session_missing", "Instagram saved session is missing.")
    try:
        if hasattr(http_client, "instagram_saved_page"):
            value = http_client.instagram_saved_page(
                session_file=str(path), cursor=cursor, amount=amount, timeout=timeout
            )
            if not isinstance(value, dict):
                raise ValueError("invalid saved-page payload")
            status = str(value.get("status") or "ok")
            if status not in {"ok", "success"}:
                code = str(value.get("error_code") or "instagram_saved_collect_failed")
                raise auth.InstagramPlatformError(code, "Instagram saved-feed request failed.")
            items = value.get("items") or []
            return list(items), str(value["next_cursor"]) if value.get("next_cursor") is not None else None

        from instagrapi import Client

        client = Client()
        client.request_timeout = timeout
        client.delay_range = [1, 3]
        client.load_settings(path)
        medias, next_cursor = client.collection_medias_v1_chunk("saved", max_id=cursor or "")
        return list(medias)[:amount], str(next_cursor) if next_cursor else None
    except auth.InstagramPlatformError:
        raise
    except Exception as exc:  # pragma: no cover - exercised through fake clients
        code = auth.classify_exception(exc, default_code="instagram_saved_collect_failed")
        raise auth.InstagramPlatformError(
            code,
            "Instagram saved-feed request failed.",
            details={"exception_type": type(exc).__name__},
            cause=exc,
        ) from exc
