"""JMComic favorite-folder selectors and stable user-facing aliases."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from mediagent.platforms.jmcomic.links import SITE_HOSTS


ALL_FOLDER_ID = "0"
ALL_FOLDER_NAME = "all"
ALL_FOLDER_ALIASES = frozenset({"all", "default", "全部", "所有"})
FOLDERS_ENV = "MEDIAGENT_JMCOMIC_FAVORITE_FOLDERS"


class JMComicFavoriteFolderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JMComicFavoriteFolder:
    name: str
    folder_id: str
    canonical_url: str | None = None

    def public_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "folder_id": self.folder_id,
            "url": self.canonical_url,
        }


def parse_folder_locator(value: str) -> tuple[str, str | None]:
    """Return ``(folder_id, canonical_url)`` for an ID or trusted folder URL."""

    text = str(value or "").strip()
    if text.isdigit():
        return _valid_folder_id(text), None
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_locator_invalid",
            "JMComic favorite folder must be a numeric ID or trusted folder URL.",
        )
    if parsed.username or parsed.password:
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_locator_invalid",
            "JMComic favorite folder URL must not contain user information.",
        )
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in SITE_HOSTS or not parsed.path.endswith("/favorite/albums"):
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_locator_invalid",
            "JMComic favorite folder URL is not a trusted favorite/albums URL.",
        )
    values = parse_qs(parsed.query).get("folder") or []
    if len(values) != 1:
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_locator_invalid",
            "JMComic favorite folder URL must contain one folder query parameter.",
        )
    folder_id = _valid_folder_id(values[0])
    return folder_id, f"https://18comic.vip{parsed.path}?folder={folder_id}"


def normalize_folder_name(value: str) -> tuple[str, str]:
    name = " ".join(str(value or "").strip().split())
    if not name or len(name) > 200:
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_name_invalid",
            "JMComic favorite folder name must contain 1 to 200 characters.",
        )
    return name, name.casefold()


def all_folder() -> JMComicFavoriteFolder:
    return JMComicFavoriteFolder(ALL_FOLDER_NAME, ALL_FOLDER_ID)


def parse_remote_folder_list(values: object) -> list[JMComicFavoriteFolder]:
    if not isinstance(values, list):
        return []
    folders: list[JMComicFavoriteFolder] = []
    seen_ids: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            folder_id = _valid_folder_id(str(value.get("FID") or value.get("folder_id") or ""))
            name, _ = normalize_folder_name(str(value.get("name") or value.get("title") or ""))
        except JMComicFavoriteFolderError:
            continue
        if folder_id == ALL_FOLDER_ID or folder_id in seen_ids:
            continue
        seen_ids.add(folder_id)
        folders.append(JMComicFavoriteFolder(name, folder_id))
    return folders


def _valid_folder_id(value: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) < 0:
        raise JMComicFavoriteFolderError(
            "jmcomic_folder_id_invalid",
            "JMComic favorite folder ID must be a non-negative integer.",
        )
    return str(int(text))
