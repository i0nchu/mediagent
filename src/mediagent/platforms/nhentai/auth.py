"""Reusable nhentai session-cookie storage and refresh helpers."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mediagent.core.auth import read_credential_json, resolve_credential_path, write_credential_json
from mediagent.core.http import UrllibHttpClient


NHENTAI_SESSION_FILE_ENV = "MEDIAGENT_NHENTAI_SESSION_FILE"
NHENTAI_COOKIE_FILE_ENV = "MEDIAGENT_NHENTAI_COOKIE_FILE"
LEGACY_SESSION_FILE_ENV = "NHENTAI_SESSION_FILE"
LEGACY_COOKIE_FILE_ENV = "NHENTAI_COOKIE_FILE"
REFRESH_URL = "https://nhentai.net/auth/refresh"


class NhentaiAuthError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def session_file_path(*, env: Any, cwd: Path, session_file: str | None = None) -> Path | None:
    value = (
        session_file
        or env.get(NHENTAI_SESSION_FILE_ENV)
        or env.get(NHENTAI_COOKIE_FILE_ENV)
        or env.get(LEGACY_SESSION_FILE_ENV)
        or env.get(LEGACY_COOKIE_FILE_ENV)
    )
    if not value:
        return None
    return resolve_credential_path(value, env=env, cwd=cwd)


def load_session(*, env: Any, cwd: Path, session_file: str | None = None) -> dict[str, Any]:
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is None or not path.exists():
        raise NhentaiAuthError("nhentai_session_missing", "nhentai saved session is missing.")
    if _is_cookie_txt(path):
        session = {"cookies": _read_netscape_cookie_file(path)}
    else:
        session = read_credential_json(str(path), env=env, cwd=cwd)
    return normalize_session(session)


def save_session(
    session: dict[str, Any],
    *,
    env: Any,
    cwd: Path,
    session_file: str | None = None,
) -> Path:
    path = session_file_path(env=env, cwd=cwd, session_file=session_file)
    if path is None:
        raise NhentaiAuthError("nhentai_session_path_missing", "nhentai session file is not configured.")
    normalized = normalize_session(session)
    normalized["updated_at"] = datetime.now(UTC).isoformat()
    if _is_cookie_txt(path):
        return _write_netscape_cookie_file(path, normalized["cookies"])
    return write_credential_json(str(path), normalized, env=env, cwd=cwd)


def normalize_session(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise NhentaiAuthError("nhentai_session_invalid", "nhentai session must be a JSON object.")
    cookies = _normalize_cookies(value.get("cookies"))
    access_token = _text(value.get("access_token")) or _cookie_value(cookies, "access_token")
    if not cookies and not access_token:
        raise NhentaiAuthError("nhentai_session_invalid", "nhentai session contains no reusable credentials.")
    result = {
        "schema_version": 1,
        "cookies": cookies,
    }
    if access_token:
        result["access_token"] = access_token
    for key in ("account_id", "created_at", "updated_at"):
        if _text(value.get(key)):
            result[key] = _text(value.get(key))
    return result


def session_headers(session: dict[str, Any], *, url: str = "https://nhentai.net/") -> dict[str, str]:
    normalized = normalize_session(session)
    cookies = cookies_for_url(normalized["cookies"], url=url)
    headers = {
        "Accept": "application/json",
        "Referer": "https://nhentai.net/",
    }
    if cookies:
        headers["Cookie"] = "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)
    token = normalized.get("access_token")
    if token:
        headers["Authorization"] = f"User {token}"
    return headers


def cookies_for_url(cookies: list[dict[str, Any]], *, url: str) -> list[dict[str, Any]]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    now = datetime.now(UTC).timestamp()
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "nhentai.net").lstrip(".").lower()
        cookie_path = str(cookie.get("path") or "/")
        expires = cookie.get("expires")
        if expires is not None:
            try:
                if float(expires) <= now:
                    continue
            except (TypeError, ValueError):
                pass
        if host != domain and not host.endswith(f".{domain}"):
            continue
        if not path.startswith(cookie_path):
            continue
        if cookie.get("secure") and parsed.scheme != "https":
            continue
        result.append(cookie)
    return result


def refresh_session(
    *,
    http_client: Any | None,
    session: dict[str, Any],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Refresh and return a new in-memory session without exposing credentials."""

    current = normalize_session(session)
    client = http_client or UrllibHttpClient()
    response = client.post_form(
        REFRESH_URL,
        {},
        headers=session_headers(current, url=REFRESH_URL),
        timeout=timeout,
    )
    if response.status_code < 200 or response.status_code >= 300:
        code = "nhentai_auth_required" if response.status_code in {401, 403} else "nhentai_session_refresh_failed"
        raise NhentaiAuthError(code, f"nhentai session refresh failed with HTTP {response.status_code}.")
    payload = _json_object(response.content)
    refreshed = deepcopy(current)
    new_cookies = _set_cookie_values(response.headers)
    token = (
        _text(payload.get("access_token"))
        or _text(payload.get("token"))
        or _cookie_value(new_cookies, "access_token")
    )
    if new_cookies:
        refreshed["cookies"] = _merge_cookies(refreshed["cookies"], new_cookies)
    if token:
        refreshed["access_token"] = token
        refreshed["cookies"] = _merge_cookies(
            refreshed["cookies"],
            [{"name": "access_token", "value": token, "domain": "nhentai.net", "path": "/", "secure": True}],
        )
    refreshed["updated_at"] = datetime.now(UTC).isoformat()
    return normalize_session(refreshed)


def refresh_saved_session(
    *,
    http_client: Any | None,
    env: Any,
    cwd: Path,
    session_file: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    session = load_session(env=env, cwd=cwd, session_file=session_file)
    refreshed = refresh_session(http_client=http_client, session=session, timeout=timeout)
    path = save_session(refreshed, env=env, cwd=cwd, session_file=session_file)
    return {"status": "refreshed", "session_file": str(path), "credentials_written": True}


def _normalize_cookies(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = [{"name": name, "value": cookie_value} for name, cookie_value in value.items()]
    if not isinstance(value, list):
        return []
    result = []
    for cookie in value:
        if not isinstance(cookie, dict):
            continue
        name = _text(cookie.get("name"))
        cookie_value = _text(cookie.get("value"))
        if not name or cookie_value is None:
            continue
        normalized = {
            "name": name,
            "value": cookie_value,
            "domain": (_text(cookie.get("domain")) or "nhentai.net").lstrip("."),
            "path": _text(cookie.get("path")) or "/",
            "secure": bool(cookie.get("secure", True)),
        }
        expires = cookie.get("expires", cookie.get("expirationDate"))
        if expires is not None:
            normalized["expires"] = expires
        result.append(normalized)
    return result


def _set_cookie_values(headers: dict[str, Any]) -> list[dict[str, Any]]:
    raw = headers.get("Set-Cookie") or headers.get("set-cookie")
    values = raw if isinstance(raw, list) else [raw] if raw else []
    cookies = []
    for value in values:
        parsed = SimpleCookie()
        try:
            parsed.load(str(value))
        except Exception:
            continue
        for name, morsel in parsed.items():
            cookie = {
                "name": name,
                "value": morsel.value,
                "domain": morsel["domain"] or "nhentai.net",
                "path": morsel["path"] or "/",
                "secure": bool(morsel["secure"]),
            }
            cookies.append(cookie)
    return cookies


def _merge_cookies(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = {(cookie["name"], cookie.get("domain", "nhentai.net"), cookie.get("path", "/")): cookie for cookie in old}
    for cookie in new:
        key = (cookie["name"], cookie.get("domain", "nhentai.net"), cookie.get("path", "/"))
        merged[key] = cookie
    return list(merged.values())


def _cookie_value(cookies: list[dict[str, Any]], name: str) -> str | None:
    for cookie in cookies:
        if cookie["name"] == name:
            return cookie["value"]
    return None


def _json_object(content: bytes) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _is_cookie_txt(path: Path) -> bool:
    return path.suffix.lower() in {".txt", ".cookies"}


def _read_netscape_cookie_file(path: Path) -> list[dict[str, Any]]:
    cookies: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise NhentaiAuthError("nhentai_session_invalid", "nhentai cookie.txt could not be read.") from exc
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
        domain, _include_subdomains, cookie_path, secure, expires, name, value = fields
        normalized: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain.lstrip("."),
            "path": cookie_path or "/",
            "secure": secure.upper() == "TRUE",
        }
        try:
            expiry = int(expires)
        except ValueError:
            expiry = 0
        if expiry > 0:
            normalized["expires"] = expiry
        if name and value and normalized["domain"]:
            cookies.append(normalized)
    if not cookies:
        raise NhentaiAuthError("nhentai_session_invalid", "nhentai cookie.txt contains no reusable cookies.")
    return cookies


def _write_netscape_cookie_file(path: Path, cookies: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    lines = ["# Netscape HTTP Cookie File", "# Managed by Mediagent; keep private."]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "nhentai.net").lstrip(".")
        include_subdomains = "TRUE" if domain.endswith("nhentai.net") else "FALSE"
        cookie_path = str(cookie.get("path") or "/")
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = str(cookie.get("expires") or 0)
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or not value or any("\t" in part or "\n" in part or "\r" in part for part in (domain, cookie_path, name, value)):
            continue
        lines.append("\t".join((f".{domain}" if include_subdomains == "TRUE" else domain, include_subdomains, cookie_path, secure, expires, name, value)))
    if len(lines) == 2:
        raise NhentaiAuthError("nhentai_session_invalid", "Refusing to persist an empty nhentai cookie.txt.")
    temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temp_path, 0o600)
    temp_path.replace(path)
    os.chmod(path, 0o600)
    return path
