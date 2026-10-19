"""Offline regression coverage for resolver HTTP policy and regional redirects."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import patch

from mediagent.core.http import HttpResponse
from mediagent.core.links import (
    GenericHTMLMediaResolver,
    ResolveRequest,
    default_link_resolver_registry,
    validate_url_safety,
)


class FakeHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def head(self, url, **kwargs):
        self.calls.append(("HEAD", url))
        return self.responses[url]

    def get_limited(self, url, **kwargs):
        self.calls.append(("GET", url))
        return self.responses[url]


class LinkHTTPPolicyTests(unittest.TestCase):
    def test_status_policy_is_explicit_for_head_and_html_get(self):
        url = "https://example.com/missing"
        for status in (400, 401, 403, 404, 408, 410, 429, 500, 502, 503):
            for generic in (True, False):
                with self.subTest(status=status, generic=generic):
                    fake = FakeHTTP({url: HttpResponse(status, {"Content-Type": "video/mp4"}, b"")})
                    request = ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"])
                    if generic:
                        result = GenericHTMLMediaResolver().resolve(validate_url_safety(url, host_resolver=request.host_resolver), request)
                    else:
                        result = default_link_resolver_registry().resolve(url, request)
                    self.assertEqual(result["status"], "skipped")
                    self.assertEqual(result["details"]["status_code"], status)
                    self.assertEqual(result["details"]["error_code"], f"http_{status}")
                    self.assertEqual(result["details"]["retryable"], status in (408, 429) or status >= 500)
                    self.assertEqual(result["skip_reason"], "requires_auth" if status in (401, 403) else "resolver_error")
                    self.assertEqual(len(fake.calls), 2 if status in (401, 403) and not generic else 1)

    def test_retry_after_is_bounded_and_malformed_values_are_ignored(self):
        url = "https://example.com/rate-limited"
        values = (("120", 120), ("9999999999", 86400), ("-12", None), ("invalid", None), ("1e10", None))
        for value, expected in values:
            with self.subTest(value=value):
                fake = FakeHTTP({url: HttpResponse(429, {"Retry-After": value}, b"")})
                result = default_link_resolver_registry().resolve(url, ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]))
                self.assertEqual(result["details"].get("retry_after_seconds"), expected)
        value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
        fake = FakeHTTP({url: HttpResponse(503, {"Retry-After": value}, b"")})
        result = default_link_resolver_registry().resolve(url, ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]))
        self.assertTrue(115 <= result["details"]["retry_after_seconds"] <= 120)

    def test_validated_regional_redirect_uses_dedicated_adapter_and_preserves_origin(self):
        original = "https://example.com/shared"
        final = "https://cn.pornhub.com/view_video.php?viewkey=ph123abc"
        for generic in (False, True):
            with self.subTest(generic=generic):
                fake = FakeHTTP({original: HttpResponse(302, {"Location": final}, b""), final: HttpResponse(200, {"Content-Type": "text/html"}, b"")})
                request = ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"])
                with patch("mediagent.platforms.pornhub.client.probe_exact_video", return_value={"viewkey": "ph123abc"}) as probe:
                    if generic:
                        result = GenericHTMLMediaResolver().resolve(validate_url_safety(original, host_resolver=request.host_resolver), request)
                    else:
                        result = default_link_resolver_registry().resolve(original, request)
                self.assertEqual(result["resolver"], "pornhub_exact_video")
                self.assertEqual(result["original_url"], original)
                self.assertEqual(result["normalized_url"], final)
                self.assertEqual(result["canonical_url"], "https://www.pornhub.com/view_video.php?viewkey=ph123abc")
                self.assertEqual(probe.call_args.args[0], result["canonical_url"])

    def test_unsafe_redirect_is_rejected_before_provider_or_html_processing(self):
        original = "https://example.com/shared"
        for target in ("https://user:secret@jp.pornhub.com/embed/ph123abc", "https://127.0.0.1/embed/ph123abc"):
            with self.subTest(target=target):
                fake = FakeHTTP({original: HttpResponse(302, {"Location": target}, b"")})
                with patch("mediagent.platforms.pornhub.client.probe_exact_video") as probe:
                    result = default_link_resolver_registry().resolve(original, ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]))
                self.assertEqual(result["skip_reason"], "unsafe_url")
                self.assertEqual(len(fake.calls), 1)
                probe.assert_not_called()

    def test_regional_redirect_to_non_exact_page_is_reserved(self):
        original = "https://example.com/shared"
        final = "https://jp.pornhub.com/playlist/123"
        fake = FakeHTTP({original: HttpResponse(302, {"Location": final}, b""), final: HttpResponse(200, {"Content-Type": "text/html"}, b'<meta property="og:image" content="https://example.com/banner.jpg">')})
        with patch("mediagent.platforms.pornhub.client.probe_exact_video") as probe:
            result = default_link_resolver_registry().resolve(original, ResolveRequest(http_client=fake, host_resolver=lambda host: ["93.184.216.34"]))
        self.assertEqual(result["resolver"], "reserved_platform_page")
        self.assertEqual(result["skip_reason"], "pornhub_url_unsupported")
        self.assertFalse(result["details"]["retryable"])
        probe.assert_not_called()
