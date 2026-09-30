"""Bounded yt-dlp materialization for Pornhub exact-video links."""

from __future__ import annotations

import importlib
import os
import shutil
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from mediagent.core.filesystem import PathSafetyError, ensure_inside
from mediagent.core.links import URLSafetyError, validate_url_safety
from mediagent.core import library_content, local_import
from mediagent.platforms.pornhub.links import PornhubVideoLink, parse_exact_video_link


DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_MEDIA_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
YtDlpFactory = Callable[[dict[str, Any]], Any]
URLValidator = Callable[[str], Any]
_YTDLP_INIT_LOCK = threading.Lock()


class PornhubClientError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "network",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.details = details or {}


@dataclass(frozen=True)
class PornhubMaterializedVideo:
    viewkey: str
    canonical_url: str
    target_path: str
    title: str | None
    author: str | None
    description: str | None
    source_timestamp: str | None
    duration_seconds: float | None
    mime_type: str
    extension: str
    checksum: str
    size_bytes: int

    def metadata(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "source_timestamp": self.source_timestamp,
            "duration_seconds": self.duration_seconds,
        }


def probe_exact_video(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ytdlp_factory: YtDlpFactory | None = None,
    url_validator: URLValidator = validate_url_safety,
) -> dict[str, Any]:
    """Resolve safe, persistable metadata without downloading media bytes."""

    link = parse_exact_video_link(url)
    if link is None:
        raise PornhubClientError(
            "pornhub_url_unsupported",
            "Only exact Pornhub video links are supported.",
            category="validation",
        )
    _validate_limits(
        timeout_seconds=timeout_seconds,
        max_media_bytes=DEFAULT_MAX_MEDIA_BYTES,
        max_redirects=max_redirects,
    )
    factory = ytdlp_factory or _default_ytdlp_factory(url_validator, max_redirects=max_redirects)
    options = _ytdlp_options(
        staging=Path("."),
        timeout_seconds=timeout_seconds,
        max_media_bytes=DEFAULT_MAX_MEDIA_BYTES,
    )
    try:
        with factory(options) as ydl:
            info = _validate_info(
                ydl.extract_info(link.canonical_url, download=False),
                link=link,
                url_validator=url_validator,
            )
    except PornhubClientError:
        raise
    except Exception as exc:
        raise _mapped_ytdlp_error(exc) from exc
    return {
        "viewkey": link.viewkey,
        "canonical_url": link.canonical_url,
        "title": _bounded_text(info.get("title"), 500),
        "author": _bounded_text(info.get("uploader") or info.get("channel"), 300),
        "description": _bounded_text(info.get("description"), 4000),
        "source_timestamp": _source_timestamp(info),
        "duration_seconds": _positive_float(info.get("duration")),
    }


class _QuietLogger:
    """Prevent yt-dlp from writing provider payloads or signed URLs to output."""

    def debug(self, _message: str) -> None:
        return None

    def info(self, _message: str) -> None:
        return None

    def warning(self, _message: str) -> None:
        return None

    def error(self, _message: str) -> None:
        return None


def materialize_exact_video(
    url: str,
    *,
    target_path: Path,
    allowed_write_roots: list[Path],
    overwrite: bool = False,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ytdlp_factory: YtDlpFactory | None = None,
    url_validator: URLValidator = validate_url_safety,
) -> PornhubMaterializedVideo:
    link = parse_exact_video_link(url)
    if link is None:
        raise PornhubClientError(
            "pornhub_url_unsupported",
            "Only exact Pornhub video links are supported.",
            category="validation",
        )
    _validate_limits(
        timeout_seconds=timeout_seconds,
        max_media_bytes=max_media_bytes,
        max_redirects=max_redirects,
    )
    try:
        ensure_inside(target_path, allowed_write_roots)
    except PathSafetyError as exc:
        raise PornhubClientError(
            "unsafe_path",
            "Pornhub media target is outside configured library roots.",
            category="filesystem",
        ) from exc

    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging = target_path.parent / f".mediagent-pornhub-{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=0o700)
        downloaded, info = _download_to_staging(
            link,
            staging=staging,
            timeout_seconds=timeout_seconds,
            max_media_bytes=max_media_bytes,
            max_redirects=max_redirects,
            ytdlp_factory=ytdlp_factory,
            url_validator=url_validator,
        )
        detected = local_import.detect_media(downloaded)
        if detected.media_type != "video" or detected.extension != ".mp4":
            raise PornhubClientError(
                "pornhub_media_unsupported",
                "Pornhub extraction did not produce a supported MP4 video.",
                category="validation",
            )
        checksum, size_bytes = library_content.sha256_checksum(downloaded)
        if size_bytes <= 0:
            raise PornhubClientError(
                "pornhub_download_empty",
                "Pornhub extraction produced an empty file.",
            )
        if size_bytes > max_media_bytes:
            raise PornhubClientError(
                "pornhub_media_too_large",
                "Pornhub media exceeds the configured download limit.",
                details={"size_bytes": size_bytes, "max_media_bytes": max_media_bytes},
            )
        local_import.copy_verified(
            source=downloaded,
            target=target_path,
            library_root=_containing_root(target_path, allowed_write_roots),
            expected_checksum=checksum,
            expected_size=size_bytes,
            replace_existing_invalid=overwrite,
        )
        return PornhubMaterializedVideo(
            viewkey=link.viewkey,
            canonical_url=link.canonical_url,
            target_path=str(target_path),
            title=_bounded_text(info.get("title"), 500),
            author=_bounded_text(info.get("uploader") or info.get("channel"), 300),
            description=_bounded_text(info.get("description"), 4000),
            source_timestamp=_source_timestamp(info),
            duration_seconds=_positive_float(info.get("duration")),
            mime_type=detected.mime_type,
            extension=detected.extension,
            checksum=checksum,
            size_bytes=size_bytes,
        )
    except PornhubClientError:
        raise
    except (FileExistsError, OSError, ValueError) as exc:
        raise PornhubClientError(
            "pornhub_materialize_failed",
            "Pornhub media could not be published safely.",
            category="filesystem",
            details={"exception_type": type(exc).__name__},
        ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _download_to_staging(
    link: PornhubVideoLink,
    *,
    staging: Path,
    timeout_seconds: float,
    max_media_bytes: int,
    max_redirects: int,
    ytdlp_factory: YtDlpFactory | None,
    url_validator: URLValidator,
) -> tuple[Path, dict[str, Any]]:
    factory = ytdlp_factory or _default_ytdlp_factory(url_validator, max_redirects=max_redirects)
    options = _ytdlp_options(
        staging=staging,
        timeout_seconds=timeout_seconds,
        max_media_bytes=max_media_bytes,
        progress_hooks=[_media_size_progress_hook(staging, max_media_bytes)],
    )
    try:
        with factory(options) as ydl:
            info = ydl.extract_info(link.canonical_url, download=False)
            normalized = _validate_info(info, link=link, url_validator=url_validator)
            ydl.process_ie_result(info, download=True)
    except PornhubClientError:
        raise
    except Exception as exc:
        raise _mapped_ytdlp_error(exc) from exc

    files = []
    for candidate in staging.iterdir():
        try:
            mode = candidate.stat(follow_symlinks=False).st_mode
        except OSError as exc:
            raise PornhubClientError(
                "pornhub_download_invalid",
                "Pornhub extraction output could not be inspected safely.",
                category="filesystem",
            ) from exc
        if stat.S_ISLNK(mode):
            raise PornhubClientError(
                "pornhub_download_invalid",
                "Pornhub extraction output must not contain symbolic links.",
                category="filesystem",
            )
        if stat.S_ISREG(mode) and not candidate.name.endswith((".part", ".ytdl")):
            files.append(candidate)
    if len(files) != 1:
        raise PornhubClientError(
            "pornhub_download_invalid",
            "Pornhub exact-video extraction must produce one media file.",
            details={"files_produced": len(files)},
        )
    return files[0], normalized


def _default_ytdlp_factory(
    url_validator: URLValidator,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> YtDlpFactory:
    try:
        module = importlib.import_module("yt_dlp")
        requests_module = importlib.import_module("yt_dlp.networking._requests")
        curl_handler_module = importlib.import_module("yt_dlp.networking._curlcffi")
        curl_requests_module = importlib.import_module("curl_cffi.requests")
        network_exceptions = importlib.import_module("yt_dlp.networking.exceptions")
        globals_module = importlib.import_module("yt_dlp.globals")
    except ImportError as exc:
        raise PornhubClientError(
            "pornhub_backend_missing",
            "Pornhub exact-video support requires the optional video dependency.",
            category="validation",
            details={"missing_dependency": "yt-dlp", "install_extra": "video"},
        ) from exc

    class SafeRequestsRH(requests_module.RequestsRH):
        def _create_instance(self, cookiejar, legacy_ssl_support=None):
            session = super()._create_instance(
                cookiejar=cookiejar,
                legacy_ssl_support=legacy_ssl_support,
            )
            original_get_redirect_target = session.get_redirect_target
            session.max_redirects = max_redirects

            def safe_redirect_target(response):
                target = original_get_redirect_target(response)
                if target:
                    try:
                        _validate_transport_url(urljoin(response.url, target), url_validator)
                    except PornhubClientError:
                        response.close()
                        raise
                return target

            session.get_redirect_target = safe_redirect_target
            return session

    class SafeCurlSession(curl_requests_module.Session):
        def request(self, method, url, **kwargs):
            kwargs.pop("max_redirects", None)
            redirect_limit = max_redirects
            kwargs["allow_redirects"] = False
            current_method = method
            current_url = url
            current_data = kwargs.get("data")
            current_headers = dict(kwargs.get("headers") or {})
            for redirect_count in range(redirect_limit + 1):
                response = super().request(
                    current_method,
                    current_url,
                    **{**kwargs, "data": current_data, "headers": current_headers},
                )
                if response.status_code not in {301, 302, 303, 307, 308}:
                    return response
                location = response.headers.get("location")
                if not location or redirect_count >= redirect_limit:
                    return response
                next_url = urljoin(str(response.url), str(location))
                try:
                    _validate_transport_url(next_url, url_validator)
                except PornhubClientError as exc:
                    response.close()
                    raise network_exceptions.RequestError("pornhub_unsafe_media_url") from exc
                if response.status_code == 303 or (
                    response.status_code in {301, 302} and str(current_method).upper() == "POST"
                ):
                    current_method = "GET"
                    current_data = None
                    current_headers = {
                        key: value
                        for key, value in current_headers.items()
                        if str(key).casefold() not in {"content-type", "content-length"}
                    }
                current_origin = _url_origin(current_url)
                if _url_origin(next_url) != current_origin:
                    current_headers = {
                        key: value
                        for key, value in current_headers.items()
                        if str(key).casefold() not in {"authorization", "cookie"}
                    }
                response.close()
                current_url = next_url
            return response

    class SafeCurlCFFIRH(curl_handler_module.CurlCFFIRH):
        def _create_instance(self, cookiejar=None):
            return SafeCurlSession(cookies=cookiejar)

    class SafeYoutubeDL(module.YoutubeDL):
        def __init__(self, *args, **kwargs):
            # YoutubeDL loads user/system plugins before reading instance params.
            # Disable that global loader while constructing this isolated client.
            with _YTDLP_INIT_LOCK:
                if globals_module.plugin_ies.value or globals_module.plugin_pps.value:
                    raise PornhubClientError(
                        "pornhub_backend_unsafe",
                        "Pornhub extraction cannot run after external yt-dlp plugins were loaded.",
                        category="validation",
                    )
                prior_env = os.environ.get("YTDLP_NO_PLUGINS")
                prior_dirs = globals_module.plugin_dirs.value
                prior_loaded = globals_module.all_plugins_loaded.value
                os.environ["YTDLP_NO_PLUGINS"] = "1"
                globals_module.plugin_dirs.value = []
                try:
                    super().__init__(*args, **kwargs)
                finally:
                    globals_module.plugin_dirs.value = prior_dirs
                    globals_module.all_plugins_loaded.value = prior_loaded
                    if prior_env is None:
                        os.environ.pop("YTDLP_NO_PLUGINS", None)
                    else:
                        os.environ["YTDLP_NO_PLUGINS"] = prior_env

        def build_request_director(self, _handlers, preferences=None):
            return super().build_request_director(
                [SafeCurlCFFIRH, SafeRequestsRH],
                preferences,
            )

        def urlopen(self, request):
            url = request if isinstance(request, str) else getattr(request, "url", None) or request.get_full_url()
            _validate_transport_url(str(url), url_validator)
            return super().urlopen(request)

    return SafeYoutubeDL


def _ytdlp_options(
    *,
    staging: Path,
    timeout_seconds: float,
    max_media_bytes: int,
    progress_hooks: list[Callable[[dict[str, Any]], None]] | None = None,
) -> dict[str, Any]:
    options = {
        "cachedir": False,
        "continuedl": True,
        "extract_flat": False,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "fragment_retries": 3,
        "hls_prefer_native": True,
        "ignoreconfig": True,
        "ignoreerrors": False,
        "logger": _QuietLogger(),
        "max_filesize": max_media_bytes,
        "merge_output_format": "mp4",
        "no_warnings": True,
        "noplaylist": True,
        "nopart": False,
        "outtmpl": {"default": str(staging / "video.%(ext)s")},
        "overwrites": True,
        "playlistend": 1,
        "plugin_dirs": [],
        "progress_hooks": list(progress_hooks or []),
        "quiet": True,
        "retries": 3,
        "socket_timeout": timeout_seconds,
        "writedescription": False,
        "writeinfojson": False,
        "writesubtitles": False,
        "writethumbnail": False,
    }
    return options


def _media_size_progress_hook(staging: Path, max_media_bytes: int) -> Callable[[dict[str, Any]], None]:
    def enforce_limit(progress: dict[str, Any]) -> None:
        reported = progress.get("downloaded_bytes")
        try:
            reported_bytes = int(reported or 0)
        except (TypeError, ValueError):
            reported_bytes = 0
        disk_bytes = _regular_file_bytes(staging)
        if max(reported_bytes, disk_bytes) > max_media_bytes:
            raise PornhubClientError(
                "pornhub_media_too_large",
                "Pornhub media exceeds the configured download limit.",
                details={"max_media_bytes": max_media_bytes},
            )

    return enforce_limit


def _regular_file_bytes(root: Path) -> int:
    total = 0
    for candidate in root.rglob("*"):
        try:
            mode = candidate.stat(follow_symlinks=False).st_mode
            if stat.S_ISREG(mode):
                total += candidate.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total


def _validate_info(
    info: Any,
    *,
    link: PornhubVideoLink,
    url_validator: URLValidator,
) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise PornhubClientError(
            "pornhub_response_invalid",
            "Pornhub extraction returned an invalid response.",
        )
    if info.get("_type") in {"playlist", "multi_video"} or info.get("entries") is not None:
        raise PornhubClientError(
            "pornhub_playlist_unsupported",
            "Pornhub collections and playlists are not supported by exact-video intake.",
            category="validation",
        )
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").casefold()
    if "pornhub" not in extractor:
        raise PornhubClientError(
            "pornhub_response_invalid",
            "The exact-video URL resolved through an unexpected extractor.",
        )
    remote_id = str(info.get("id") or "")
    if remote_id != link.viewkey:
        raise PornhubClientError(
            "pornhub_identity_mismatch",
            "Pornhub extraction returned a different video identity.",
        )
    if info.get("is_live") is True:
        raise PornhubClientError(
            "pornhub_live_unsupported",
            "Live Pornhub streams are not supported.",
            category="validation",
        )
    for transport_url in _transport_urls(info):
        _validate_transport_url(transport_url, url_validator)
    return info


def _transport_urls(info: dict[str, Any]) -> list[str]:
    output: list[str] = []
    pending: list[Any] = [info]
    seen_objects: set[int] = set()
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            identity = id(value)
            if identity in seen_objects:
                continue
            seen_objects.add(identity)
            for key, item in value.items():
                lowered = str(key).casefold()
                if lowered in {"url", "manifest_url", "fragment_base_url"} and isinstance(item, str):
                    output.append(item)
                elif lowered in {"formats", "requested_formats", "requested_downloads", "fragments"}:
                    pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)
    return list(dict.fromkeys(output))


def _validate_transport_url(url: str, validator: URLValidator) -> None:
    try:
        validator(url)
    except (URLSafetyError, ValueError) as exc:
        raise PornhubClientError(
            "pornhub_unsafe_media_url",
            "Pornhub extraction returned an unsafe media URL.",
            details={"reason": getattr(exc, "reason", type(exc).__name__)},
        ) from exc


def _mapped_ytdlp_error(exc: Exception) -> PornhubClientError:
    if isinstance(exc, PornhubClientError):
        return exc
    text = str(exc).casefold()
    exception_type = type(exc).__name__
    if "pornhub_unsafe_media_url" in text:
        return PornhubClientError(
            "pornhub_unsafe_media_url",
            "Pornhub extraction returned an unsafe media URL.",
            details={"exception_type": exception_type},
        )
    if isinstance(exc, TimeoutError) or any(
        marker in text for marker in ("timed out", "timeout", "read operation timed out")
    ):
        return PornhubClientError(
            "pornhub_timeout",
            "Pornhub extraction timed out while waiting for network progress.",
            details={"exception_type": exception_type},
        )
    if any(marker in text for marker in ("private video", "premium", "login required", "sign in")):
        return PornhubClientError(
            "pornhub_auth_required",
            "Pornhub video requires authentication or premium access.",
            category="auth",
            details={"exception_type": exception_type},
        )
    if any(marker in text for marker in ("not available", "removed", "deleted", "does not exist")):
        return PornhubClientError(
            "pornhub_video_unavailable",
            "Pornhub video is unavailable or removed.",
            category="validation",
            details={"exception_type": exception_type},
        )
    if any(marker in text for marker in ("geo", "country", "region")):
        return PornhubClientError(
            "pornhub_geo_restricted",
            "Pornhub video is not available in this region.",
            category="validation",
            details={"exception_type": exception_type},
        )
    if any(marker in text for marker in ("too many requests", "rate limit", "http error 429")):
        return PornhubClientError(
            "pornhub_rate_limited",
            "Pornhub temporarily rate limited the request.",
            category="rate_limit",
            details={"exception_type": exception_type},
        )
    if "ffmpeg" in text:
        return PornhubClientError(
            "pornhub_ffmpeg_required",
            "Pornhub extraction requires ffmpeg to merge the selected streams.",
            category="validation",
            details={"exception_type": exception_type},
        )
    return PornhubClientError(
        "pornhub_download_failed",
        "Pornhub extraction failed.",
        details={"exception_type": exception_type},
    )


def _validate_limits(*, timeout_seconds: float, max_media_bytes: int, max_redirects: int) -> None:
    if not 1 <= float(timeout_seconds) <= 600:
        raise PornhubClientError(
            "pornhub_timeout_invalid",
            "Pornhub timeout must be between 1 and 600 seconds.",
            category="validation",
        )
    if not 1024 * 1024 <= int(max_media_bytes) <= 64 * 1024 * 1024 * 1024:
        raise PornhubClientError(
            "pornhub_size_limit_invalid",
            "Pornhub media limit must be between 1 MiB and 64 GiB.",
            category="validation",
        )
    if not 0 <= int(max_redirects) <= 10:
        raise PornhubClientError(
            "pornhub_redirect_limit_invalid",
            "Pornhub redirect limit must be between 0 and 10.",
            category="validation",
        )


def _containing_root(path: Path, roots: list[Path]) -> Path:
    resolved = path.resolve()
    matches = [root.resolve() for root in roots if resolved == root.resolve() or root.resolve() in resolved.parents]
    if not matches:
        raise PathSafetyError("Pornhub media target is outside configured library roots.")
    return max(matches, key=lambda root: len(root.parts))


def _bounded_text(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit] if text else None


def _positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _source_timestamp(info: dict[str, Any]) -> str | None:
    timestamp = info.get("timestamp") or info.get("release_timestamp")
    try:
        if timestamp is not None:
            from datetime import UTC, datetime

            return datetime.fromtimestamp(float(timestamp), tz=UTC).isoformat()
    except (OverflowError, TypeError, ValueError):
        pass
    upload_date = str(info.get("upload_date") or "")
    if len(upload_date) == 8 and upload_date.isdigit():
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}T00:00:00+00:00"
    return None


def _url_origin(url: str) -> tuple[str, str, int | None]:
    from urllib.parse import urlparse

    parsed = urlparse(str(url))
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port
