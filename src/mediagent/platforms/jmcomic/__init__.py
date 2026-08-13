"""JMComic/18comic comic-source adapter.

The module intentionally exposes album, photo, and favorite primitives only.
Download scheduling, persistence, and follow policy remain owned by core/tools.
"""

from mediagent.platforms.jmcomic.client import JMComicClient, JMComicResolution
from mediagent.platforms.jmcomic.links import JMComicLink, parse_jmcomic_link
from mediagent.platforms.jmcomic.parser import (
    JMComicAlbum,
    JMComicFavoriteCollection,
    JMComicFavoritePage,
    JMComicPhoto,
)

__all__ = [
    "JMComicAlbum",
    "JMComicClient",
    "JMComicFavoriteCollection",
    "JMComicFavoritePage",
    "JMComicLink",
    "JMComicPhoto",
    "JMComicResolution",
    "parse_jmcomic_link",
]
