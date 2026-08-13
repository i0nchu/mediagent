"""JMComic API transport and comic resolution facade."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urlencode

from mediagent.core.http import UrllibHttpClient
from mediagent.platforms.jmcomic import auth
from mediagent.platforms.jmcomic.codec import api_headers, decode_api_envelope
from mediagent.platforms.jmcomic.images import scramble_segment_count
from mediagent.platforms.jmcomic.links import JMComicLink, parse_jmcomic_link
from mediagent.platforms.jmcomic.parser import (
    JMComicAlbum,
    JMComicFavoriteCollection,
    JMComicFavoritePage,
    JMComicPhoto,
    parse_album,
    parse_favorite_page,
    parse_photo,
)


DEFAULT_API_BASE_URL = "https://www.cdngwc.net"
DEFAULT_IMAGE_DOMAIN = "cdn-msp.jmapiproxy2.cc"
_SCRAMBLE_RE = re.compile(r"(?:var\s+)?scramble_id\s*=\s*['\"]?(\d+)")


class JMComicClientError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class JMComicTransportResult:
    payload: Any
    cookies: dict[str, str]
    status_code: int = 200


@dataclass(frozen=True)
class JMComicResolution:
    link: JMComicLink
    album: JMComicAlbum | None
    photos: tuple[JMComicPhoto, ...]

    @property
    def policy(self) -> str:
        return "exact"

    def normalized_items(self) -> list[dict[str, Any]]:
        album = self.album
        total_count = len(album.episodes) if album else None
        album_title = album.title if album else None
        items = []
        for photo in self.photos:
            files = []
            for page in photo.pages:
                runtime: dict[str, Any] = {
                    "provider": "jmcomic",
                    "photo_id": photo.photo_id,
                    "filename": page.filename,
                }
                if page.scramble_id is not None:
                    runtime.update(
                        {
                            "scramble_id": page.scramble_id,
                            "vertical_segments": scramble_segment_count(
                                scramble_id=page.scramble_id,
                                photo_id=photo.photo_id,
                                filename=page.filename,
                            ),
                        }
                    )
                files.append(
                    {
                        "url": page.download_url,
                        "kind": "image",
                        "page": page.index,
                        "storage_category": "comic-pages",
                        "runtime_decode": runtime,
                    }
                )
            comic = photo.comic_metadata(
                album_title=album_title,
                total_count=total_count,
                album_scoped=album is not None,
            )
            comic["tags"] = list(dict.fromkeys([*(album.tags if album else ()), *photo.tags]))
            if album and album.description:
                comic["summary"] = album.description
            items.append(
                {
                    "platform": "jmcomic",
                    "remote_id": photo.provider_work_id,
                    "media_type": "photo",
                    "source_url": f"https://18comic.vip/photo/{photo.photo_id}/",
                    "author_name": ", ".join(album.authors) if album and album.authors else None,
                    "source_availability": "available",
                    "status": "discovered",
                    "metadata": {
                        "title": photo.title,
                        "work_type": "comic",
                        "storage_category": "comic-pages",
                        "page_count": len(files),
                        "tags": list(photo.tags),
                        "comic": comic,
                        "files": files,
                        "jmcomic": {
                            "entity_type": "photo",
                            "photo_id": photo.photo_id,
                            "album_id": photo.album_id,
                            "chapter_number": photo.number,
                        },
                    },
                }
            )
        return items


class JMComicApiTransport:
    """Small replaceable HTTP boundary for the undocumented mobile API."""

    def __init__(
        self,
        *,
        http_client: Any | None = None,
        base_url: str = DEFAULT_API_BASE_URL,
        timeout: float = 30.0,
        clock: Any = time.time,
        decrypt: Any | None = None,
    ) -> None:
        self.http_client = http_client or UrllibHttpClient()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.clock = clock
        self.decrypt = decrypt

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> JMComicTransportResult:
        timestamp = int(self.clock())
        headers = api_headers(timestamp, content_endpoint=path == "/chapter_view_template")
        cookie_value = auth.cookie_header(cookies or {})
        if cookie_value:
            headers["Cookie"] = cookie_value
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.base_url}{path}{'?' + query if query else ''}"
        if method.upper() == "POST":
            response = self.http_client.post_form(
                url,
                {str(key): str(value) for key, value in (data or {}).items()},
                headers=headers,
                timeout=self.timeout,
            )
        else:
            response = self.http_client.get(url, headers=headers, timeout=self.timeout)
        if response.status_code in {401, 403}:
            raise JMComicClientError("jmcomic_auth_required", "JMComic session is missing or expired.", status_code=response.status_code)
        if response.status_code == 429:
            raise JMComicClientError("jmcomic_rate_limited", "JMComic API rate limit was reached.", status_code=429)
        if not 200 <= response.status_code < 300:
            raise JMComicClientError("jmcomic_request_failed", "JMComic API request failed.", status_code=response.status_code)
        response_cookies = {**(cookies or {}), **_response_cookies(response.headers)}
        if path == "/chapter_view_template":
            text = response.content.decode("utf-8", errors="replace")
            match = _SCRAMBLE_RE.search(text)
            payload: Any = {"scramble_id": match.group(1)} if match else {}
        else:
            try:
                payload = decode_api_envelope(response.content, timestamp=timestamp, decrypt=self.decrypt)
            except ValueError as exc:
                raise JMComicClientError("jmcomic_response_invalid", str(exc), status_code=response.status_code) from exc
        return JMComicTransportResult(payload, response_cookies, response.status_code)


class JMComicClient:
    def __init__(
        self,
        transport: Any,
        *,
        session: auth.JMComicSession | None = None,
        image_domain: str = DEFAULT_IMAGE_DOMAIN,
    ) -> None:
        self.transport = transport
        self.session = session or auth.JMComicSession({})
        self.image_domain = image_domain

    def login(self, *, username: str, password: str) -> auth.JMComicSession:
        if not username or not password:
            raise JMComicClientError("jmcomic_login_required", "JMComic username and password are required.")
        result = self._request("/login", method="POST", data={"username": username, "password": password})
        payload = result.payload if isinstance(result.payload, dict) else {}
        cookies = dict(result.cookies)
        if payload.get("s"):
            cookies["AVS"] = str(payload["s"])
        if not cookies:
            raise JMComicClientError("jmcomic_login_failed", "JMComic login did not issue a reusable session.")
        self.session = auth.JMComicSession(cookies, username=username)
        return self.session

    def get_album(self, album_id: str | int) -> JMComicAlbum:
        result = self._request("/album", params={"id": _numeric_id(album_id)})
        return parse_album(_mapping(result.payload, "album"))

    def get_photo(self, photo_id: str | int, *, scramble_id: str | None = None) -> JMComicPhoto:
        remote_id = _numeric_id(photo_id)
        result = self._request("/chapter", params={"id": remote_id})
        actual_scramble_id = scramble_id or self.get_scramble_id(remote_id)
        return parse_photo(
            _mapping(result.payload, "photo"),
            image_domain=self.image_domain,
            scramble_id=actual_scramble_id,
        )

    def get_scramble_id(self, photo_id: str | int) -> str:
        remote_id = _numeric_id(photo_id)
        result = self._request(
            "/chapter_view_template",
            params={"id": remote_id, "mode": "vertical", "page": "0", "app_img_shunt": "1", "express": "off"},
        )
        payload = _mapping(result.payload, "scramble")
        value = str(payload.get("scramble_id") or "220980")
        return value if value.isdigit() else "220980"

    def get_favorites_page(self, *, page: int = 1, folder_id: str = "0", order: str = "mr") -> JMComicFavoritePage:
        result = self._request("/favorite", params={"page": max(1, page), "folder_id": folder_id, "o": order})
        return parse_favorite_page(_mapping(result.payload, "favorites"), page=max(1, page))

    def favorite_target_ids(self, *, page: int = 1, folder_id: str = "0") -> tuple[str, ...]:
        favorite_page = self.get_favorites_page(page=page, folder_id=folder_id)
        return tuple(item.provider_work_id for item in favorite_page.items)

    def collect_favorites(
        self,
        *,
        folder_id: str = "0",
        order: str = "mr",
        max_pages: int = 100,
    ) -> JMComicFavoriteCollection:
        """Collect a complete album snapshot or fail without endorsing partial state."""
        if max_pages < 1:
            raise JMComicClientError("jmcomic_collection_invalid", "JMComic max_pages must be positive.")
        items = []
        seen_ids: set[str] = set()
        expected_total: int | None = None
        for page_number in range(1, max_pages + 1):
            page = self.get_favorites_page(page=page_number, folder_id=folder_id, order=order)
            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                raise JMComicClientError(
                    "jmcomic_collection_incomplete",
                    "JMComic favorite count changed while collecting the snapshot.",
                )
            new_on_page = 0
            for item in page.items:
                if item.album_id in seen_ids:
                    continue
                seen_ids.add(item.album_id)
                items.append(item)
                new_on_page += 1
            if len(items) >= (expected_total or 0):
                return JMComicFavoriteCollection(tuple(items), expected_total or 0, page_number, folder_id)
            if not page.items or new_on_page == 0:
                break
        raise JMComicClientError(
            "jmcomic_collection_incomplete",
            "JMComic favorite pagination ended before the declared total was collected.",
        )

    def resolve_exact(self, url: str) -> JMComicResolution:
        link = parse_jmcomic_link(url)
        if link.exact_scope == "photo":
            photo = self.get_photo(link.remote_id)
            return JMComicResolution(link=link, album=None, photos=(photo,))
        album = self.get_album(link.remote_id)
        # The provider uses one scramble threshold for every chapter in an album.
        # Fetching it once avoids an extra undocumented API request per chapter.
        scramble_id = self.get_scramble_id(album.episodes[0].photo_id)
        photos = tuple(
            self.get_photo(episode.photo_id, scramble_id=scramble_id)
            for episode in album.episodes
        )
        return JMComicResolution(link=link, album=album, photos=photos)

    def _request(self, path: str, **kwargs: Any) -> JMComicTransportResult:
        try:
            result = self.transport.request(path, cookies=self.session.cookies, **kwargs)
        except JMComicClientError:
            raise
        except Exception as exc:
            raise JMComicClientError("jmcomic_request_failed", "JMComic API transport failed.") from exc
        if isinstance(result, JMComicTransportResult):
            normalized = result
        elif isinstance(result, dict) and "payload" in result:
            normalized = JMComicTransportResult(
                payload=result.get("payload"),
                cookies={str(k): str(v) for k, v in (result.get("cookies") or {}).items()},
                status_code=int(result.get("status_code") or 200),
            )
        else:
            normalized = JMComicTransportResult(result, dict(self.session.cookies))
        if normalized.cookies != self.session.cookies:
            self.session = auth.JMComicSession(normalized.cookies, username=self.session.username)
        return normalized


def _mapping(value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise JMComicClientError("jmcomic_response_invalid", f"JMComic {kind} response is not an object.")
    return value


def _numeric_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise JMComicClientError("jmcomic_id_invalid", "JMComic ID must be a positive integer.")
    return text


def _response_cookies(headers: dict[str, str]) -> dict[str, str]:
    raw = next((value for name, value in headers.items() if name.lower() == "set-cookie"), "")
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}
