import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.links import (
    LinkSafetyPolicy,
    ResolveRequest,
    default_link_resolver_registry,
    normalize_url,
    resolution_to_media_item,
    validate_url_safety,
)
from mediagent.core.tooling import ToolContext
from mediagent.tools.defaults import create_default_registry
from tests.test_telegram_tools import FakeTelegramClient, _telegram_context


PUBLIC_TEST_IP = "1.1.1.1"


class FakeLinkHttpClient:
    def __init__(self) -> None:
        self.heads: dict[str, HttpResponse] = {}
        self.gets: dict[str, HttpResponse] = {}
        self.calls: list[tuple[str, str]] = []
        self.headers_by_call: list[tuple[str, str, dict[str, str]]] = []

    def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.calls.append(("HEAD", url))
        self.headers_by_call.append(("HEAD", url, dict(headers or {})))
        return self.heads[url]

    def get_limited(
        self,
        url: str,
        *,
        headers=None,
        timeout: float = 30.0,
        max_bytes: int = 1024 * 1024,
    ) -> HttpResponse:
        self.calls.append(("GET_LIMITED", url))
        self.headers_by_call.append(("GET_LIMITED", url, dict(headers or {})))
        response = self.gets[url]
        return HttpResponse(
            response.status_code,
            response.headers,
            response.content[:max_bytes],
            response.url,
        )

    def get(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.calls.append(("GET", url))
        self.headers_by_call.append(("GET", url, dict(headers or {})))
        return self.gets[url]


class CombinedTelegramLinkClient(FakeTelegramClient):
    def __init__(self, *, messages: dict[str, list[dict[str, Any]]], http: FakeLinkHttpClient) -> None:
        super().__init__(messages=messages)
        self.http = http

    def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        return self.http.head(url, headers=headers, timeout=timeout)

    def get_limited(
        self,
        url: str,
        *,
        headers=None,
        timeout: float = 30.0,
        max_bytes: int = 1024 * 1024,
    ) -> HttpResponse:
        return self.http.get_limited(url, headers=headers, timeout=timeout, max_bytes=max_bytes)

    def get(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        return self.http.get(url, headers=headers, timeout=timeout)


class LinkResolverTests(unittest.TestCase):
    def test_url_normalization_removes_fragment_and_preserves_query(self) -> None:
        normalized = normalize_url("HTTPS://Example.COM/Media/File.JPG?sig=abc#section")

        self.assertEqual(normalized, "https://example.com/Media/File.JPG?sig=abc")

    def test_safety_gate_rejects_unsafe_url_shapes(self) -> None:
        for url in (
            "http://example.com/file.jpg",
            "file:///tmp/file.jpg",
            "https://localhost/file.jpg",
            "https://127.0.0.1/file.jpg",
            "https://10.0.0.2/file.jpg",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    validate_url_safety(url)

    def test_safety_gate_rejects_userinfo_before_normalization(self) -> None:
        for url in (
            "https://user@example.com/file.jpg",
            "https://user:pass@example.com/file.jpg",
        ):
            with self.subTest(url=url):
                result = default_link_resolver_registry().resolve(
                    url,
                    request=ResolveRequest(host_resolver=lambda host: ["93.184.216.34"]),
                )

                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["skip_reason"], "unsafe_url")
                self.assertEqual(result["details"]["reason"], "userinfo_not_allowed")

    def test_malformed_url_is_structured_skip_not_crash(self) -> None:
        malformed = "https://example.com:bad/a.jpg"
        result = default_link_resolver_registry().resolve(
            malformed,
            request=ResolveRequest(host_resolver=lambda host: ["93.184.216.34"]),
        )

        self.assertEqual(normalize_url(malformed), malformed)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "unsafe_url")
        self.assertEqual(result["details"]["reason"], "malformed_url")

    def test_link_extraction_skips_userinfo_and_malformed_urls_only(self) -> None:
        from mediagent.core.links import extract_external_links_from_messages

        messages = [
            {
                "id": 1,
                "date": "2026-07-28T10:00:00+00:00",
                "chat": {"id": "inbox"},
                "text": (
                    "bad https://user:pass@example.com/a.jpg "
                    "also-bad https://example.com:bad/a.jpg "
                    f"good https://{PUBLIC_TEST_IP}/ok.jpg"
                ),
            }
        ]

        links = extract_external_links_from_messages(messages)

        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["normalized_url"], f"https://{PUBLIC_TEST_IP}/ok.jpg")

    def test_safety_gate_rejects_unresolved_host_with_injected_resolver(self) -> None:
        with self.assertRaises(ValueError):
            validate_url_safety("https://example.test/file.jpg", host_resolver=lambda host: [])

    def test_direct_media_resolver_accepts_video_quicktime_mov(self) -> None:
        url = f"https://{PUBLIC_TEST_IP}/clip.mov"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(
            200,
            {"Content-Type": "video/quicktime", "Content-Length": "4"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            url,
            request=ResolveRequest(http_client=fake),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["media_type"], "video")
        self.assertEqual(result["mime_type"], "video/quicktime")
        self.assertEqual(result["extension"], ".mov")

    def test_direct_media_resolver_accepts_mov_extension_fallback_when_size_is_bounded(self) -> None:
        url = f"https://{PUBLIC_TEST_IP}/clip.mov"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(
            200,
            {"Content-Type": "application/octet-stream", "Content-Length": "4"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            url,
            request=ResolveRequest(http_client=fake),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["mime_type"], "video/quicktime")
        self.assertEqual(result["details"]["validation"], "extension_fallback")

    def test_direct_media_resolver_rejects_unsupported_mime(self) -> None:
        url = f"https://{PUBLIC_TEST_IP}/note.txt"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(200, {"Content-Type": "text/plain", "Content-Length": "4"}, b"")

        result = default_link_resolver_registry().resolve(url, request=ResolveRequest(http_client=fake))

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "unsupported_media_type")

    def test_direct_media_resolver_rejects_excessive_redirects(self) -> None:
        fake = FakeLinkHttpClient()
        for index in range(5):
            url = f"https://{PUBLIC_TEST_IP}/r{index}.jpg"
            fake.heads[url] = HttpResponse(
                302,
                {"Location": f"https://{PUBLIC_TEST_IP}/r{index + 1}.jpg"},
                b"",
            )

        result = default_link_resolver_registry().resolve(
            f"https://{PUBLIC_TEST_IP}/r0.jpg",
            request=ResolveRequest(http_client=fake, policy=LinkSafetyPolicy(max_redirects=3)),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "unsafe_url")
        self.assertEqual(result["details"]["reason"], "too_many_redirects")

    def test_imgur_single_resolver_accepts_exactly_one_open_graph_media_url(self) -> None:
        page_url = "https://imgur.com/abc123"
        media_url = "https://i.imgur.com/abc123.jpg"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b'<html><meta property="og:image" content="https://i.imgur.com/abc123.jpg"></html>',
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "4"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.0.193"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "imgur_single")
        self.assertEqual(result["origin_source"], "imgur")
        self.assertEqual(result["remote_id"], "abc123")
        self.assertEqual(result["resolved_media_url"], media_url)

    def test_imgur_single_resolver_skips_multi_media_as_ambiguous(self) -> None:
        page_url = "https://imgur.com/abc123"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<meta property="og:image" content="https://i.imgur.com/a.jpg">'
                b'<meta property="og:video" content="https://i.imgur.com/a.mp4">'
            ),
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.0.193"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "ambiguous")

    def test_reserved_imgur_gallery_does_not_fall_through_to_generic_html(self) -> None:
        page_url = "https://imgur.com/gallery/abc123"
        fake = FakeLinkHttpClient()

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.0.193"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "reserved_platform_page")
        self.assertEqual(result["skip_reason"], "imgur_url_unsupported")
        self.assertEqual(result["origin_source"], "imgur")
        self.assertEqual(result["details"]["reason"], "unsupported_imgur_url")
        self.assertEqual(fake.calls, [])

    def test_reserved_instagram_story_does_not_fall_through_to_generic_html(self) -> None:
        page_url = "https://www.instagram.com/stories/alice/1234567890123456789?utm_source=ig_story_item_share"
        fake = FakeLinkHttpClient()

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["157.240.22.174"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "reserved_platform_page")
        self.assertEqual(result["skip_reason"], "instagram_url_unsupported")
        self.assertEqual(result["origin_source"], "instagram")
        self.assertEqual(result["details"]["reason"], "unsupported_instagram_url")
        self.assertEqual(fake.calls, [])

    def test_reddit_direct_image_resolver_preserves_reddit_platform(self) -> None:
        url = "https://i.redd.it/owv7awun2zfh1.jpeg"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "95778"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["origin_source"], "reddit")
        self.assertEqual(result["remote_id"], "i_owv7awun2zfh1")
        self.assertEqual(result["resolved_media_url"], url)

    def test_reddit_media_resolver_reads_modern_shreddit_post(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/title/"
        media_url = "https://i.redd.it/abc123.jpeg"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<shreddit-post id="t3_abc123" post-title="A title" author="alice" '
                b'subreddit-prefixed-name="r/example" created-timestamp="2026-07-28T10:00:00+00:00" '
                b'post-type="image" content-href="https://i.redd.it/abc123.jpeg"></shreddit-post>'
            ),
            page_url,
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )
        item = resolution_to_media_item(result, ingest_provenance={"platform": "telegram", "message_id": "7"})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["remote_id"], "t3_abc123")
        self.assertEqual(result["details"]["html_source"], "reddit")
        self.assertEqual(result["details"]["reddit"]["subreddit"], "example")
        self.assertEqual(item["author_name"], "alice")
        self.assertEqual(item["metadata"]["reddit"]["title"], "A title")
        self.assertEqual(item["metadata"]["source_timestamp"], "2026-07-28T10:00:00+00:00")

    def test_reddit_media_resolver_falls_back_to_old_reddit_for_verification_page(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/title/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/title/"
        media_url = "https://i.redd.it/abc123.jpeg"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        fake.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-subreddit="example" '
                b'data-author="alice" data-nsfw="true" data-timestamp="1785245238000" '
                b'data-url="https://i.redd.it/abc123.jpeg"></div>'
            ),
            old_url,
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["remote_id"], "t3_abc123")
        self.assertEqual(result["details"]["html_source"], "old_reddit")
        self.assertEqual(result["details"]["legacy_url"], old_url)
        self.assertEqual(result["details"]["reddit"]["over_18"], True)
        self.assertIn(("GET_LIMITED", old_url), fake.calls)
        self.assertIn(("GET_LIMITED", old_url, {"Cookie": "over18=1"}), fake.headers_by_call)

    def test_reddit_media_resolver_uses_over18_cookie_for_direct_old_reddit_page(self) -> None:
        page_url = "https://old.reddit.com/r/example/comments/abc123/title/"
        media_url = "https://i.redd.it/abc123.jpeg"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-subreddit="example" '
                b'data-author="alice" data-nsfw="true" data-url="https://i.redd.it/abc123.jpeg"></div>'
            ),
            page_url,
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["remote_id"], "t3_abc123")
        self.assertEqual(result["details"]["html_source"], "reddit")
        self.assertIn(("GET_LIMITED", page_url, {"Cookie": "over18=1"}), fake.headers_by_call)

    def test_reddit_media_resolver_skips_gallery_without_public_candidates(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/gallery/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        fake.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-is-gallery="true" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
            ),
            old_url,
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["skip_reason"], "unsupported_media_type")
        self.assertEqual(result["details"]["reason"], "gallery_unsupported")

    def test_reddit_media_resolver_accepts_static_gallery_images(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/gallery/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        media_a = "https://i.redd.it/gallery_a.jpg"
        media_b = "https://i.redd.it/gallery_b.png"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        fake.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-is-gallery="true" '
                b'data-author="alice" data-subreddit="example" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
                b'<script>{"media_metadata":{"a":{"s":{"u":"https://i.redd.it/gallery_a.jpg"}},'
                b'"b":{"s":{"u":"https://i.redd.it/gallery_b.png"}}}}</script>'
            ),
            old_url,
        )
        fake.heads[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"")
        fake.heads[media_b] = HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "20"}, b"")

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )
        item = resolution_to_media_item(result, ingest_provenance={"platform": "telegram", "message_id": "9"})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["remote_id"], "t3_abc123")
        self.assertEqual(result["media_count"], 2)
        self.assertEqual([candidate["url"] for candidate in result["media_candidates"]], [media_a, media_b])
        self.assertEqual([file_info["part"] for file_info in item["metadata"]["files"]], ["p0", "p1"])
        self.assertEqual(item["metadata"]["reddit"]["source_kind"], "post")

    def test_reddit_media_resolver_uses_gallery_preview_fallback(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/gallery/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        media_a = "https://preview.redd.it/gallery_a.jpg?width=1291&format=pjpg&auto=webp&s=a"
        media_b = "https://preview.redd.it/gallery_b.jpg?width=925&format=pjpg&auto=webp&s=b"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        fake.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-is-gallery="true" '
                b'data-author="alice" data-subreddit="example" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
                b'<img src="https://preview.redd.it/gallery_a.jpg?width=108&amp;crop=smart&amp;auto=webp&amp;s=thumb">'
                b'<img src="https://preview.redd.it/gallery_a.jpg?width=1291&amp;format=pjpg&amp;auto=webp&amp;s=a">'
                b'<img src="https://preview.redd.it/gallery_b.jpg?width=640&amp;blur=40&amp;format=pjpg&amp;auto=webp&amp;s=blur">'
                b'<img src="https://preview.redd.it/gallery_b.jpg?width=925&amp;format=pjpg&amp;auto=webp&amp;s=b">'
            ),
            old_url,
        )
        fake.heads[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"")
        fake.heads[media_b] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "20"}, b"")

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["remote_id"], "t3_abc123")
        self.assertEqual(result["media_count"], 2)
        self.assertEqual([candidate["url"] for candidate in result["media_candidates"]], [media_a, media_b])
        self.assertEqual(result["details"]["media_quality"], "preview_fallback")
        self.assertEqual(result["details"]["preview_fallback_count"], 2)

    def test_reddit_media_resolver_accepts_direct_v_redd_it_mp4_before_generic_direct_media(self) -> None:
        url = "https://v.redd.it/video/DASH_720.mp4"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(
            200,
            {"Content-Type": "video/mp4", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["origin_source"], "reddit")
        self.assertEqual(result["remote_id"], "v_video")
        self.assertEqual(result["media_type"], "video")
        self.assertEqual(result["resolved_media_url"], url)
        self.assertEqual(result["details"]["reddit"]["audio_status"], "not_merged")
        self.assertTrue(result["details"]["reddit"]["mux_required"])
        self.assertIn("audio muxing is not implemented yet", result["warnings"][0])

    def test_reddit_media_resolver_skips_direct_v_redd_it_manifest_without_mp4(self) -> None:
        url = "https://v.redd.it/abc123"

        result = default_link_resolver_registry().resolve(
            url,
            request=ResolveRequest(host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["origin_source"], "reddit")
        self.assertEqual(result["skip_reason"], "unsupported_media_type")
        self.assertEqual(result["details"]["reason"], "video_manifest_unsupported")

    def test_reddit_media_resolver_accepts_post_pointing_to_v_redd_it_mp4(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/video/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/video/"
        media_url = "https://v.redd.it/abc123/DASH_720.mp4"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        fake.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b'<div class="thing" data-fullname="t3_abc123" data-domain="v.redd.it" '
            b'data-url="https://v.redd.it/abc123/DASH_720.mp4"></div>',
            old_url,
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "video/mp4", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )
        item = resolution_to_media_item(result, ingest_provenance={"platform": "telegram", "message_id": "8"})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["media_type"], "video")
        self.assertEqual(result["resolved_media_url"], media_url)
        self.assertEqual(item["media_type"], "video")
        self.assertEqual(item["metadata"]["files"][0]["part"], "v0")
        self.assertEqual(item["metadata"]["reddit"]["audio_status"], "not_merged")

    def test_reddit_media_resolver_prefers_highest_dash_mp4_candidate(self) -> None:
        page_url = "https://www.reddit.com/r/example/comments/abc123/video/"
        media_url = "https://v.redd.it/abc123/DASH_1080.mp4?source=fallback"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<shreddit-post id="t3_abc123" post-title="Video" author="alice" '
                b'post-type="video" content-href="https://v.redd.it/abc123/DASH_720.mp4"></shreddit-post>'
                b'<script>{"fallback_url":"https:\\/\\/v.redd.it\\/abc123\\/DASH_1080.mp4?source=fallback"}</script>'
            ),
            page_url,
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "video/mp4", "Content-Length": "12"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "reddit_media_link")
        self.assertEqual(result["resolved_media_url"], media_url)

    def test_redgifs_resolver_extracts_static_mp4_from_watch_page(self) -> None:
        page_url = "https://www.redgifs.com/watch/ExampleClip"
        media_url = "https://media.redgifs.com/ExampleClip.mp4"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b'<script>{"hd":"https:\\/\\/media.redgifs.com\\/ExampleClip.mp4"}</script>',
            page_url,
        )
        fake.heads[media_url] = HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": "12"}, b"")

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )
        item = resolution_to_media_item(result, ingest_provenance={"platform": "telegram", "message_id": "10"})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "redgifs")
        self.assertEqual(result["origin_source"], "redgifs")
        self.assertEqual(result["remote_id"], "ExampleClip")
        self.assertEqual(result["resolved_media_url"], media_url)
        self.assertEqual(item["metadata"]["redgifs"]["audio_status"], "unknown")
        self.assertEqual(item["metadata"]["files"][0]["part"], "v0")

    def test_reddit_media_resolver_delegates_external_redgifs_link(self) -> None:
        page_url = "https://old.reddit.com/r/example/comments/abc123/redgifs/"
        redgifs_url = "https://www.redgifs.com/watch/ExampleClip"
        media_url = "https://media.redgifs.com/ExampleClip.mp4"
        fake = FakeLinkHttpClient()
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-domain="redgifs.com" '
                b'data-author="alice" data-subreddit="example" '
                b'data-timestamp="1780000000" '
                b'data-url="https://www.redgifs.com/watch/ExampleClip"></div>'
            ),
            page_url,
        )
        fake.gets[redgifs_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b'<script>{"hd":"https:\\/\\/media.redgifs.com\\/ExampleClip.mp4"}</script>',
            redgifs_url,
        )
        fake.heads[media_url] = HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": "12"}, b"")

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["151.101.1.140"]),
        )
        item = resolution_to_media_item(result, ingest_provenance={"platform": "telegram", "message_id": "11"})

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "redgifs")
        self.assertEqual(result["origin_source"], "redgifs")
        self.assertEqual(result["remote_id"], "ExampleClip")
        self.assertEqual(result["resolved_media_url"], media_url)
        self.assertEqual(result["details"]["delegated_from"]["resolver"], "reddit_media_link")
        self.assertEqual(result["details"]["reddit"]["id"], "t3_abc123")
        self.assertIn({"kind": "url", "url": page_url}, result["aliases"])
        self.assertEqual(item["platform"], "redgifs")
        self.assertEqual(item["metadata"]["reddit"]["id"], "t3_abc123")
        self.assertEqual(item["metadata"]["delegated_from"]["resolver"], "reddit_media_link")

    def test_generic_html_resolver_finds_single_media_without_domain_allowlist(self) -> None:
        page_url = "https://example.com/post/one"
        media_url = "https://cdn.example.com/media/photo.png?post=one"
        fake = FakeLinkHttpClient()
        fake.heads[page_url] = HttpResponse(200, {"Content-Type": "text/html"}, b"")
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html; charset=UTF-8"},
            b'<html><meta property="og:image" content="https://cdn.example.com/media/photo.png?post=one"></html>',
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/png", "Content-Length": "16"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "generic_html_media")
        self.assertEqual(result["origin_source"], "example_com")
        self.assertEqual(result["normalized_url"], page_url)
        self.assertEqual(result["resolved_media_url"], media_url)
        self.assertTrue(result["remote_id"].startswith("url_"))

    def test_generic_html_resolver_handles_head_forbidden_for_html_pages(self) -> None:
        page_url = "https://example.com/index.php?page=post&id=1"
        media_url = "https://cdn.example.com/images/full.jpg"
        fake = FakeLinkHttpClient()
        fake.heads[page_url] = HttpResponse(403, {"Content-Type": "text/html"}, b"")
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b'<a href="https://cdn.example.com/images/full.jpg">Original image</a>',
        )
        fake.heads[media_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "32"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "generic_html_media")
        self.assertEqual(result["resolved_media_url"], media_url)

    def test_generic_html_resolver_skips_multi_media_pages_as_ambiguous(self) -> None:
        page_url = "https://example.com/gallery"
        fake = FakeLinkHttpClient()
        fake.heads[page_url] = HttpResponse(200, {"Content-Type": "text/html"}, b"")
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<meta property="og:image" content="https://cdn.example.com/a.jpg">'
                b'<meta property="og:image" content="https://cdn.example.com/b.jpg">'
            ),
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "generic_html_media")
        self.assertEqual(result["skip_reason"], "ambiguous")

    def test_generic_html_resolver_prefers_original_over_preview_candidate(self) -> None:
        page_url = "https://example.com/post/9640226"
        original_url = "https://cdn.example.com/original/fd/0c/file.png"
        preview_url = "https://cdn.example.com/720x720/fd/0c/file.webp"
        fake = FakeLinkHttpClient()
        fake.heads[page_url] = HttpResponse(200, {"Content-Type": "text/html"}, b"")
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<meta property="og:image" content="https://cdn.example.com/original/fd/0c/file.png">'
                b'<meta name="twitter:image" content="https://cdn.example.com/720x720/fd/0c/file.webp">'
            ),
        )
        fake.heads[original_url] = HttpResponse(
            200,
            {"Content-Type": "image/png", "Content-Length": "64"},
            b"",
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]),
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver"], "generic_html_media")
        self.assertEqual(result["resolved_media_url"], original_url)

    def test_generic_html_resolver_skips_x_age_wall_without_downloading_preview_image(self) -> None:
        page_url = "https://x.com/user/status/123"
        fake = FakeLinkHttpClient()
        fake.heads[page_url] = HttpResponse(200, {"Content-Type": "text/html"}, b"")
        fake.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<meta property="og:image" content="https://abs.twimg.com/rweb/ssr/default/v2/og/image.png">'
                b'Age-restricted adult content. To view this media, you need to log in to X.'
                b'"blurred_image_url":"https://pbs.twimg.com/media/Gx.jpg?format=jpg&name=240x240"'
                b'"image_url":"https://pbs.twimg.com/profile_images/123/avatar_normal.jpg"'
            ),
        )

        result = default_link_resolver_registry().resolve(
            page_url,
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["104.244.42.1"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "generic_html_media")
        self.assertEqual(result["skip_reason"], "requires_auth")
        self.assertEqual(result["details"]["reason"], "login_or_age_gate")
        self.assertNotIn("pbs.twimg.com", [call[1] for call in fake.calls])

    def test_pixiv_artwork_link_is_identified_but_blocked_as_requires_auth(self) -> None:
        result = default_link_resolver_registry().resolve(
            "https://www.pixiv.net/artworks/143734851",
            request=ResolveRequest(host_resolver=lambda host: ["210.140.131.219"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "requires_auth")
        self.assertEqual(result["origin_source"], "pixiv")
        self.assertEqual(result["remote_id"], "143734851")

    def test_reserved_pixiv_non_artwork_url_does_not_fall_through_to_generic_html(self) -> None:
        fake = FakeLinkHttpClient()

        result = default_link_resolver_registry().resolve(
            "https://www.pixiv.net/users/12345",
            request=ResolveRequest(http_client=fake, host_resolver=lambda host: ["210.140.131.219"]),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["resolver"], "reserved_platform_page")
        self.assertEqual(result["skip_reason"], "pixiv_url_unsupported")
        self.assertEqual(result["origin_source"], "pixiv")
        self.assertEqual(result["details"]["reason"], "unsupported_pixiv_url")
        self.assertEqual(fake.calls, [])

    def test_link_resolve_preview_tool_returns_structured_resolution(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg"
        fake = FakeLinkHttpClient()
        fake.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(Path(temp_dir) / "data")},
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run("link.resolve.preview", {"url": url}, context, allow_experimental=True)
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["resolution"]["status"], "resolved")
        self.assertEqual(result.data["resolution"]["origin_source"], "1_1_1_1")


class LinkQueueAndSyncTests(unittest.TestCase):
    def test_link_queue_upsert_tool_queues_urls_without_experimental_flag(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg#one"
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(Path(temp_dir) / "data")},
                cwd=Path(temp_dir),
            )
            db_path = Path(temp_dir) / "mediagent.sqlite3"

            result = asyncio.run(
                registry.run(
                    "link.queue.upsert",
                    {"db_path": str(db_path), "url": url, "ingest_platform": "cli"},
                    context,
                )
            )
            rows = db.list_links(db_path)

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["queued"], 1)
        self.assertEqual(rows[0]["normalized_url"], f"https://{PUBLIC_TEST_IP}/photo.jpg")
        self.assertEqual(rows[0]["source_provenance"][0]["ingest_platform"], "cli")

    def test_telegram_inbox_collect_links_queues_unique_normalized_urls(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={
                "curated": [
                    {
                        "id": 1,
                        "date": "2026-07-28T10:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": (
                            f"one https://{PUBLIC_TEST_IP}/photo.jpg#frag "
                            f"two https://{PUBLIC_TEST_IP}/photo.jpg"
                        ),
                        "media": [],
                    }
                ]
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                    allow_experimental=True,
                )
            )
            rows = db.list_links(db_path)

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["links_found"], 1)
        self.assertEqual(result.data["summary"]["links_queued"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["normalized_url"], f"https://{PUBLIC_TEST_IP}/photo.jpg")

    def test_telegram_inbox_collect_links_uses_env_default_chat_and_cursor_key(self) -> None:
        registry = create_default_registry()
        old_url = f"https://{PUBLIC_TEST_IP}/old.jpg"
        new_url = f"https://{PUBLIC_TEST_IP}/new.jpg"
        fake = FakeTelegramClient(
            messages={
                "mediagent_inbox": [
                    {
                        "id": 34,
                        "date": "2026-08-04T01:00:00+00:00",
                        "chat": {"id": "3779502941", "title": "Inbox", "type": "channel"},
                        "text": old_url,
                        "media": [],
                    },
                    {
                        "id": 35,
                        "date": "2026-08-04T01:05:00+00:00",
                        "chat": {"id": "3779502941", "title": "Inbox", "type": "channel"},
                        "text": new_url,
                        "media": [],
                    },
                ]
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(
                temp_dir,
                fake,
                env_overrides={
                    "MEDIAGENT_TELEGRAM_INBOX_KEY": "mediagent_inbox",
                    "MEDIAGENT_TELEGRAM_INBOX_CHAT_ID": "3779502941",
                },
            )
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="links:mediagent_inbox",
                cursor_value="34",
            )

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {"db_path": str(db_path)},
                    context,
                    allow_experimental=True,
                )
            )
            rows = db.list_links(db_path)
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="links:mediagent_inbox")

        self.assertTrue(result.is_success)
        self.assertEqual(fake.calls[0][1]["chats"], [{"key": "mediagent_inbox", "id": "3779502941"}])
        self.assertEqual(fake.calls[0][1]["after_by_source"], {"mediagent_inbox": 34})
        self.assertEqual(result.data["summary"]["links_found"], 1)
        self.assertEqual(rows[0]["normalized_url"], new_url)
        self.assertEqual(cursor["cursor_value"], "35")

    def test_telegram_inbox_collect_links_full_sync_ignores_existing_cursor(self) -> None:
        registry = create_default_registry()
        old_url = f"https://{PUBLIC_TEST_IP}/old.jpg"
        fake = FakeTelegramClient(
            messages={
                "curated": [
                    {
                        "id": 10,
                        "date": "2026-08-04T01:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": old_url,
                        "media": [],
                    }
                ]
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="links:curated",
                cursor_value="10",
            )

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {
                        "db_path": str(db_path),
                        "chat": "curated",
                        "full_sync": True,
                        "store_cursor": False,
                    },
                    context,
                    allow_experimental=True,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.calls[0][1]["after_by_source"], {"curated": None})
        self.assertEqual(result.data["summary"]["links_found"], 1)
        self.assertEqual(result.data["links"][0]["normalized_url"], old_url)

    def test_telegram_inbox_collect_links_explicit_after_message_id_wins_over_full_sync(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"curated": []})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="links:curated",
                cursor_value="99",
            )

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {
                        "db_path": str(db_path),
                        "chat": "curated",
                        "after_message_id": 7,
                        "full_sync": True,
                    },
                    context,
                    allow_experimental=True,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.calls[0][1]["after_by_source"], {"curated": 7})

    def test_telegram_inbox_collect_links_requires_chat_or_env_default(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {"db_path": str(db_path)},
                    context,
                    allow_experimental=True,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "missing_telegram_inbox_chat")

    def test_telegram_inbox_sync_links_downloads_with_origin_source_layout_and_metadata(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"image-data")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 7,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url} SECRET_INLINE_MESSAGE",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {
                        "db_path": str(db_path),
                        "chat": "curated",
                        "write_sidecar_metadata": True,
                    },
                    context,
                    allow_experimental=True,
                )
            )
            files = db.list_media_files(db_path, platform="1_1_1_1")
            with db.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT metadata_json FROM media_items WHERE platform = ?",
                    ("1_1_1_1",),
                ).fetchone()
            metadata = json.loads(item_row["metadata_json"])
            written_files = list((data_dir / "library").rglob("*.jpg"))
            sidecars = list((data_dir / "library").rglob("*.json"))
            sidecar_text = sidecars[0].read_text(encoding="utf-8")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(len(files), 1)
        self.assertIn("/library/1_1_1_1/photo/2026/07/", str(written_files[0]))
        self.assertNotIn("/library/telegram/", str(written_files[0]))
        self.assertEqual(metadata["origin_source"], "1_1_1_1")
        self.assertEqual(metadata["ingested_from"]["platform"], "telegram")
        self.assertEqual(metadata["ingested_from"]["message_id"], "7")
        self.assertNotIn("SECRET_INLINE_MESSAGE", json.dumps(metadata))
        self.assertEqual(len(sidecars), 1)
        self.assertNotIn("SECRET_INLINE_MESSAGE", sidecar_text)

    def test_telegram_inbox_sync_bridges_public_message_link_alongside_external_url(self) -> None:
        registry = create_default_registry()
        external_url = f"https://{PUBLIC_TEST_IP}/mixed.jpg"
        telegram_url = "https://t.me/source_channel/100"
        http = FakeLinkHttpClient()
        http.heads[external_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "8"},
            b"",
        )
        http.gets[external_url] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "8"},
            b"external",
        )
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 20,
                        "date": "2026-08-11T10:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save {external_url} and {telegram_url}",
                        "media": [],
                    }
                ],
                "link:source_channel": [
                    {
                        "id": 100,
                        "date": "2026-08-10T09:00:00+00:00",
                        "chat": {
                            "id": "source-channel-id",
                            "title": "Source",
                            "type": "channel",
                            "username": "source_channel",
                        },
                        "media": [
                            {
                                "id": "photo-100",
                                "kind": "photo",
                                "mime_type": "image/jpeg",
                                "download_ref": {
                                    "chat_id": "source-channel-id",
                                    "chat_username": "source_channel",
                                    "message_id": "100",
                                    "media_id": "photo-100",
                                },
                            }
                        ],
                    }
                ],
            },
            http=http,
        )
        fake.downloads["source-channel-id:100:photo-100"] = b"telegram"
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                )
            )
            with db.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT metadata_json FROM media_items WHERE platform = 'telegram'"
                ).fetchone()
            metadata = json.loads(row["metadata_json"])

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["links_considered"], 2)
        self.assertEqual(result.data["summary"]["resolved"], 2)
        self.assertEqual(result.data["summary"]["downloaded"], 2)
        self.assertEqual(result.data["summary"]["files_downloaded"], 2)
        self.assertEqual(metadata["ingested_from"]["chat_id"], "curated")
        self.assertEqual(metadata["ingested_from"]["message_id"], "20")
        self.assertEqual(metadata["source_provenance"][0]["message_date"], "2026-08-11T10:00:00+00:00")

    def test_telegram_inbox_sync_bridges_private_message_link(self) -> None:
        registry = create_default_registry()
        telegram_url = "https://t.me/c/123456789/55?single"
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 21,
                        "chat": {"id": "curated", "type": "channel"},
                        "text": telegram_url,
                        "media": [],
                    }
                ],
                "link:-100123456789": [
                    {
                        "id": 55,
                        "chat": {"id": "-100123456789", "type": "channel"},
                        "media": [
                            {
                                "id": "video-55",
                                "kind": "video",
                                "mime_type": "video/mp4",
                                "download_ref": {
                                    "chat_id": "-100123456789",
                                    "message_id": "55",
                                    "media_id": "video-55",
                                },
                            }
                        ],
                    }
                ],
            },
            http=FakeLinkHttpClient(),
        )
        fake.downloads["-100123456789:55:video-55"] = b"private-video"
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["telegram_links_considered"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(result.data["telegram_message_links"][0]["status"], "resolved")

    def test_telegram_inbox_sync_structures_inaccessible_and_protected_message_link_skips(self) -> None:
        registry = create_default_registry()
        inaccessible_url = "https://telegram.me/c/123456789/55"
        protected_url = "https://t.me/c/123456789/56"
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 22,
                        "chat": {"id": "curated", "type": "channel"},
                        "text": f"{inaccessible_url} {protected_url}",
                        "media": [],
                    }
                ],
                "link:-100123456789": [
                    {
                        "id": 56,
                        "protected_content": True,
                        "chat": {"id": "-100123456789", "type": "channel"},
                        "media": [{"id": "photo-56", "kind": "photo", "mime_type": "image/jpeg"}],
                    }
                ],
            },
            http=FakeLinkHttpClient(),
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["skipped_links"], 2)
        self.assertEqual(
            [entry["skip_reason"] for entry in result.data["telegram_message_links"]],
            ["inaccessible", "protected_content"],
        )
        self.assertEqual(result.data["summary"]["downloaded"], 0)

    def test_telegram_inbox_sync_links_downloads_reddit_video_mp4_to_reddit_layout(self) -> None:
        registry = create_default_registry()
        url = "https://v.redd.it/abc123/DASH_720.mp4"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": "10"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "video/mp4", "Content-Length": "10"}, b"video-data")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 8,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            with patch("mediagent.core.links.resolve_host_ips", return_value=["151.101.1.140"]):
                result = asyncio.run(
                    registry.run(
                        "telegram.inbox.sync_links",
                        {
                            "db_path": str(db_path),
                            "chat": "curated",
                            "write_sidecar_metadata": True,
                        },
                        context,
                        allow_experimental=True,
                    )
                )
            files = db.list_media_files(db_path, platform="reddit")
            with db.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT metadata_json FROM media_items WHERE platform = ?",
                    ("reddit",),
                ).fetchone()
            metadata = json.loads(item_row["metadata_json"])
            written_files = list((data_dir / "library").rglob("*.mp4"))
            sidecars = list((data_dir / "library").rglob("*.json"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(written_files), 1)
        self.assertIn("/library/reddit/video/2026/07/", str(written_files[0]))
        self.assertEqual(metadata["origin_source"], "reddit")
        self.assertEqual(metadata["ingested_from"]["platform"], "telegram")
        self.assertEqual(metadata["reddit"]["audio_status"], "not_merged")
        self.assertTrue(metadata["reddit"]["mux_required"])
        self.assertEqual(metadata["files"][0]["part"], "v0")
        self.assertEqual(len(sidecars), 1)

    def test_telegram_inbox_sync_links_downloads_reddit_gallery_files(self) -> None:
        registry = create_default_registry()
        page_url = "https://www.reddit.com/r/example/comments/abc123/gallery/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        media_a = "https://i.redd.it/gallery_a.jpg"
        media_b = "https://i.redd.it/gallery_b.png"
        http = FakeLinkHttpClient()
        http.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        http.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-is-gallery="true" '
                b'data-author="alice" data-subreddit="example" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
                b'<script>{"media_metadata":{"a":{"s":{"u":"https://i.redd.it/gallery_a.jpg"}},'
                b'"b":{"s":{"u":"https://i.redd.it/gallery_b.png"}}}}</script>'
            ),
            old_url,
        )
        http.heads[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "7"}, b"")
        http.heads[media_b] = HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "8"}, b"")
        http.gets[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "7"}, b"jpgdata")
        http.gets[media_b] = HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "8"}, b"png-data")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 11,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {page_url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            with patch("mediagent.core.links.resolve_host_ips", return_value=["151.101.1.140"]):
                result = asyncio.run(
                    registry.run(
                        "telegram.inbox.sync_links",
                        {
                            "db_path": str(db_path),
                            "chat": "curated",
                            "write_sidecar_metadata": True,
                        },
                        context,
                        allow_experimental=True,
                    )
                )
            files = db.list_media_files(db_path, platform="reddit")
            with db.connect(db_path) as connection:
                item_row = connection.execute(
                    "SELECT metadata_json FROM media_items WHERE platform = ?",
                    ("reddit",),
                ).fetchone()
            metadata = json.loads(item_row["metadata_json"])
            written_files = list((data_dir / "library" / "reddit" / "photo").rglob("*.*"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(result.data["summary"]["files_downloaded"], 2)
        self.assertEqual(len(files), 2)
        self.assertEqual({file["status"] for file in files}, {"downloaded"})
        self.assertEqual([file_info["part"] for file_info in metadata["files"]], ["p0", "p1"])
        self.assertEqual(len([path for path in written_files if path.suffix in {".jpg", ".png"}]), 2)

    def test_link_media_sync_downloads_reddit_gallery_without_telegram(self) -> None:
        registry = create_default_registry()
        page_url = "https://www.reddit.com/r/example/comments/abc123/gallery/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        media_a = "https://i.redd.it/gallery_a.jpg"
        media_b = "https://i.redd.it/gallery_b.png"
        http = FakeLinkHttpClient()
        http.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            b"<title>Reddit - Please wait for verification</title><form id='js_challenge'></form>",
            page_url,
        )
        http.gets[old_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-is-gallery="true" '
                b'data-author="alice" data-subreddit="example" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
                b'<script>{"media_metadata":{"a":{"s":{"u":"https://i.redd.it/gallery_a.jpg"}},'
                b'"b":{"s":{"u":"https://i.redd.it/gallery_b.png"}}}}</script>'
            ),
            old_url,
        )
        http.heads[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "7"}, b"")
        http.heads[media_b] = HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "8"}, b"")
        http.gets[media_a] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "7"}, b"jpgdata")
        http.gets[media_b] = HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "8"}, b"png-data")
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(Path(temp_dir) / "data")},
                cwd=Path(temp_dir),
                http_client=http,
            )
            db_path = Path(temp_dir) / "mediagent.sqlite3"

            with patch("mediagent.core.links.resolve_host_ips", return_value=["151.101.1.140"]):
                result = asyncio.run(
                    registry.run(
                        "link.media.sync",
                        {
                            "db_path": str(db_path),
                            "url": page_url,
                            "write_sidecar_metadata": True,
                        },
                        context,
                    )
                )
            files = db.list_media_files(db_path, platform="reddit")
            written_files = list((Path(temp_dir) / "data" / "library" / "reddit" / "photo").rglob("*.*"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(result.data["summary"]["files_downloaded"], 2)
        self.assertEqual(len(files), 2)
        self.assertEqual(len([path for path in written_files if path.suffix in {".jpg", ".png"}]), 2)

    def test_telegram_inbox_sync_links_dry_run_does_not_write(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 7,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake, dry_run=True)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                    allow_experimental=True,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["queued"], 1)
        self.assertFalse(db_path.exists())
        self.assertFalse((data_dir / "library").exists())

    def test_telegram_inbox_sync_links_revalidates_get_redirect_safety(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "10"}, b"")
        http.gets[url] = HttpResponse(302, {"Location": "https://127.0.0.1/private.jpg"}, b"")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 8,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                    allow_experimental=True,
                )
            )
            files = db.list_media_files(db_path, platform="1_1_1_1")
            written_files = list((data_dir / "library").rglob("*.jpg"))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_inbox_sync_links_failed")
        self.assertEqual(files[0]["status"], "failed")
        self.assertEqual(written_files, [])

    def test_telegram_inbox_sync_links_rejects_oversized_get_body(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/photo.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg"}, b"12345")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 9,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated", "max_media_bytes": 4},
                    context,
                    allow_experimental=True,
                )
            )
            files = db.list_media_files(db_path, platform="1_1_1_1")
            written_files = list((data_dir / "library").rglob("*.jpg"))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_inbox_sync_links_failed")
        self.assertEqual(files[0]["status"], "failed")
        self.assertEqual(written_files, [])

    def test_telegram_inbox_sync_links_rejects_mov_get_redirect_to_non_media(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/clip.mov"
        redirected = f"https://{PUBLIC_TEST_IP}/not-media.txt"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(
            200,
            {"Content-Type": "application/octet-stream", "Content-Length": "4"},
            b"",
        )
        http.gets[url] = HttpResponse(302, {"Location": redirected}, b"")
        http.gets[redirected] = HttpResponse(200, {"Content-Type": "text/plain", "Content-Length": "4"}, b"text")
        fake = CombinedTelegramLinkClient(
            messages={
                "curated": [
                    {
                        "id": 10,
                        "date": "2026-07-28T11:00:00+00:00",
                        "chat": {"id": "curated", "title": "Inbox", "type": "channel"},
                        "text": f"save this {url}",
                        "media": [],
                    }
                ]
            },
            http=http,
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {"db_path": str(db_path), "chat": "curated"},
                    context,
                    allow_experimental=True,
                )
            )
            files = db.list_media_files(db_path, platform="1_1_1_1")
            written_files = list((data_dir / "library").rglob("*.mov"))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_inbox_sync_links_failed")
        self.assertEqual(files[0]["status"], "failed")
        self.assertEqual(written_files, [])

    def test_link_queue_database_enforces_normalized_url_uniqueness(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            first = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "telegram",
                    "original_url": "HTTPS://1.1.1.1/photo.jpg#one",
                    "normalized_url": normalize_url("HTTPS://1.1.1.1/photo.jpg#one"),
                    "source_chat_id": "inbox",
                    "source_message_id": "1",
                },
            )
            second = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "telegram",
                    "original_url": "https://1.1.1.1/photo.jpg#two",
                    "normalized_url": normalize_url("https://1.1.1.1/photo.jpg#two"),
                    "source_chat_id": "inbox",
                    "source_message_id": "2",
                },
            )
            updated = db.update_link_resolution(
                db_path,
                link_id=first["id"],
                status="skipped",
                resolution={
                    "status": "skipped",
                    "original_url": "https://1.1.1.1/photo.jpg#one",
                    "normalized_url": "https://1.1.1.1/photo.jpg",
                    "skip_reason": "requires_auth",
                    "details": {"reason": "login_wall"},
                },
                skip_reason="requires_auth",
            )
            with sqlite3.connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM link_queue").fetchone()[0]

        self.assertTrue(first["is_new"])
        self.assertFalse(second["is_new"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(count, 1)
        self.assertEqual(len(second["source_provenance"]), 2)
        self.assertEqual(updated["attempt_count"], 1)
        self.assertFalse(updated["retryable"])
        self.assertEqual(updated["last_error_code"], "requires_auth")

    def test_link_queue_claims_ready_links_and_respects_lease(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "cli",
                    "original_url": f"https://{PUBLIC_TEST_IP}/photo.jpg",
                    "normalized_url": f"https://{PUBLIC_TEST_IP}/photo.jpg",
                },
            )

            claimed = db.claim_links(
                db_path,
                lease_owner="worker-a",
                lease_seconds=60,
                now="2026-07-29T00:00:00+00:00",
            )
            blocked = db.claim_links(
                db_path,
                lease_owner="worker-b",
                lease_seconds=60,
                now="2026-07-29T00:00:30+00:00",
            )
            reclaimed = db.claim_links(
                db_path,
                lease_owner="worker-b",
                lease_seconds=60,
                now="2026-07-29T00:01:01+00:00",
            )

        self.assertEqual([row["id"] for row in claimed], [link["id"]])
        self.assertEqual(claimed[0]["lease_owner"], "worker-a")
        self.assertEqual(blocked, [])
        self.assertEqual([row["id"] for row in reclaimed], [link["id"]])
        self.assertEqual(reclaimed[0]["lease_owner"], "worker-b")

    def test_link_retryable_resolution_is_deferred_until_next_attempt(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "cli",
                    "original_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                    "normalized_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                },
            )
            claimed = db.claim_links(
                db_path,
                lease_owner="worker-a",
                lease_seconds=60,
                now="2026-07-29T00:00:00+00:00",
            )
            updated = db.update_link_resolution(
                db_path,
                link_id=link["id"],
                status="skipped",
                skip_reason="resolver_error",
                resolution={
                    "status": "skipped",
                    "skip_reason": "resolver_error",
                    "normalized_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                    "details": {"reason": "temporary_network_error"},
                },
            )
            not_ready = db.list_ready_links(db_path, now=updated["last_attempt_at"])
            ready_later = db.list_ready_links(db_path, now="2999-01-01T00:00:00+00:00")

        self.assertEqual([row["id"] for row in claimed], [link["id"]])
        self.assertEqual(updated["status"], "deferred")
        self.assertEqual(updated["attempt_count"], 1)
        self.assertTrue(updated["retryable"])
        self.assertIsNotNone(updated["next_attempt_at"])
        self.assertIsNone(updated["lease_owner"])
        self.assertEqual(not_ready, [])
        self.assertEqual([row["id"] for row in ready_later], [link["id"]])

    def test_link_retryable_resolution_fails_after_max_attempts(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "cli",
                    "original_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                    "normalized_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                    "max_attempts": 1,
                },
            )
            updated = db.update_link_resolution(
                db_path,
                link_id=link["id"],
                status="skipped",
                skip_reason="resolver_error",
                resolution={
                    "status": "skipped",
                    "skip_reason": "resolver_error",
                    "normalized_url": f"https://{PUBLIC_TEST_IP}/temporary.jpg",
                },
            )

        self.assertEqual(updated["status"], "failed")
        self.assertFalse(updated["retryable"])
        self.assertIsNone(updated["next_attempt_at"])

    def test_link_media_sync_retries_previously_auth_skipped_link(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/auth-now-usable.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"data")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "cli",
                    "original_url": url,
                    "normalized_url": url,
                },
            )
            db.update_link_resolution(
                db_path,
                link_id=link["id"],
                status="skipped",
                skip_reason="requires_auth",
                resolution={"status": "skipped", "skip_reason": "requires_auth", "normalized_url": url},
            )

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {"db_path": str(db_path), "retry_auth_skipped": True},
                    context,
                )
            )
            retried_link = db.get_link(db_path, link_id=link["id"])

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["auth_links_retried"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(retried_link["status"], "resolved")

    def test_telegram_inbox_sync_retries_old_auth_skip_after_session_becomes_usable(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/old-auth-skip.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"data")
        fake = CombinedTelegramLinkClient(messages={"curated": []}, http=http)
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "telegram",
                    "original_url": url,
                    "normalized_url": url,
                    "source_chat_id": "curated",
                    "source_message_id": "5",
                },
            )
            db.update_link_resolution(
                db_path,
                link_id=link["id"],
                status="skipped",
                skip_reason="requires_auth",
                resolution={"status": "skipped", "skip_reason": "requires_auth", "normalized_url": url},
            )

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync_links",
                    {
                        "db_path": str(db_path),
                        "chat": "curated",
                        "retry_auth_skipped": True,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["auth_links_retried"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 1)

    def test_link_media_sync_claims_queued_links_and_clears_lease(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/queued.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {"ingest_platform": "cli", "original_url": url, "normalized_url": normalize_url(url)},
            )

            result = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path)}, context))
            stored_link = db.get_link(db_path, link_id=link["id"])
            files = db.list_media_files(db_path, platform=PUBLIC_TEST_IP.replace(".", "_"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["links_considered"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(stored_link["status"], "resolved")
        self.assertEqual(stored_link["attempt_count"], 1)
        self.assertIsNone(stored_link["lease_owner"])
        self.assertIsNone(stored_link["lease_expires_at"])
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["status"], "downloaded")

    def test_link_media_sync_default_rerun_keeps_missing_downloaded_item_skipped(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/missing-default.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            first = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": url}, context))
            stored_file = db.list_media_files(db_path)[0]
            Path(stored_file["local_path"]).unlink()
            http.calls.clear()
            rerun = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": url}, context))

        self.assertTrue(first.is_success)
        self.assertTrue(rerun.is_success)
        self.assertEqual(rerun.data["summary"]["queued"], 0)
        self.assertEqual(rerun.data["summary"]["skipped_items"], 1)
        self.assertEqual(rerun.data["summary"]["files_downloaded"], 0)
        self.assertNotIn(("GET_LIMITED", url), http.calls)

    def test_link_media_sync_repair_missing_file_queues_and_redownloads(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/missing-repair.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            first = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": url}, context))
            stored_file = db.list_media_files(db_path)[0]
            target_path = Path(stored_file["local_path"])
            target_path.unlink()
            repair = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {"db_path": str(db_path), "url": url, "repair_missing_files": True},
                    context,
                )
            )
            repaired_file = db.list_media_files(db_path)[0]
            repaired_path_exists = Path(repaired_file["local_path"]).exists()

        self.assertTrue(first.is_success)
        self.assertTrue(repair.is_success)
        self.assertEqual(repair.data["summary"]["queued"], 1)
        self.assertEqual(repair.data["summary"]["repair_items"], 1)
        self.assertEqual(repair.data["summary"]["repair_files_missing"], 1)
        self.assertEqual(repair.data["summary"]["repaired"], 1)
        self.assertEqual(repair.data["summary"]["files_downloaded"], 1)
        self.assertTrue(repaired_path_exists)
        self.assertEqual(repaired_file["file_health"], "valid")

    def test_link_media_sync_repair_keeps_healthy_downloaded_item_skipped(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/healthy-repair.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            first = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": url}, context))
            http.calls.clear()
            repair = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {"db_path": str(db_path), "url": url, "repair_missing_files": True},
                    context,
                )
            )

        self.assertTrue(first.is_success)
        self.assertTrue(repair.is_success)
        self.assertEqual(repair.data["summary"]["queued"], 0)
        self.assertEqual(repair.data["summary"]["skipped_items"], 1)
        self.assertEqual(repair.data["summary"]["skipped_healthy"], 1)
        self.assertEqual(repair.data["summary"]["repair_items"], 0)
        self.assertEqual(repair.data["summary"]["files_downloaded"], 0)
        self.assertNotIn(("GET_LIMITED", url), http.calls)

    def test_link_media_sync_dry_run_repair_plans_missing_file_without_writes(self) -> None:
        registry = create_default_registry()
        url = f"https://{PUBLIC_TEST_IP}/missing-dry-run.jpg"
        http = FakeLinkHttpClient()
        http.heads[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            first = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": url}, context))
            stored_file = db.list_media_files(db_path)[0]
            target_path = Path(stored_file["local_path"])
            target_path.unlink()
            dry_context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
                dry_run=True,
            )
            http.calls.clear()
            dry_run = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {"db_path": str(db_path), "url": url, "repair_missing_files": True},
                    dry_context,
                )
            )
            unchanged_file = db.list_media_files(db_path)[0]

        self.assertTrue(first.is_success)
        self.assertTrue(dry_run.is_success)
        self.assertEqual(dry_run.data["summary"]["queued"], 1)
        self.assertEqual(dry_run.data["summary"]["repair_items"], 1)
        self.assertEqual(dry_run.data["summary"]["repair_files_missing"], 1)
        self.assertEqual(len(dry_run.data["planned_downloads"]), 1)
        self.assertFalse(target_path.exists())
        self.assertEqual(unchanged_file["file_health"], "valid")
        self.assertNotIn(("GET_LIMITED", url), http.calls)

    def test_link_media_sync_dedupes_resolved_media_items_from_distinct_links(self) -> None:
        registry = create_default_registry()
        page_url = "https://www.reddit.com/r/example/comments/abc123/photo/"
        old_url = "https://old.reddit.com/r/example/comments/abc123/photo/"
        media_url = "https://i.redd.it/abc123.jpeg"
        http = FakeLinkHttpClient()
        page_html = (
            b'<shreddit-post id="t3_abc123" post-title="Photo" author="alice" '
            b'content-href="https://i.redd.it/abc123.jpeg"></shreddit-post>'
        )
        old_html = (
            b'<div class="thing" data-fullname="t3_abc123" data-domain="i.redd.it" '
            b'data-url="https://i.redd.it/abc123.jpeg"></div>'
        )
        http.gets[page_url] = HttpResponse(200, {"Content-Type": "text/html"}, page_html, page_url)
        http.gets[old_url] = HttpResponse(200, {"Content-Type": "text/html"}, old_html, old_url)
        http.heads[media_url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[media_url] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {"db_path": str(db_path), "urls": [page_url, old_url]},
                    context,
                )
            )
            files = db.list_media_files(db_path, platform="reddit")
            media_get_calls = [call for call in http.calls if call == ("GET_LIMITED", media_url)]
            with sqlite3.connect(db_path) as connection:
                item_count = connection.execute(
                    "SELECT COUNT(*) FROM media_items WHERE platform = 'reddit'"
                ).fetchone()[0]

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["links_considered"], 2)
        self.assertEqual(result.data["summary"]["resolved"], 2)
        self.assertEqual(result.data["summary"]["queued"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(item_count, 1)
        self.assertEqual(len(files), 1)
        self.assertEqual(len(media_get_calls), 1)

    def test_link_media_sync_marks_multi_file_item_partial_when_required_file_fails(self) -> None:
        registry = create_default_registry()
        page_url = "https://old.reddit.com/r/example/comments/abc123/gallery/"
        first_media = "https://i.redd.it/abc123a.jpeg"
        second_media = "https://i.redd.it/abc123b.jpeg"
        http = FakeLinkHttpClient()
        http.gets[page_url] = HttpResponse(
            200,
            {"Content-Type": "text/html"},
            (
                b'<div class="thing" data-fullname="t3_abc123" data-domain="reddit.com" '
                b'data-url="https://www.reddit.com/gallery/abc123"></div>'
                b'<a href="https://i.redd.it/abc123a.jpeg"></a>'
                b'<a href="https://i.redd.it/abc123b.jpeg"></a>'
            ),
            page_url,
        )
        http.heads[first_media] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.heads[second_media] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"")
        http.gets[first_media] = HttpResponse(200, {"Content-Type": "image/jpeg", "Content-Length": "4"}, b"test")
        http.gets[second_media] = HttpResponse(500, {"Content-Type": "text/plain"}, b"fail")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir), "MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                http_client=http,
            )

            result = asyncio.run(registry.run("link.media.sync", {"db_path": str(db_path), "url": page_url}, context))
            files = db.list_media_files(db_path, platform="reddit")
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT status, metadata_json FROM media_items WHERE platform = 'reddit' AND remote_id = 't3_abc123'"
                ).fetchone()

        metadata = json.loads(row[1])
        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "link_media_sync_partial")
        self.assertEqual(result.data["summary"]["partial"], 1)
        self.assertEqual(row[0], "partial")
        self.assertEqual(metadata["candidate_group"]["required_files"], 2)
        self.assertEqual(metadata["candidate_group"]["optional_files"], 0)
        self.assertTrue(all(file_info["required"] for file_info in metadata["files"]))
        self.assertEqual({file["status"] for file in files}, {"downloaded", "failed"})

    def test_link_resolution_storage_removes_credential_bearing_headers(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            db.initialize_database(db_path)
            link = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "cli",
                    "original_url": "https://1.1.1.1/photo.jpg",
                    "normalized_url": "https://1.1.1.1/photo.jpg",
                },
            )
            updated = db.update_link_resolution(
                db_path,
                link_id=link["id"],
                status="resolved",
                resolution={
                    "status": "resolved",
                    "normalized_url": "https://1.1.1.1/photo.jpg",
                    "canonical_url": "https://1.1.1.1/photo.jpg",
                    "media_candidates": [
                        {
                            "url": "https://1.1.1.1/photo.jpg",
                            "persistable_headers": {
                                "Referer": "https://example.com/",
                                "Authorization": "Bearer secret",
                                "Cookie": "session=secret",
                            },
                            "details": {
                                "headers": {
                                    "X-CSRF-Token": "secret",
                                    "Accept": "image/*",
                                },
                                "download_context": {"headers": {"Cookie": "secret"}},
                            },
                        }
                    ],
                },
            )

        candidate = updated["resolution"]["media_candidates"][0]
        self.assertEqual(candidate["persistable_headers"], {"Referer": "https://example.com/"})
        self.assertEqual(candidate["details"]["headers"], {"Accept": "image/*"})
        self.assertNotIn("download_context", candidate["details"])
