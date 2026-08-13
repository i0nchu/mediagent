from __future__ import annotations

import unittest

from mediagent.platforms.jmcomic.links import JMComicLinkError, parse_jmcomic_link


class JMComicLinkTests(unittest.TestCase):
    def test_album_query_is_removed_and_scope_is_album_exact(self) -> None:
        link = parse_jmcomic_link("https://18comic.vip/album/624076/?series_sort=2")
        self.assertEqual(link.entity_type, "album")
        self.assertEqual(link.remote_id, "624076")
        self.assertEqual(link.canonical_url, "https://18comic.vip/album/624076/")
        self.assertEqual(link.provider_work_id, "album:624076")
        self.assertEqual(link.exact_scope, "album")

    def test_photo_link_remains_a_single_photo(self) -> None:
        link = parse_jmcomic_link("https://www.18comic.vip/photo/1459311/?foo=bar")
        self.assertEqual(link.provider_work_id, "photo:1459311")
        self.assertEqual(link.exact_scope, "photo")

    def test_trusted_cover_resolves_to_album(self) -> None:
        link = parse_jmcomic_link(
            "https://cdn-msp3.jmapiproxy2.cc/media/albums/624076_3x4.jpg"
        )
        self.assertEqual(link.entity_type, "cover")
        self.assertEqual(link.provider_work_id, "album:624076")
        self.assertEqual(link.canonical_url, "https://18comic.vip/album/624076/")

    def test_untrusted_cover_lookalike_is_rejected(self) -> None:
        with self.assertRaises(JMComicLinkError):
            parse_jmcomic_link("https://evil.example/media/albums/624076.jpg")

    def test_non_comic_site_path_is_rejected(self) -> None:
        with self.assertRaises(JMComicLinkError):
            parse_jmcomic_link("https://18comic.vip/video/123/")


if __name__ == "__main__":
    unittest.main()
