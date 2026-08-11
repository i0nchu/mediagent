"""Instagram session helpers.

Instagram does not expose a stable public download API for this use case. This
module keeps the account/session behavior isolated from core tools so the
public tool contract can stay stable if the underlying client changes later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mediagent.core.auth import CredentialRef, CredentialSource, resolve_credential_path
from mediagent.core.redaction import redact_secrets


INSTAGRAM_ACCOUNT_ENV = "INSTAGRAM_ACCOUNT"
INSTAGRAM_SECRET_ENV = "INSTAGRAM_SECRET"
INSTAGRAM_SESSION_FILE_ENV = "INSTAGRAM_SESSION_FILE"
DEFAULT_LOGIN_COOLDOWN_SECONDS = 6 * 60 * 60

SESSION_REPAIR_CODES = {
    "instagram_session_missing",
    "instagram_session_invalid",
    "instagram_login_required",
}
USER_ACTION_REQUIRED_CODES = {
    "instagram_checkpoint_required",
    "instagram_two_factor_required",
}
RETRYABLE_CODES = {
    "instagram_rate_limited",
    "instagram_temporarily_blocked",
    "instagram_resolve_failed",
}


@dataclass(frozen=True)
class InstagramConfig:
    account: str | None
    secret: str | None
    session_file: str | None

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "account_present": bool(self.account),
            "secret_present": bool(self.secret),
            "session_file": self.session_file,
        }


class InstagramPlatformError(Exception):
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
        return instagram_error_details(self.code, self.details)


def load_config(*, env: Any, cwd: Path) -> InstagramConfig:
    session_file = env.get(INSTAGRAM_SESSION_FILE_ENV)
    if not session_file and env.get("MEDIAGENT_DATA_DIR"):
        session_file = str(
            resolve_credential_path(
                "${MEDIAGENT_DATA_DIR}/credentials/instagram_session.json",
                env=env,
                cwd=cwd,
            )
        )
    return InstagramConfig(
        account=env.get(INSTAGRAM_ACCOUNT_ENV) or None,
        secret=env.get(INSTAGRAM_SECRET_ENV) or None,
        session_file=session_file or None,
    )


def session_file_path(
    *,
    env: Any,
    cwd: Path,
    session_file: str | None = None,
) -> Path | None:
    value = session_file or load_config(env=env, cwd=cwd).session_file
    if not value:
        return None
    return resolve_credential_path(value, env=env, cwd=cwd)


def session_meta_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.meta.json")


def session_status(
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None = None,
    session_file: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    if hasattr(http_client, "instagram_auth_status"):
        value = http_client.instagram_auth_status(
            session_file=str(path) if path else None,
            timeout=timeout,
        )
        return _normalize_status_result(value, path=path)
    if path is None:
        return _status_dict(
            "missing",
            code="instagram_session_missing",
            session_file=None,
            session_file_exists=False,
        )
    if not path.exists():
        return _status_dict(
            "missing",
            code="instagram_session_missing",
            session_file=str(path),
            session_file_exists=False,
        )
    try:
        client = _load_client_from_session(path, timeout=timeout)
        account = client.account_info()
    except Exception as exc:  # pragma: no cover - covered through fake clients
        code = classify_exception(exc, default_code="instagram_session_invalid")
        return _status_dict(
            _status_from_code(code),
            code=code,
            session_file=str(path),
            session_file_exists=True,
            metadata={"exception_type": type(exc).__name__},
        )
    return _status_dict(
        "usable",
        code=None,
        account_id=str(getattr(account, "pk", "") or "") or None,
        session_file=str(path),
        session_file_exists=True,
    )


def login(
    *,
    env: Any,
    cwd: Path,
    http_client: Any | None = None,
    username: str | None = None,
    password: str | None = None,
    session_file: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    config = load_config(env=env, cwd=cwd)
    account = username or config.account
    secret = password or config.secret
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    missing = []
    if not account:
        missing.append(INSTAGRAM_ACCOUNT_ENV)
    if not secret:
        missing.append(INSTAGRAM_SECRET_ENV)
    if path is None:
        missing.append(INSTAGRAM_SESSION_FILE_ENV)
    if missing:
        raise InstagramPlatformError(
            "instagram_login_required",
            "Instagram username/password/session file configuration is missing.",
            details={"missing": missing},
        )
    if hasattr(http_client, "instagram_login"):
        value = http_client.instagram_login(
            username=account,
            password=secret,
            session_file=str(path),
            timeout=timeout,
        )
        return _normalize_status_result(value, path=path)
    try:
        client = _new_client(timeout=timeout)
        if path.exists():
            try:
                client.load_settings(path)
            except Exception:
                pass
        client.login(account, secret, relogin=True)
        _dump_settings_securely(client, path)
        account_info = client.account_info()
    except Exception as exc:  # pragma: no cover - covered through fake clients
        code = classify_exception(exc, default_code="instagram_login_required")
        raise InstagramPlatformError(
            code,
            "Instagram login failed.",
            details={"exception_type": type(exc).__name__},
            cause=exc,
        ) from exc
    return _status_dict(
        "usable",
        code=None,
        account_id=str(getattr(account_info, "pk", "") or "") or None,
        session_file=str(path),
        session_file_exists=True,
    )


def read_session_meta(path: Path) -> dict[str, Any]:
    import json

    meta_path = session_meta_path(path)
    if not meta_path.exists():
        return {}
    try:
        value = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_session_meta(path: Path, metadata: dict[str, Any]) -> Path:
    import json

    meta_path = session_meta_path(path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = meta_path.with_name(f".{meta_path.name}.tmp")
    temp_path.write_text(
        json.dumps(redact_secrets(metadata), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)
    temp_path.replace(meta_path)
    os.chmod(meta_path, 0o600)
    return meta_path


def should_attempt_login(
    *,
    metadata: dict[str, Any],
    now: datetime | None = None,
    cooldown_seconds: int = DEFAULT_LOGIN_COOLDOWN_SECONDS,
    force: bool = False,
) -> tuple[bool, str | None]:
    if force:
        return True, None
    attempted_at = _parse_datetime(metadata.get("last_login_attempt_at"))
    if attempted_at is None:
        return True, None
    current = now or datetime.now(UTC)
    next_attempt = attempted_at + timedelta(seconds=max(0, cooldown_seconds))
    if current >= next_attempt:
        return True, None
    return False, next_attempt.isoformat()


def instagram_error_details(code: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(details or {})
    payload["error_code"] = code
    payload["retryable"] = code in RETRYABLE_CODES
    payload["user_action_required"] = code in USER_ACTION_REQUIRED_CODES
    if code in SESSION_REPAIR_CODES:
        payload["recommended_tool"] = "instagram.auth.ensure_session"
    return payload


def classify_exception(exc: Exception, *, default_code: str) -> str:
    name = type(exc).__name__
    lowered = str(exc).lower()
    if name in {"TwoFactorRequired"} or "two-factor" in lowered or "two factor" in lowered:
        return "instagram_two_factor_required"
    if name in {
        "CaptchaChallengeRequired",
        "ChallengeError",
        "ChallengeRedirection",
        "ChallengeRequired",
        "ChallengeSelfieCaptcha",
        "ChallengeUnknownStep",
        "RecaptchaChallengeForm",
    } or "checkpoint" in lowered or "challenge" in lowered:
        return "instagram_checkpoint_required"
    if name in {"PleaseWaitFewMinutes", "RateLimitError", "ClientThrottledError"}:
        return "instagram_rate_limited"
    if name in {"FeedbackRequired", "SentryBlock"} or "temporarily blocked" in lowered:
        return "instagram_temporarily_blocked"
    if name in {"ClientLoginRequired", "LoginRequired"}:
        return "instagram_login_required"
    if name in {"BadCredentials", "BadPassword", "ClientUnauthorizedError"}:
        return "instagram_login_required"
    if name in {"MediaNotFound", "NotFoundError", "InvalidMediaId", "ClientNotFoundError"}:
        return "instagram_media_not_found"
    if name in {"PrivateAccount", "ClientForbiddenError"} or "private media" in lowered:
        return "instagram_media_private"
    if "media unavailable" in lowered or "not available" in lowered:
        return "instagram_media_unavailable"
    if name in {"MediaUnavailable", "PrivateAccount", "PrivateError", "ClientForbiddenError"}:
        return "instagram_media_private"
    if name in {"AlbumNotDownload", "MediaError", "PhotoNotUpload", "VideoNotDownload"}:
        return "instagram_media_unsupported"
    return default_code


def credential_refs(*, include_password: bool = False) -> list[CredentialRef]:
    refs = [
        CredentialRef(CredentialSource.ENV, INSTAGRAM_ACCOUNT_ENV),
        CredentialRef(CredentialSource.ENV, INSTAGRAM_SESSION_FILE_ENV),
    ]
    if include_password:
        refs.append(CredentialRef(CredentialSource.ENV, INSTAGRAM_SECRET_ENV))
    return refs


def _status_dict(
    status: str,
    *,
    code: str | None,
    session_file: str | None,
    session_file_exists: bool,
    account_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = {
        "provider": "instagram",
        "status": status,
        "account_id": account_id,
        "scopes": [],
        "expires_at": None,
        "refresh_available": False,
        "credential_refs": [ref.to_dict() for ref in credential_refs()],
        "metadata": redact_secrets(
            {
                "session_file": session_file,
                "session_file_exists": session_file_exists,
                **(metadata or {}),
            }
        ),
    }
    if code:
        data["error"] = instagram_error_details(code)
    return data


def _normalize_status_result(value: Any, *, path: Path | None) -> dict[str, Any]:
    if isinstance(value, dict):
        status = str(value.get("status") or "unknown")
        code = value.get("error_code") or value.get("code")
        return _status_dict(
            status,
            code=str(code) if code else None,
            account_id=str(value.get("account_id")) if value.get("account_id") else None,
            session_file=str(path) if path else value.get("session_file"),
            session_file_exists=bool(value.get("session_file_exists", path.exists() if path else False)),
            metadata=value.get("metadata") if isinstance(value.get("metadata"), dict) else {},
        )
    return _status_dict(
        "unknown",
        code="instagram_session_invalid",
        session_file=str(path) if path else None,
        session_file_exists=bool(path and path.exists()),
    )


def _status_from_code(code: str) -> str:
    if code == "instagram_session_missing":
        return "missing"
    if code in {"instagram_checkpoint_required", "instagram_two_factor_required"}:
        return "user_action_required"
    if code == "instagram_rate_limited":
        return "rate_limited"
    if code == "instagram_temporarily_blocked":
        return "temporarily_blocked"
    if code == "instagram_login_required":
        return "login_required"
    return "invalid"


def _new_client(*, timeout: float) -> Any:
    try:
        from instagrapi import Client
    except ImportError as exc:  # pragma: no cover - dependency is present in normal installs
        raise InstagramPlatformError(
            "instagram_resolve_failed",
            "The instagrapi dependency is required for Instagram support.",
            details={"missing_dependency": "instagrapi"},
            cause=exc,
        ) from exc
    client = Client()
    if hasattr(client, "request_timeout"):
        client.request_timeout = timeout
    if hasattr(client, "delay_range"):
        client.delay_range = [1, 3]
    return client


def _load_client_from_session(path: Path, *, timeout: float) -> Any:
    client = _new_client(timeout=timeout)
    client.load_settings(path)
    return client


def _dump_settings_securely(client: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    if temp_path.exists():
        temp_path.unlink()
    client.dump_settings(temp_path)
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    os.chmod(path, 0o600)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
