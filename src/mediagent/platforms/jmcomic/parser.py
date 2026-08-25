"""Normalize JMComic API payloads into comic-only adapter entities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class JMComicPayloadError(ValueError):
    """Raised when a provider response cannot identify a comic entity."""


@dataclass(frozen=True)
class JMComicEpisode:
    photo_id: str
    number: int
    title: str
    number_source: str = "provider_sort"
    position: int = 1


@dataclass(frozen=True)
class JMComicAlbum:
    album_id: str
    title: str
    episodes: tuple[JMComicEpisode, ...]
    authors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    description: str | None = None
    cover_url: str | None = None

    @property
    def provider_work_id(self) -> str:
        return f"album:{self.album_id}"

    @property
    def is_one_shot(self) -> bool:
        return len(self.episodes) == 1

    def comic_metadata(self) -> dict[str, Any]:
        return {
            "provider": "jmcomic",
            "provider_work_id": self.provider_work_id,
            "title": self.title,
            "series_id": self.album_id,
            "series_title": self.title,
            "total_count": len(self.episodes),
            "is_one_shot": self.is_one_shot,
            "summary": self.description,
        }


@dataclass(frozen=True)
class JMComicPage:
    index: int
    filename: str
    download_url: str
    scramble_id: str | None = None


@dataclass(frozen=True)
class JMComicPhoto:
    photo_id: str
    album_id: str
    title: str
    number: int
    pages: tuple[JMComicPage, ...]
    number_source: str = "photo_series"
    tags: tuple[str, ...] = ()

    @property
    def provider_work_id(self) -> str:
        return f"photo:{self.photo_id}"

    def comic_metadata(
        self,
        *,
        album_title: str | None = None,
        total_count: int | None = None,
        album_scoped: bool = False,
        chapter_number: int | str | None = None,
        chapter_number_source: str | None = None,
        provider_chapter_number: int | None = None,
        album_position: int | None = None,
        chapter_collision_index: int | None = None,
    ) -> dict[str, Any]:
        # JM albums may gain chapters later. A stable series identity prevents
        # the first chapter's archive from moving when favorites follow it.
        is_one_shot = False if album_scoped else self.album_id == self.photo_id
        effective_number = self.number if chapter_number is None else chapter_number
        result = {
            "provider": "jmcomic",
            "provider_work_id": self.provider_work_id,
            "title": self.title,
            "series_id": self.album_id,
            "series_title": album_title or self.title,
            "directory_title": f"JM {self.album_id}",
            "archive_title": f"Chapter {effective_number}",
            "chapter_number": effective_number,
            "chapter_number_source": chapter_number_source or self.number_source,
            "provider_chapter_number": provider_chapter_number or self.number,
            "total_count": total_count,
            "is_one_shot": is_one_shot,
        }
        if album_position is not None:
            result["album_position"] = album_position
        if chapter_collision_index is not None:
            result["chapter_collision_index"] = chapter_collision_index
        return result


@dataclass(frozen=True)
class JMComicFavorite:
    album_id: str
    title: str
    latest_photo_id: str | None = None
    latest_episode: str | None = None
    author: str | None = None
    cover_url: str | None = None

    @property
    def provider_work_id(self) -> str:
        return f"album:{self.album_id}"


@dataclass(frozen=True)
class JMComicFavoritePage:
    items: tuple[JMComicFavorite, ...]
    page: int
    total: int
    page_size: int
    folders: tuple[dict[str, str], ...] = ()

    @property
    def complete(self) -> bool:
        return self.page * self.page_size >= self.total


@dataclass(frozen=True)
class JMComicFavoriteCollection:
    items: tuple[JMComicFavorite, ...]
    total: int
    pages_fetched: int
    folder_id: str

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(item.provider_work_id for item in self.items)


def parse_album(payload: dict[str, Any]) -> JMComicAlbum:
    album_id = _required_id(payload.get("id") or payload.get("album_id"), "album")
    title = _title(payload.get("name") or payload.get("title"), fallback=f"JM{album_id}")
    series = payload.get("series") if isinstance(payload.get("series"), list) else []
    episodes = []
    for position, raw in enumerate(series, start=1):
        if not isinstance(raw, dict):
            continue
        photo_id = _optional_id(raw.get("id") or raw.get("photo_id"))
        if not photo_id:
            continue
        number = _positive_int(raw.get("sort"), fallback=position)
        episodes.append(
            JMComicEpisode(
                photo_id=photo_id,
                number=number,
                title=_title(raw.get("name") or raw.get("title"), fallback=f"Chapter {position}"),
                number_source="provider_sort" if _is_positive_int(raw.get("sort")) else "album_position",
                position=position,
            )
        )
    if not episodes:
        episodes.append(
            JMComicEpisode(
                photo_id=album_id,
                number=1,
                title=title,
                number_source="album_fallback",
                position=1,
            )
        )
    episodes.sort(key=lambda item: (item.number, item.position, int(item.photo_id)))
    return JMComicAlbum(
        album_id=album_id,
        title=title,
        episodes=tuple(episodes),
        authors=_strings(payload.get("author") or payload.get("authors")),
        tags=_strings(payload.get("tags")),
        description=_optional_text(payload.get("description")),
        cover_url=_cover_url(payload, album_id=album_id),
    )


def parse_photo(
    payload: dict[str, Any],
    *,
    image_domain: str | None = None,
    scramble_id: str | int | None = None,
) -> JMComicPhoto:
    photo_id = _required_id(payload.get("id") or payload.get("photo_id"), "photo")
    series_id = _optional_id(payload.get("series_id"))
    album_id = series_id if series_id and series_id != "0" else photo_id
    series = payload.get("series") if isinstance(payload.get("series"), list) else []
    number = 1
    number_source = "photo_fallback"
    for position, raw in enumerate(series, start=1):
        if isinstance(raw, dict) and _optional_id(raw.get("id")) == photo_id:
            number = _positive_int(raw.get("sort"), fallback=position)
            number_source = "photo_series" if _is_positive_int(raw.get("sort")) else "photo_series_position"
            break
    filenames = payload.get("images") or payload.get("page_arr") or []
    if isinstance(filenames, str):
        filenames = [filenames]
    if not isinstance(filenames, list):
        filenames = []
    domain = _normalize_image_domain(image_domain or payload.get("image_domain") or payload.get("data_original_domain"))
    pages = []
    for index, raw_filename in enumerate(filenames):
        filename = str(raw_filename or "").strip().rsplit("/", 1)[-1]
        if not filename:
            continue
        explicit_url = str(raw_filename) if str(raw_filename).startswith(("https://", "http://")) else None
        download_url = explicit_url or (f"https://{domain}/media/photos/{photo_id}/{filename}" if domain else "")
        pages.append(
            JMComicPage(
                index=index,
                filename=filename,
                download_url=download_url,
                scramble_id=str(scramble_id) if scramble_id is not None else None,
            )
        )
    return JMComicPhoto(
        photo_id=photo_id,
        album_id=album_id,
        title=_title(payload.get("name") or payload.get("title"), fallback=f"JM{photo_id}"),
        number=number,
        pages=tuple(pages),
        number_source=number_source,
        tags=_strings(payload.get("tags")),
    )


def parse_favorite_page(payload: dict[str, Any], *, page: int) -> JMComicFavoritePage:
    raw_items = payload.get("list") if isinstance(payload.get("list"), list) else []
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        album_id = _optional_id(raw.get("id") or raw.get("album_id"))
        if not album_id:
            continue
        items.append(
            JMComicFavorite(
                album_id=album_id,
                title=_title(raw.get("name") or raw.get("title"), fallback=f"JM{album_id}"),
                latest_photo_id=_optional_id(raw.get("latest_ep_aid")),
                latest_episode=_optional_text(raw.get("latest_ep")),
                author=_optional_text(raw.get("author")),
                cover_url=_cover_url(raw, album_id=album_id),
            )
        )
    folders = []
    for raw in payload.get("folder_list") or []:
        if isinstance(raw, dict):
            folders.append({str(key): str(value) for key, value in raw.items() if value is not None})
    total = _nonnegative_int(payload.get("total"), fallback=len(items))
    page_size = _positive_int(payload.get("count"), fallback=max(1, len(items)))
    return JMComicFavoritePage(tuple(items), max(1, int(page)), total, page_size, tuple(folders))


def _required_id(value: Any, kind: str) -> str:
    result = _optional_id(value)
    if not result:
        raise JMComicPayloadError(f"JMComic {kind} payload is missing a numeric ID.")
    return result


def _optional_id(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if text.isdigit() and int(text) > 0 else None


def _positive_int(value: Any, *, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if result > 0 else fallback


def _is_positive_int(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def _nonnegative_int(value: Any, *, fallback: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return fallback
    return result if result >= 0 else fallback


def _title(value: Any, *, fallback: str) -> str:
    return str(value or "").strip() or fallback


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_values = value.replace(",", " ").split()
    elif isinstance(value, list):
        raw_values = value
    else:
        raw_values = []
    return tuple(dict.fromkeys(str(item).strip() for item in raw_values if str(item).strip()))


def _normalize_image_domain(value: Any) -> str | None:
    text = str(value or "").strip().removeprefix("https://").removeprefix("http://").strip("/")
    return text or None


def _cover_url(payload: dict[str, Any], *, album_id: str) -> str | None:
    image = str(payload.get("image") or payload.get("cover") or "").strip()
    if image.startswith(("https://", "http://")):
        return image
    domain = _normalize_image_domain(payload.get("image_domain"))
    if domain:
        filename = image or f"{album_id}.jpg"
        return f"https://{domain}/media/albums/{filename}"
    return None
