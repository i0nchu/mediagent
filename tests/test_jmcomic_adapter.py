from __future__ import annotations

import unittest

from mediagent.platforms.jmcomic.auth import JMComicSession
from mediagent.platforms.jmcomic.client import JMComicClient, JMComicTransportResult
from mediagent.platforms.jmcomic.favorite_folders import (
    JMComicFavoriteFolderError,
    normalize_folder_name,
    parse_folder_locator,
    parse_remote_folder_list,
)
from mediagent.platforms.jmcomic.parser import parse_album, parse_favorite_page, parse_photo
from mediagent.core.comics import comic_archive_relative_path


ALBUM = {
    "id": "624076",
    "name": "Long Series",
    "author": ["Author A"],
    "description": "Summary",
    "tags": ["tag-a", "tag-b"],
    "series": [
        {"id": "624076", "sort": "1", "name": "Episode 1"},
        {"id": "1459311", "sort": "106", "name": "Episode 106"},
    ],
}

PHOTOS = {
    "624076": {
        "id": "624076",
        "name": "Episode 1",
        "series_id": "624076",
        "series": ALBUM["series"],
        "images": ["00001.jpg", "00002.webp"],
        "tags": "tag-a tag-b",
    },
    "1459311": {
        "id": "1459311",
        "name": "Episode 106",
        "series_id": "624076",
        "series": ALBUM["series"],
        "images": ["00106.webp"],
    },
}


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def request(self, path: str, **kwargs):
        self.calls.append((path, kwargs))
        if path == "/album":
            return JMComicTransportResult(ALBUM, kwargs.get("cookies") or {})
        if path == "/chapter":
            return JMComicTransportResult(PHOTOS[kwargs["params"]["id"]], kwargs.get("cookies") or {})
        if path == "/chapter_view_template":
            return JMComicTransportResult({"scramble_id": "220980"}, kwargs.get("cookies") or {})
        if path == "/favorite":
            return JMComicTransportResult(
                {
                    "list": [
                        {
                            "id": "624076",
                            "name": "Long Series",
                            "latest_ep": "Episode 106",
                            "latest_ep_aid": "1459311",
                        }
                    ],
                    "total": "1",
                    "count": 20,
                    "folder_list": [],
                },
                kwargs.get("cookies") or {},
            )
        if path == "/login":
            return JMComicTransportResult({"s": "issued-session"}, {"session": "cookie"})
        raise AssertionError(path)


class PagedFavoriteTransport(FakeTransport):
    def request(self, path: str, **kwargs):
        if path != "/favorite":
            return super().request(path, **kwargs)
        page = int(kwargs["params"]["page"])
        item_id = "624076" if page == 1 else "349717"
        return JMComicTransportResult(
            {
                "list": [{"id": item_id, "name": f"Album {item_id}"}],
                "total": "2",
                "count": 1,
            },
            kwargs.get("cookies") or {},
        )


class JMComicParserTests(unittest.TestCase):
    def test_favorite_folder_locator_accepts_id_and_trusted_folder_url(self) -> None:
        self.assertEqual(parse_folder_locator("1234567"), ("1234567", None))
        self.assertEqual(
            parse_folder_locator("https://18comic.vip/user/example/favorite/albums?folder=1234567"),
            (
                "1234567",
                "https://18comic.vip/user/example/favorite/albums?folder=1234567",
            ),
        )
        self.assertEqual(normalize_folder_name("  稍後  閱讀  "), ("稍後 閱讀", "稍後 閱讀"))

    def test_favorite_folder_locator_rejects_untrusted_or_ambiguous_urls(self) -> None:
        for value in (
            "https://example.test/user/a/favorite/albums?folder=1",
            "https://18comic.vip/album/1/?folder=2",
            "https://user:secret@18comic.vip/user/a/favorite/albums?folder=1",
            "https://18comic.vip/user/a/favorite/albums?folder=1&folder=2",
        ):
            with self.subTest(value=value), self.assertRaises(JMComicFavoriteFolderError):
                parse_folder_locator(value)

    def test_remote_favorite_folder_list_normalizes_and_deduplicates(self) -> None:
        folders = parse_remote_folder_list(
            [
                {"FID": "1234567", "UID": "private", "name": "稍後閱讀"},
                {"FID": "1234567", "name": "duplicate"},
                {"FID": "0", "name": "all"},
                {"FID": "bad", "name": "ignored"},
            ]
        )
        self.assertEqual([(folder.name, folder.folder_id) for folder in folders], [("稍後閱讀", "1234567")])

    def test_album_manifest_and_one_shot_fallback(self) -> None:
        album = parse_album(ALBUM)
        self.assertEqual([episode.photo_id for episode in album.episodes], ["624076", "1459311"])
        self.assertFalse(album.is_one_shot)
        one_shot = parse_album({"id": "349717", "name": "One Shot", "series": []})
        self.assertTrue(one_shot.is_one_shot)
        self.assertEqual(one_shot.episodes[0].photo_id, "349717")

    def test_photo_pages_are_ordered_and_use_series_identity(self) -> None:
        photo = parse_photo(PHOTOS["1459311"], image_domain="images.example", scramble_id="220980")
        self.assertEqual(photo.album_id, "624076")
        self.assertEqual(photo.number, 106)
        self.assertEqual(photo.pages[0].download_url, "https://images.example/media/photos/1459311/00106.webp")
        one_shot = parse_photo({"id": "349717", "name": "One Shot", "series_id": "0", "images": []})
        self.assertTrue(one_shot.comic_metadata()["is_one_shot"])

    def test_favorites_are_album_targets(self) -> None:
        page = parse_favorite_page(
            {
                "list": [{"id": "624076", "name": "Long Series", "latest_ep_aid": "1459311"}],
                "total": "1",
                "count": 20,
            },
            page=1,
        )
        self.assertEqual(page.items[0].provider_work_id, "album:624076")
        self.assertEqual(page.items[0].latest_photo_id, "1459311")
        self.assertTrue(page.complete)


class JMComicClientTests(unittest.TestCase):
    def test_album_exact_fetches_the_current_complete_manifest(self) -> None:
        transport = FakeTransport()
        resolution = JMComicClient(transport).resolve_exact(
            "https://18comic.vip/album/624076/?series_sort=1"
        )
        self.assertEqual(resolution.policy, "exact")
        self.assertEqual([photo.photo_id for photo in resolution.photos], ["624076", "1459311"])
        items = resolution.normalized_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["metadata"]["comic"]["series_id"], "624076")
        self.assertEqual(items[1]["metadata"]["comic"]["chapter_number"], 106)
        self.assertEqual(items[1]["metadata"]["comic"]["total_count"], 2)
        self.assertFalse(items[0]["metadata"]["comic"]["is_one_shot"])
        self.assertEqual(items[1]["metadata"]["files"][0]["page"], 0)
        self.assertIn("vertical_segments", items[1]["metadata"]["files"][0]["runtime_decode"])
        self.assertEqual([path for path, _ in transport.calls].count("/chapter_view_template"), 1)

    def test_album_manifest_overrides_lagging_photo_chapter_number(self) -> None:
        class LaggingTransport(FakeTransport):
            def request(self, path: str, **kwargs):
                if path == "/album":
                    return JMComicTransportResult(
                        {
                            "id": "1215913",
                            "name": "Growing Series",
                            "series": [{"id": "1463677", "sort": "74", "name": "CH74"}],
                        },
                        {},
                    )
                if path == "/chapter":
                    return JMComicTransportResult(
                        {
                            "id": "1463677",
                            "series_id": "1215913",
                            "name": "CH74",
                            # The photo payload does not contain itself yet, so
                            # the old resolver incorrectly fell back to CH1.
                            "series": [{"id": "1215913", "sort": "1", "name": "CH1"}],
                            "images": ["00001.webp"],
                        },
                        {},
                    )
                return super().request(path, **kwargs)

        item = JMComicClient(LaggingTransport()).resolve_exact(
            "https://18comic.vip/album/1215913/"
        ).normalized_items()[0]
        comic = item["metadata"]["comic"]
        self.assertEqual(comic["chapter_number"], 74)
        self.assertEqual(comic["provider_chapter_number"], 74)
        self.assertEqual(comic["chapter_number_source"], "album_provider_sort")
        self.assertEqual(comic["album_position"], 1)

    def test_duplicate_provider_chapter_numbers_get_stable_unique_numbers(self) -> None:
        class DuplicateTransport(FakeTransport):
            album = {
                "id": "777",
                "name": "Duplicate Series",
                "series": [
                    {"id": "7001", "sort": "55", "name": "CH55 A"},
                    {"id": "7002", "sort": "55", "name": "CH55 B"},
                ],
            }

            def request(self, path: str, **kwargs):
                if path == "/album":
                    return JMComicTransportResult(self.album, {})
                if path == "/chapter":
                    photo_id = str(kwargs["params"]["id"])
                    return JMComicTransportResult(
                        {
                            "id": photo_id,
                            "series_id": "777",
                            "name": f"Photo {photo_id}",
                            "series": self.album["series"],
                            "images": ["00001.webp"],
                        },
                        {},
                    )
                return super().request(path, **kwargs)

        items = JMComicClient(DuplicateTransport()).resolve_exact(
            "https://18comic.vip/album/777/"
        ).normalized_items()
        comics = [item["metadata"]["comic"] for item in items]
        self.assertEqual([comic["chapter_number"] for comic in comics], [55, "55.001"])
        self.assertEqual([comic["provider_chapter_number"] for comic in comics], [55, 55])
        self.assertEqual([comic["chapter_collision_index"] for comic in comics], [0, 1])
        self.assertEqual([comic["album_position"] for comic in comics], [1, 2])

    def test_one_chapter_album_keeps_stable_series_layout_for_future_follow(self) -> None:
        photo = parse_photo(
            {"id": "349717", "series_id": "349717", "name": "Only now", "images": ["1.jpg"]},
            image_domain="images.example",
            scramble_id="999999",
        )
        comic = photo.comic_metadata(album_title="Growing album", total_count=1, album_scoped=True)
        self.assertFalse(comic["is_one_shot"])
        self.assertEqual(comic["series_id"], "349717")

    def test_photo_then_album_metadata_keeps_the_same_archive_path(self) -> None:
        transport = FakeTransport()
        photo_item = JMComicClient(transport).resolve_exact(
            "https://18comic.vip/photo/1459311/"
        ).normalized_items()[0]
        album_item = JMComicClient(FakeTransport()).resolve_exact(
            "https://18comic.vip/album/624076/"
        ).normalized_items()[1]
        self.assertNotEqual(
            photo_item["metadata"]["comic"]["series_title"],
            album_item["metadata"]["comic"]["series_title"],
        )
        self.assertEqual(
            comic_archive_relative_path(item=photo_item, include_platform_layer=True),
            comic_archive_relative_path(item=album_item, include_platform_layer=True),
        )

    def test_photo_exact_does_not_expand_parent_album(self) -> None:
        transport = FakeTransport()
        resolution = JMComicClient(transport).resolve_exact("https://18comic.vip/photo/1459311/")
        self.assertIsNone(resolution.album)
        self.assertEqual([photo.photo_id for photo in resolution.photos], ["1459311"])
        self.assertNotIn("/album", [path for path, _ in transport.calls])

    def test_favorite_ids_are_album_targets_for_follow_policy(self) -> None:
        client = JMComicClient(FakeTransport(), session=JMComicSession({"session": "saved"}))
        self.assertEqual(client.favorite_target_ids(), ("album:624076",))

    def test_collect_favorites_requires_and_returns_a_complete_album_snapshot(self) -> None:
        collection = JMComicClient(PagedFavoriteTransport()).collect_favorites()
        self.assertEqual(collection.target_ids, ("album:624076", "album:349717"))
        self.assertEqual(collection.pages_fetched, 2)

    def test_collect_favorites_rejects_partial_pagination(self) -> None:
        with self.assertRaisesRegex(Exception, "ended before"):
            JMComicClient(PagedFavoriteTransport()).collect_favorites(max_pages=1)

    def test_login_updates_reusable_cookie_jar_without_exposing_password(self) -> None:
        client = JMComicClient(FakeTransport())
        session = client.login(username="account", password="secret")
        self.assertEqual(session.username, "account")
        self.assertEqual(session.cookies, {"session": "cookie", "AVS": "issued-session"})


if __name__ == "__main__":
    unittest.main()
