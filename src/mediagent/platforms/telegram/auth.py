"""Telegram credential/session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediagent.core.auth import AuthSession, CredentialRef, CredentialSource
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path


TELEGRAM_API_ID_ENV = "TELEGRAM_API_ID"
TELEGRAM_API_HASH_ENV = "TELEGRAM_API_HASH"
TELEGRAM_PHONE_NUMBER_ENV = "TELEGRAM_PHONE_NUMBER"
TELEGRAM_SESSION_FILE_ENV = "TELEGRAM_SESSION_FILE"
DEFAULT_SESSION_FILENAME = "telegram.session"


@dataclass(frozen=True)
class TelegramConfig:
    api_id: int | None
    api_hash: str | None
    phone_number: str | None
    session_path: Path | None
    config_errors: tuple[str, ...] = ()

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "api_id_present": self.api_id is not None,
            "api_hash_present": bool(self.api_hash),
            "phone_number_present": bool(self.phone_number),
            "session_file": str(self.session_path) if self.session_path else None,
            "session_file_exists": self.session_path.exists() if self.session_path else False,
        }


def load_config(
    *,
    env: Any,
    cwd: Path,
    data_dir: Path | None,
) -> TelegramConfig:
    errors: list[str] = []
    api_id = _api_id(env.get(TELEGRAM_API_ID_ENV), errors)
    api_hash = env.get(TELEGRAM_API_HASH_ENV) or None
    session_path = _session_path(env=env, cwd=cwd, data_dir=data_dir, errors=errors)
    return TelegramConfig(
        api_id=api_id,
        api_hash=api_hash,
        phone_number=env.get(TELEGRAM_PHONE_NUMBER_ENV) or None,
        session_path=session_path,
        config_errors=tuple(errors),
    )


def missing_config(config: TelegramConfig) -> list[str]:
    missing = list(config.config_errors)
    if config.api_id is None and TELEGRAM_API_ID_ENV not in missing:
        missing.append(TELEGRAM_API_ID_ENV)
    if not config.api_hash:
        missing.append(TELEGRAM_API_HASH_ENV)
    if config.session_path is None:
        missing.append(f"{TELEGRAM_SESSION_FILE_ENV} or MEDIAGENT_DATA_DIR")
    return missing


def ensure_session_path_allowed(config: TelegramConfig, allowed_roots: list[Path]) -> None:
    if config.session_path is None:
        raise PathSafetyError("Telegram session path is not configured.")
    ensure_inside(config.session_path, allowed_roots)


def prepare_session_parent(config: TelegramConfig) -> None:
    if config.session_path is None:
        raise PathSafetyError("Telegram session path is not configured.")
    config.session_path.parent.mkdir(parents=True, exist_ok=True)


def secure_session_file(config: TelegramConfig) -> None:
    if config.session_path and config.session_path.exists():
        config.session_path.chmod(0o600)


def session_from_status(payload: dict[str, Any], *, config: TelegramConfig) -> AuthSession:
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    account_id = account.get("id")
    credential_refs = [
        CredentialRef(source=CredentialSource.ENV, name=TELEGRAM_API_ID_ENV),
        CredentialRef(source=CredentialSource.ENV, name=TELEGRAM_API_HASH_ENV),
    ]
    if config.session_path:
        credential_refs.append(CredentialRef(source=CredentialSource.FILE, name=str(config.session_path)))
    return AuthSession(
        provider="telegram",
        account_id=str(account_id) if account_id is not None else None,
        scopes=["user_session"],
        expires_at=None,
        refresh_available=False,
        status=str(payload.get("status") or "unknown"),
        credential_refs=credential_refs,
        metadata={
            "username": account.get("username"),
            "display_name": account.get("display_name"),
            "session_file_exists": config.session_path.exists() if config.session_path else False,
        },
    )


def _api_id(raw_value: Any, errors: list[str]) -> int | None:
    if raw_value in (None, ""):
        return None
    try:
        return int(str(raw_value))
    except ValueError:
        errors.append(f"{TELEGRAM_API_ID_ENV} must be an integer")
        return None


def _session_path(
    *,
    env: Any,
    cwd: Path,
    data_dir: Path | None,
    errors: list[str],
) -> Path | None:
    raw_path = env.get(TELEGRAM_SESSION_FILE_ENV)
    if raw_path:
        try:
            return normalize_path(str(raw_path), env=env, cwd=cwd)
        except PathSafetyError as exc:
            errors.append(str(exc))
            return None
    if data_dir:
        return (data_dir / "credentials" / DEFAULT_SESSION_FILENAME).expanduser().resolve()
    return None
