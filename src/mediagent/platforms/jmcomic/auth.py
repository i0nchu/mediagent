"""JMComic credentials and reusable session-cookie persistence."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediagent.core.auth import read_credential_json, resolve_credential_path, write_credential_json


USERNAME_ENV = "MEDIAGENT_JMCOMIC_USERNAME"
PASSWORD_ENV = "MEDIAGENT_JMCOMIC_PASSWORD"
SESSION_FILE_ENV = "MEDIAGENT_JMCOMIC_SESSION_FILE"
COOKIE_FILE_ENV = "MEDIAGENT_JMCOMIC_COOKIE_FILE"
_COMPAT_USERNAME_ENVS = ("JMCOMIC_USERNAME", "18COMIC_USERNAME", "19COMIC_USERNAME")
_COMPAT_PASSWORD_ENVS = ("JMCOMIC_PASSWORD", "18COMIC_PASSWORD", "19COMIC_PASSWORD")
_COMPAT_SESSION_ENVS = ("JMCOMIC_SESSION_FILE", "18COMIC_SESSION_FILE", "19COMIC_SESSION_FILE")
_COMPAT_COOKIE_ENVS = ("JMCOMIC_COOKIE_FILE", "18COMIC_COOKIE_FILE", "19COMIC_COOKIE_FILE")
_COOKIE_DOMAIN_SUFFIXES = ("18comic.vip", "cdngwc.net")


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
    session_file = _first(
        env,
        (COOKIE_FILE_ENV, *_COMPAT_COOKIE_ENVS, SESSION_FILE_ENV, *_COMPAT_SESSION_ENVS),
    )
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
    if _is_cookie_txt(path):
        raw_cookies = _read_netscape_cookie_file(path)
        data: dict[str, Any] = {}
    else:
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
    if _is_cookie_txt(path):
        return _write_netscape_cookie_file(path, cookies)
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


def _is_cookie_txt(path: Path) -> bool:
    return path.suffix.lower() in {".txt", ".cookies"}


def _read_netscape_cookie_file(path: Path) -> dict[str, str]:
    cookies: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise JMComicAuthError("jmcomic_session_invalid", "JMComic cookie.txt could not be read.") from exc
    for line in lines:
        text = line.strip()
        if not text:
            continue
        if text.startswith("#HttpOnly_"):
            text = text.removeprefix("#HttpOnly_")
        elif text.startswith("#"):
            continue
        fields = text.split("\t")
        if len(fields) != 7:
            continue
        domain, _include_subdomains, _path, _secure, _expires, name, value = fields
        normalized_domain = domain.lstrip(".").lower()
        if not _trusted_cookie_domain(normalized_domain):
            continue
        if _safe_cookie_part(name) and _safe_cookie_part(value):
            cookies[name] = value
    if not cookies:
        raise JMComicAuthError(
            "jmcomic_session_invalid",
            "JMComic cookie.txt contains no reusable cookies for a trusted JMComic domain.",
        )
    return cookies


def _write_netscape_cookie_file(path: Path, cookies: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    lines = ["# Netscape HTTP Cookie File", "# Managed by Mediagent; keep private."]
    for name, value in sorted(cookies.items()):
        if not _safe_cookie_part(name) or not _safe_cookie_part(value):
            continue
        lines.append("\t".join((".18comic.vip", "TRUE", "/", "TRUE", "0", name, value)))
    if len(lines) == 2:
        raise JMComicAuthError("jmcomic_session_invalid", "Refusing to persist an empty JMComic cookie.txt.")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    os.chmod(path, 0o600)
    return path


def _trusted_cookie_domain(domain: str) -> bool:
    return any(domain == suffix or domain.endswith(f".{suffix}") for suffix in _COOKIE_DOMAIN_SUFFIXES)
