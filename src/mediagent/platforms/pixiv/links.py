"""Pixiv explicit artwork-link resolution helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mediagent.core.auth import resolve_credential_path
from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.redaction import redact_secrets
from mediagent.core.storage import extension_from_mime, safe_storage_segment
from mediagent.platforms.pixiv import auth as pixiv_auth
from mediagent.platforms.pixiv import client as pixiv_client
from mediagent.platforms.pixiv import parser as pixiv_parser


PIXIV_HOSTS = {"pixiv.net", "www.pixiv.net"}


class PixivLinkError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.__cause__ = cause

    def public_details(self) -> dict[str, Any]:
        return pixiv_error_details(self.code, self.details)


def pixiv_artwork_id(value: str) -> str | None:
    text = str(value or "").strip()
    if text.isdigit():
        return text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host and host not in PIXIV_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "artworks" and index + 1 < len(parts) and parts[index + 1].isdigit():
            return parts[index + 1]
    values = parse_qs(parsed.query).get("illust_id", [])
    return values[0] if values and values[0].isdigit() else None


def pixiv_canonical_artwork_url(illust_id: str) -> str:
    return f"https://www.pixiv.net/artworks/{safe_storage_segment(illust_id, max_length=64)}"


def pixiv_localized_artwork_url(illust_id: str) -> str:
    return f"https://www.pixiv.net/en/artworks/{safe_storage_segment(illust_id, max_length=64)}"


def resolve_artwork_from_url(
    value: str,
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None = None,
    allowed_write_roots: tuple[Path, ...] | list[Path] | None = None,
    allow_credential_write: bool = False,
    timeout: float = 30.0,
    include_ugoira_metadata: bool = True,
) -> dict[str, Any]:
    illust_id = pixiv_artwork_id(value)
    if not illust_id:
        raise PixivLinkError(
            "pixiv_artwork_unsupported_url",
            "Pixiv URL is not a supported artwork URL.",
            details={"reason": "missing_artwork_id"},
        )
    _ensure_credential_file_allowed(env=env, cwd=cwd, allowed_write_roots=allowed_write_roots)
    credentials = pixiv_auth.load_credentials(env=env, cwd=cwd)
    access_token = _ensure_access_token(
        env=env,
        cwd=cwd,
        http_client=http_client,
        credentials=credentials,
        allowed_write_roots=allowed_write_roots,
        allow_credential_write=allow_credential_write,
        timeout=timeout,
    )
    payload, rate_limit, status_code = pixiv_client.get_illust_detail(
        http_client=http_client,
        access_token=access_token,
        illust_id=illust_id,
        timeout=timeout,
    )
    if status_code == 429:
        raise PixivLinkError(
            "pixiv_rate_limited",
            "Pixiv artwork detail endpoint is rate limited.",
            details={"status_code": status_code, "rate_limit": rate_limit},
        )
    if status_code in (401, 403):
        raise PixivLinkError(
            "pixiv_auth_failed",
            "Pixiv credentials cannot access this artwork.",
            details={"status_code": status_code, "response": redact_secrets(payload)},
        )
    if status_code == 404:
        raise PixivLinkError(
            "pixiv_artwork_not_found",
            "Pixiv artwork was not found.",
            details={"status_code": status_code},
        )
    if status_code != 200:
        raise PixivLinkError(
            "pixiv_artwork_resolve_failed",
            "Pixiv artwork detail request failed.",
            details={"status_code": status_code, "response": redact_secrets(payload)},
        )
    illust = payload.get("illust") if isinstance(payload.get("illust"), dict) else None
    if not illust:
        raise PixivLinkError(
            "pixiv_artwork_resolve_failed",
            "Pixiv artwork detail response did not include an illust object.",
            details={"status_code": status_code, "reason": "missing_illust"},
        )
    ugoira_metadata = None
    if include_ugoira_metadata and illust.get("type") == "ugoira":
        metadata_payload, _, metadata_status = pixiv_client.get_ugoira_metadata(
            http_client=http_client,
            access_token=access_token,
            illust_id=illust_id,
            timeout=timeout,
        )
        if metadata_status == 200:
            ugoira_metadata = metadata_payload
    return pixiv_parser.parse_illust(illust, ugoira_metadata=ugoira_metadata)


def pixiv_error_details(code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    info = {
        "error_code": code,
        "recommended_tool": _recommended_tool(code),
        "retryable": code in {"pixiv_rate_limited", "pixiv_artwork_resolve_failed"},
        "user_action_required": code in {"pixiv_auth_missing_credentials", "pixiv_auth_refresh_failed", "pixiv_auth_failed"},
    }
    info.update(redact_secrets(details or {}))
    return info


def mime_type_for_file(file_info: dict[str, Any]) -> str | None:
    kind = str(file_info.get("kind") or "")
    if kind == "ugoira_zip":
        return "application/zip"
    extension = extension_for_file(file_info)
    if extension == ".png":
        return "image/png"
    if extension == ".webp":
        return "image/webp"
    if extension == ".gif":
        return "image/gif"
    if extension in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return None


def extension_for_file(file_info: dict[str, Any]) -> str:
    kind = str(file_info.get("kind") or "")
    if kind == "ugoira_zip":
        return ".zip"
    url = str(file_info.get("url") or "")
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix:
        return suffix
    mime_type = file_info.get("mime_type")
    if mime_type:
        return extension_from_mime(str(mime_type)) or ".bin"
    return ".bin"


def content_identity(item: dict[str, Any], file_info: dict[str, Any]) -> str:
    remote_id = safe_storage_segment(item.get("remote_id"), max_length=64)
    page = file_info.get("page", 0)
    kind = safe_storage_segment(file_info.get("kind") or "file", max_length=32)
    return f"pixiv:{remote_id}:{kind}:{page}"


def _ensure_access_token(
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None,
    credentials: dict[str, Any],
    allowed_write_roots: tuple[Path, ...] | list[Path] | None,
    allow_credential_write: bool,
    timeout: float,
) -> str:
    access_token = credentials.get("access_token")
    expires_at = str(credentials.get("expires_at")) if credentials.get("expires_at") else None
    if access_token and not pixiv_auth.is_expired(expires_at):
        return str(access_token)
    refresh_token = credentials.get("refresh_token")
    if not refresh_token:
        raise PixivLinkError(
            "pixiv_auth_missing_credentials",
            "Pixiv access token is missing or expired, and no refresh token is configured.",
            details={"recommended_tool": "pixiv.auth.login"},
        )
    payload, rate_limit, status_code = pixiv_auth.refresh_access_token(
        http_client=http_client,
        refresh_token_value=str(refresh_token),
        client_id=credentials.get("client_id") or env.get("PIXIV_CLIENT_ID"),
        client_secret=credentials.get("client_secret") or env.get("PIXIV_CLIENT_SECRET"),
        timeout=timeout,
    )
    if status_code == 429:
        raise PixivLinkError(
            "pixiv_rate_limited",
            "Pixiv token refresh is rate limited.",
            details={"status_code": status_code, "rate_limit": rate_limit},
        )
    if status_code != 200:
        raise PixivLinkError(
            "pixiv_auth_refresh_failed",
            "Pixiv token refresh failed before artwork resolution.",
            details={"status_code": status_code, "response": redact_secrets(payload)},
        )
    if allow_credential_write and credentials.get("credential_file"):
        credential_path = resolve_credential_path(str(credentials["credential_file"]), env=env, cwd=cwd)
        if allowed_write_roots is not None:
            ensure_inside(credential_path, list(allowed_write_roots))
        pixiv_auth.write_token_payload(str(credential_path), payload, env=env, cwd=cwd)
    token_data = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not token:
        raise PixivLinkError(
            "pixiv_access_token_missing",
            "Pixiv token refresh response did not include an access token.",
            details={"status_code": status_code},
        )
    return str(token)


def _ensure_credential_file_allowed(
    *,
    env: Any,
    cwd: Path,
    allowed_write_roots: tuple[Path, ...] | list[Path] | None,
) -> None:
    credential_file = env.get(pixiv_auth.PIXIV_CREDENTIALS_FILE_ENV)
    if not credential_file or allowed_write_roots is None:
        return
    path = resolve_credential_path(str(credential_file), env=env, cwd=cwd)
    try:
        ensure_inside(path, list(allowed_write_roots))
    except PathSafetyError as exc:
        raise PixivLinkError(
            "unsafe_credential_path",
            str(exc),
            details={"credential_file": str(path)},
            cause=exc,
        ) from exc


def _recommended_tool(code: str) -> str | None:
    if code == "pixiv_auth_missing_credentials":
        return "pixiv.auth.login"
    if code in {"pixiv_auth_refresh_failed", "pixiv_auth_failed"}:
        return "pixiv.auth.refresh"
    return None
