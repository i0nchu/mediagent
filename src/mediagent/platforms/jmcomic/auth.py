"""JMComic credentials and reusable session-cookie persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core.auth import read_credential_json, resolve_credential_path, write_credential_json


USERNAME_ENV = "MEDIAGENT_JMCOMIC_USERNAME"
PASSWORD_ENV = "MEDIAGENT_JMCOMIC_PASSWORD"
SESSION_FILE_ENV = "MEDIAGENT_JMCOMIC_SESSION_FILE"
_COMPAT_USERNAME_ENVS = ("JMCOMIC_USERNAME", "18COMIC_USERNAME", "19COMIC_USERNAME")
_COMPAT_PASSWORD_ENVS = ("JMCOMIC_PASSWORD", "18COMIC_PASSWORD", "19COMIC_PASSWORD")
_COMPAT_SESSION_ENVS = ("JMCOMIC_SESSION_FILE", "18COMIC_SESSION_FILE", "19COMIC_SESSION_FILE")


class JMComicAuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JMComicConfig:
    username: str | None
    password: str | None
    session_file: str | None

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "username_present": bool(self.username),
            "password_present": bool(self.password),
            "session_file": self.session_file,
        }


@dataclass(frozen=True)
class JMComicSession:
    cookies: dict[str, str]
    username: str | None = None
    updated_at: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.cookies)


def load_config(*, env: Any, cwd: Path) -> JMComicConfig:
    session_file = _first(env, (SESSION_FILE_ENV, *_COMPAT_SESSION_ENVS))
    if not session_file and env.get("MEDIAGENT_DATA_DIR"):
        session_file = str(
            resolve_credential_path(
                "${MEDIAGENT_DATA_DIR}/credentials/jmcomic_session.json",
                env=env,
                cwd=cwd,
            )
        )
    return JMComicConfig(
        username=_first(env, (USERNAME_ENV, *_COMPAT_USERNAME_ENVS)),
        password=_first(env, (PASSWORD_ENV, *_COMPAT_PASSWORD_ENVS)),
        session_file=session_file,
    )


def session_file_path(*, env: Any, cwd: Path, session_file: str | None = None) -> Path | None:
    value = session_file or load_config(env=env, cwd=cwd).session_file
    return resolve_credential_path(value, env=env, cwd=cwd) if value else None


def load_session(*, env: Any, cwd: Path, session_file: str | None = None) -> JMComicSession:
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is None or not path.exists():
        return JMComicSession({})
    try:
        data = read_credential_json(str(path), env=env, cwd=cwd)
    except (OSError, ValueError) as exc:
        raise JMComicAuthError("jmcomic_session_invalid", "JMComic session file is invalid.") from exc
    raw_cookies = data.get("cookies")
    if not isinstance(raw_cookies, dict):
        raise JMComicAuthError("jmcomic_session_invalid", "JMComic session file has no cookie jar.")
    cookies = {
        str(name): str(value)
        for name, value in raw_cookies.items()
        if _safe_cookie_part(name) and _safe_cookie_part(value)
    }
    return JMComicSession(
        cookies=cookies,
        username=str(data.get("username") or "").strip() or None,
        updated_at=str(data.get("updated_at") or "").strip() or None,
    )


def save_session(
    session: JMComicSession,
    *,
    env: Any,
    cwd: Path,
    session_file: str | None = None,
) -> Path:
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is None:
        raise JMComicAuthError("jmcomic_session_path_missing", "JMComic session file is not configured.")
    cookies = {
        str(name): str(value)
        for name, value in session.cookies.items()
        if _safe_cookie_part(name) and _safe_cookie_part(value)
    }
    if not cookies:
        raise JMComicAuthError("jmcomic_session_invalid", "Refusing to persist an empty JMComic cookie jar.")
    write_credential_json(
        str(path),
        {
            "version": 1,
            "provider": "jmcomic",
            "username": session.username,
            "cookies": cookies,
            "updated_at": datetime.now(UTC).isoformat(),
        },
        env=env,
        cwd=cwd,
    )
    return path


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(
        f"{name}={value}"
        for name, value in sorted(cookies.items())
        if _safe_cookie_part(name) and _safe_cookie_part(value)
    )


def _first(env: Any, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return None


def _safe_cookie_part(value: Any) -> bool:
    text = str(value)
    return bool(text) and not any(character in text for character in "\r\n;")
