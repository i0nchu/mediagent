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

    def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.calls.append(("HEAD", url))
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
        response = self.gets[url]
        return HttpResponse(
            response.status_code,
            response.headers,
            response.content[:max_bytes],
            response.url,
        )

    def get(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.calls.append(("GET", url))
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

    def test_reddit_media_resolver_skips_gallery_for_phase_17(self) -> None:
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
                },
            )
            second = db.upsert_link(
                db_path,
                {
                    "ingest_platform": "telegram",
                    "original_url": "https://1.1.1.1/photo.jpg#two",
                    "normalized_url": normalize_url("https://1.1.1.1/photo.jpg#two"),
                },
            )
            with sqlite3.connect(db_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM link_queue").fetchone()[0]

        self.assertTrue(first["is_new"])
        self.assertFalse(second["is_new"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(count, 1)
