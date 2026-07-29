"""HTTP client abstraction for downloader tools."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes
    url: str | None = None

    def text(self) -> str:
        return self.content.decode("utf-8")


class UrllibHttpClient:
    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        request = Request(url, headers={"User-Agent": "mediagent/0.1.0", **(headers or {})})
        return self._open(request, timeout=timeout)

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        request = Request(
            url,
            headers={"User-Agent": "mediagent/0.1.0", **(headers or {})},
        )
        return self._open(request, timeout=timeout)

    def head(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        request = Request(
            url,
            method="HEAD",
            headers={"User-Agent": "mediagent/0.1.0", **(headers or {})},
        )
        return self._open_no_redirect(request, timeout=timeout, max_bytes=0)

    def get_limited(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_bytes: int = 1024 * 1024,
    ) -> HttpResponse:
        request = Request(url, headers={"User-Agent": "mediagent/0.1.0", **(headers or {})})
        return self._open_no_redirect(request, timeout=timeout, max_bytes=max_bytes)

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        encoded = urlencode(data).encode("utf-8")
        request = Request(
            url,
            data=encoded,
            method="POST",
            headers={
                "User-Agent": "mediagent/0.1.0",
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
        )
        return self._open(request, timeout=timeout)

    def _open(self, request: Request, *, timeout: float) -> HttpResponse:
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    content=response.read(),
                    url=response.url,
                )
        except HTTPError as exc:
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                content=exc.read(),
                url=exc.url,
            )

    def _open_no_redirect(
        self,
        request: Request,
        *,
        timeout: float,
        max_bytes: int,
    ) -> HttpResponse:
        opener = build_opener(_NoRedirectHandler)
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    content=response.read(max_bytes) if max_bytes > 0 else b"",
                    url=response.url,
                )
        except HTTPError as exc:
            content = exc.read(max_bytes) if max_bytes > 0 else b""
            return HttpResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                content=content,
                url=exc.url,
            )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None
