"""Offline FxEmbed contracts and managed exact-post download regressions."""

import asyncio
import base64
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent import cli
from mediagent.core import assets, db, lifecycle
from mediagent.core.http import HttpResponse
from mediagent.core.links import (
    ResolveRequest,
    default_link_resolver_registry,
    resolution_to_media_item,
)
from mediagent.core.tooling import ToolContext
from mediagent.platforms.x import fxembed
from mediagent.tools import add_tools, library_content_tools
from tests.test_link_tools import FakeLinkHttpClient


POST_ID = "1234567890123456789"
POST_URL = f"https://x.com/example/status/{POST_ID}"
API_URL = f"{fxembed.PUBLIC_ENDPOINT}/status/{POST_ID}"
PHOTO_URL = "https://pbs.twimg.com/media/example.jpg?format=jpg&name=orig"
PHOTO_BYTES = b"\xff\xd8\xff\xe0example\xff\xd9"
VIDEO_URL = "https://video.twimg.com/ext_tw_video/example/vid/avc1/example.mp4"
VIDEO_BYTES = (
    b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00\x00\x00\x00\x08moov\x00\x00\x00\x08mdat"
)
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def payload(*, entries=None, protected=False, post_id=POST_ID):
    return {
        "code": 200,
        "status": {
            "type": "status",
            "id": post_id,
            "text": "Example post",
            "created_timestamp": 1735689600,
            "possibly_sensitive": True,
            "author": {"id": "42", "screen_name": "example", "protected": protected},
            "media": {
                "all": entries
                if entries is not None
                else [{"type": "photo", "url": PHOTO_URL}]
            },
        },
    }


def response(data, status=200):
    return HttpResponse(
        status, {"Content-Type": "application/json"}, json.dumps(data).encode()
    )


def fake_client(data=None, *, api_url=API_URL):
    client = FakeLinkHttpClient()
    client.gets[api_url] = response(data or payload())
    client.gets[PHOTO_URL] = HttpResponse(
        200,
        {"Content-Type": "image/jpeg", "Content-Length": str(len(PHOTO_BYTES))},
        PHOTO_BYTES,
    )
    client.gets[VIDEO_URL] = HttpResponse(
        200, {"Content-Type": "video/mp4"}, VIDEO_BYTES
    )
    return client


class XResolverTests(unittest.TestCase):
    def resolve(self, client=None, *, env=None, url=POST_URL, host_resolver=None):
        return default_link_resolver_registry().resolve(
            url,
            ResolveRequest(
                http_client=client or fake_client(),
                env=env or {},
                host_resolver=host_resolver or (lambda _host: ["1.1.1.1"]),
            ),
        )

    def test_exact_url_aliases_share_stable_post_identity(self):
        for url in (
            POST_URL,
            f"https://www.twitter.com/old_name/status/{POST_ID}?s=20#fragment",
            f"https://mobile.x.com/i/web/status/{POST_ID}",
            f"https://x.com/i/status/{POST_ID}",
            POST_URL + "/photo/2",
            POST_URL + "/video/1",
        ):
            with self.subTest(url=url):
                link = fxembed.parse_post_link(url)
                self.assertEqual(link.post_id, POST_ID)
                self.assertEqual(
                    link.canonical_url, f"https://x.com/i/web/status/{POST_ID}"
                )

    def test_non_post_and_unsafe_url_shapes_are_rejected(self):
        for url in (
            "https://x.com/example",
            "https://x.com/home",
            "https://x.com/example/status/123/extra",
            "http://x.com/example/status/123",
            "https://x.com:444/example/status/123",
            "https://user:secret@x.com/example/status/123",
            "https://x.com.evil.test/example/status/123",
            "https://x.com/example/status/000",
            "https://x.com/example/status/123;param",
        ):
            with self.subTest(url=url):
                self.assertIsNone(fxembed.parse_post_link(url))
        client = fake_client()
        result = self.resolve(client, url="https://x.com/example")
        self.assertEqual(result["skip_reason"], "x_url_unsupported")
        self.assertEqual(client.calls, [])

    def test_public_sensitive_photo_uses_original_and_whitelisted_metadata(self):
        data = payload(
            entries=[
                {
                    "type": "photo",
                    "url": "https://pbs.twimg.com/media/example.jpg?name=small",
                }
            ]
        )
        data["credential"] = "never-persist-top-level"
        data["status"]["author"]["cookie"] = "never-persist-author"
        client = fake_client(data)
        result = self.resolve(
            client,
            env={
                "X_ACCESS_TOKEN": "never-send-token",
                "X_CREDENTIALS_FILE": "/does/not/exist",
            },
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["origin_source"], "x")
        self.assertEqual(result["resolved_media_url"], PHOTO_URL)
        self.assertTrue(result["details"]["x"]["possibly_sensitive"])
        self.assertFalse(result["details"]["x"]["source_protected"])
        self.assertEqual(client.calls, [("GET_LIMITED", API_URL)])
        self.assertNotIn("never-", json.dumps(result))
        self.assertNotIn("Authorization", client.headers_by_call[0][2])
        self.assertNotIn("Cookie", client.headers_by_call[0][2])
        item = resolution_to_media_item(result)
        self.assertEqual(item["author_name"], "example")
        self.assertEqual(item["metadata"]["files"][0]["part"], "p0")
        self.assertNotIn("download_context", json.dumps(item["metadata"]))

    def test_mixed_attachments_keep_order_select_best_mp4_and_ignore_quotes(self):
        formats = [
            {
                "container": "m3u8",
                "url": "https://video.twimg.com/master.m3u8",
                "bitrate": 9999,
            },
            {
                "container": "mp4",
                "codec": "h264",
                "url": VIDEO_URL + "?low=1",
                "bitrate": 100,
            },
            {"container": "mp4", "codec": "h264", "url": VIDEO_URL, "bitrate": 200},
            {
                "container": "mp4",
                "codec": "hevc",
                "url": VIDEO_URL + "?hevc=1",
                "bitrate": 500,
            },
        ]
        data = payload(
            entries=[
                {"type": "photo", "url": PHOTO_URL},
                {"type": "video", "url": VIDEO_URL, "formats": formats},
                {"type": "gif", "url": VIDEO_URL},
            ]
        )
        data["status"]["quote"] = payload()["status"]
        result = self.resolve(fake_client(data))
        self.assertEqual(
            [c["media_type"] for c in result["media_candidates"]],
            ["photo", "video", "video"],
        )
        self.assertEqual(
            [c["part"] for c in result["media_candidates"]], ["p0", "p1", "p2"]
        )
        self.assertEqual(result["media_candidates"][1]["url"], VIDEO_URL)
        self.assertEqual(result["media_count"], 3)

    def test_four_photo_collection_and_split_media_lists(self):
        data = payload(
            entries=[
                {"type": "photo", "url": PHOTO_URL.replace("example", f"image{i}")}
                for i in range(4)
            ]
        )
        self.assertEqual(self.resolve(fake_client(data))["media_count"], 4)
        data["status"]["media"] = {
            "photos": [{"type": "photo", "url": PHOTO_URL}],
            "videos": [{"type": "gif", "url": VIDEO_URL}],
        }
        self.assertEqual(self.resolve(fake_client(data))["media_count"], 2)

    def test_public_protected_is_rejected_and_self_hosted_preserves_flag(self):
        data = payload(protected=True)
        self.assertEqual(
            self.resolve(fake_client(data))["skip_reason"], "x_post_requires_auth"
        )
        base = "http://127.0.0.1:8787/2/twitter"
        client = fake_client(data, api_url=f"{base}/status/{POST_ID}")
        result = self.resolve(
            client,
            env={
                "MEDIAGENT_X_ENDPOINT_MODE": "self_hosted",
                "MEDIAGENT_X_ENDPOINT_URL": base,
            },
            host_resolver=lambda host: (
                ["127.0.0.1"] if host == "127.0.0.1" else ["1.1.1.1"]
            ),
        )
        self.assertEqual(result["status"], "resolved")
        self.assertTrue(result["details"]["x"]["source_protected"])
        self.assertEqual(result["details"]["x"]["endpoint_mode"], "self_hosted")
        self.assertEqual(client.calls, [("GET_LIMITED", f"{base}/status/{POST_ID}")])
        self.assertNotIn(base, json.dumps(result))

    def test_endpoint_config_is_explicit_and_private_exception_is_scoped(self):
        invalid = [
            {"MEDIAGENT_X_ENDPOINT_MODE": "unknown"},
            {"MEDIAGENT_X_ENDPOINT_MODE": "self_hosted"},
            {"MEDIAGENT_X_ENDPOINT_URL": "https://another.test/2"},
        ]
        for url in (
            "http://example.test/2",
            "https://user:secret@example.test/2",
            "https://example.test/2?token=secret",
            "https://example.test/../2",
            "https://example.test/%2e/2",
            "https://example.test/v1",
            "http://169.254.169.254/2",
            "https://api.fxtwitter.com/2",
        ):
            invalid.append(
                {
                    "MEDIAGENT_X_ENDPOINT_MODE": "self_hosted",
                    "MEDIAGENT_X_ENDPOINT_URL": url,
                }
            )
        for env in invalid:
            with self.subTest(env=env):
                client = fake_client()
                result = self.resolve(client, env=env)
                self.assertEqual(result["skip_reason"], "x_endpoint_config_invalid")
                self.assertEqual(client.calls, [])
        # Configuring a private endpoint does not authorize private input URLs.
        result = self.resolve(url="https://127.0.0.1/media.jpg")
        self.assertEqual(result["skip_reason"], "unsafe_url")

    def test_self_hosted_loopback_ipv6_and_private_service_names(self):
        for base, ip in [
            ("http://[::1]:8787/2", "::1"),
            ("http://fxembed:8787/2/twitter", "172.18.0.2"),
            ("https://endpoint.test/2", "1.1.1.1"),
        ]:
            with self.subTest(base=base):
                client = fake_client(api_url=f"{base}/status/{POST_ID}")
                result = self.resolve(
                    client,
                    env={
                        "MEDIAGENT_X_ENDPOINT_MODE": "self_hosted",
                        "MEDIAGENT_X_ENDPOINT_URL": base,
                    },
                    host_resolver=lambda host: ["1.1.1.1"] if host == "x.com" else [ip],
                )
                self.assertEqual(result["status"], "resolved")

    def test_api_http_and_json_errors_are_bounded_and_retryable_when_needed(self):
        cases = [
            (401, "x_post_requires_auth", True),
            (403, "x_post_requires_auth", True),
            (404, "x_post_unavailable", False),
            (429, "x_rate_limited", True),
            (503, "x_endpoint_failed", True),
        ]
        for code, expected, retryable in cases:
            for http_status in [True, False]:
                with self.subTest(code=code, http_status=http_status):
                    client = fake_client()
                    client.gets[API_URL] = response(
                        {
                            "code": 200 if http_status else code,
                            "message": "never-show-secret",
                        },
                        status=code if http_status else 200,
                    )
                    result = self.resolve(client)
                    self.assertEqual(result["skip_reason"], expected)
                    self.assertEqual(result["details"]["retryable"], retryable)
                    self.assertEqual(len(client.calls), 1)
                    self.assertNotIn("never-show-secret", json.dumps(result))

    def test_failed_self_host_does_not_fall_back_or_follow_redirect(self):
        base = "https://endpoint.test/2"
        url = f"{base}/status/{POST_ID}"
        for status in [302, 503]:
            client = fake_client(api_url=url)
            client.gets[url] = HttpResponse(
                status,
                {"Location": "https://api.fxtwitter.com/2/status/" + POST_ID},
                b"",
            )
            result = self.resolve(
                client,
                env={
                    "MEDIAGENT_X_ENDPOINT_MODE": "self_hosted",
                    "MEDIAGENT_X_ENDPOINT_URL": base,
                },
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(client.calls, [("GET_LIMITED", url)])

    def test_transport_failure_is_sanitized_and_missing_dns_is_retryable(self):
        client = fake_client()
        with patch.object(
            client,
            "get_limited",
            side_effect=TimeoutError("never-show-private-url-or-cookie"),
        ):
            result = self.resolve(client)
        self.assertEqual(result["skip_reason"], "x_endpoint_unreachable")
        self.assertTrue(result["details"]["retryable"])
        self.assertNotIn("never-show", json.dumps(result))
        result = self.resolve(
            env={
                "MEDIAGENT_X_ENDPOINT_MODE": "self_hosted",
                "MEDIAGENT_X_ENDPOINT_URL": "https://missing.test/2",
            },
            host_resolver=lambda host: ["1.1.1.1"] if host == "x.com" else [],
        )
        self.assertEqual(result["skip_reason"], "x_endpoint_unreachable")

    def test_implicitly_followed_api_redirect_is_rejected(self):
        client = fake_client()
        client.gets[API_URL] = HttpResponse(
            200,
            {"Content-Type": "application/json"},
            json.dumps(payload()).encode(),
            "https://another.test/2/status/" + POST_ID,
        )
        self.assertEqual(
            self.resolve(client)["skip_reason"], "x_endpoint_redirect_rejected"
        )

    def test_tombstones_and_missing_visibility_flags_are_rejected(self):
        data = payload()
        data["status"]["type"] = "tombstone"
        self.assertEqual(
            self.resolve(fake_client(data))["skip_reason"], "x_post_unavailable"
        )
        data = payload()
        del data["status"]["possibly_sensitive"]
        self.assertEqual(
            self.resolve(fake_client(data))["skip_reason"], "x_payload_invalid"
        )

    def test_metadata_bound_and_malformed_payloads_do_not_produce_candidates(self):
        bad = [[], {"code": True}, {"code": 200, "status": []}, payload(post_id="99")]
        for key, value in [
            ("author", {}),
            ("media", "invalid"),
            ("media", {"all": [None]}),
            ("media", {"all": [{}] * 5}),
        ]:
            data = payload()
            data["status"][key] = value
            bad.append(data)
        for data in bad:
            with self.subTest(data=data):
                client = fake_client()
                client.gets[API_URL] = response(data)
                result = self.resolve(client)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["media_candidates"], [])
        for body, mime in [
            (b"{", "application/json"),
            (b"x" * (fxembed.MAX_METADATA_BYTES + 1), "application/json"),
            (b"<html>login</html>", "text/html"),
        ]:
            client = fake_client()
            client.gets[API_URL] = HttpResponse(200, {"Content-Type": mime}, body)
            self.assertEqual(self.resolve(client)["status"], "skipped")

    def test_no_media_and_non_native_urls_fail_without_preview_fallback(self):
        self.assertEqual(
            self.resolve(fake_client(payload(entries=[])))["skip_reason"],
            "x_no_native_media",
        )
        for url in [
            "https://pbs.twimg.com/profile_images/avatar.jpg",
            "https://evil.test/media.jpg",
            "https://pbs.twimg.com/media/../private.jpg",
            "https://pbs.twimg.com/media/%2e%2e/private.jpg",
            "https://user:secret@pbs.twimg.com/media/photo.jpg",
            "https://video.twimg.com/video.m3u8",
            "http://pbs.twimg.com/media/photo.jpg",
        ]:
            with self.subTest(url=url):
                data = payload(entries=[{"type": "photo", "url": url}])
                result = self.resolve(fake_client(data))
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["resolver"], "x_exact_post")


class XManagedAddTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = fake_client()
        self.context = ToolContext(
            cwd=self.root,
            env={"MEDIAGENT_AUTO_TAG": "false"},
            dry_run=False,
            run_id="x-add-test",
            data_dir=self.root / "data",
            library_dir=self.root / "library",
            db_path=self.root / "data" / "mediagent.sqlite3",
            http_client=self.client,
        )
        self.dns = patch(
            "mediagent.core.links.resolve_host_ips", return_value=["1.1.1.1"]
        )
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def add(self, url=POST_URL):
        return asyncio.run(add_tools.media_add(self.context, {"input": url}))

    def sources(self, asset_id):
        with db.connect(self.context.db_path) as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT mi.platform, mi.remote_id FROM asset_sources s JOIN media_items mi ON mi.id = s.media_item_id WHERE s.asset_id = ?",
                    (asset_id,),
                )
            ]

    def test_add_records_asset_source_and_is_idempotent_across_aliases(self):
        first = self.add()
        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["summary"]["files_downloaded"], 1)
        self.assertEqual(len(first.data["asset_ids"]), 1)
        self.assertEqual(Path(first.artifacts[0]["path"]).read_bytes(), PHOTO_BYTES)
        second = self.add(f"https://twitter.com/renamed/status/{POST_ID}?s=20")
        self.assertTrue(second.is_success)
        self.assertEqual(first.data["asset_ids"], second.data["asset_ids"])
        self.assertEqual(second.data["summary"]["files_downloaded"], 0)
        with db.connect(self.context.db_path) as connection:
            asset = assets.get_asset(connection, first.data["asset_ids"][0])
            self.assertEqual(asset["source_count"], 1)
            stored = connection.execute(
                "SELECT metadata_json FROM media_items WHERE platform = 'x'"
            ).fetchone()[0]
        self.assertIn('"possibly_sensitive": true', stored)
        self.assertEqual(
            self.sources(first.data["asset_ids"][0]),
            [{"platform": "x", "remote_id": POST_ID}],
        )
        self.assertNotIn("download_context", stored)
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_multiple_posts_deduplicate_bytes_without_losing_sources(self):
        first = self.add()
        other_id = "1234567890123456790"
        self.client.gets[f"{fxembed.PUBLIC_ENDPOINT}/status/{other_id}"] = response(
            payload(post_id=other_id)
        )
        second = self.add(f"https://x.com/other/status/{other_id}")
        self.assertTrue(second.is_success)
        self.assertEqual(second.data["summary"]["files_deduplicated"], 1)
        self.assertEqual(first.data["asset_ids"], second.data["asset_ids"])
        self.assertEqual(
            {
                source["remote_id"]
                for source in self.sources(first.data["asset_ids"][0])
            },
            {POST_ID, other_id},
        )

    def test_mixed_partial_download_repairs_failed_attachment_only(self):
        self.client.gets[API_URL] = response(
            payload(
                entries=[
                    {"type": "photo", "url": PHOTO_URL},
                    {"type": "gif", "url": VIDEO_URL},
                ]
            )
        )
        self.client.gets[VIDEO_URL] = HttpResponse(
            200, {"Content-Type": "video/mp4"}, b"<html>not-video</html>"
        )
        first = self.add()
        self.assertFalse(first.is_success)
        self.assertEqual(first.data["summary"]["partial"], 1)
        self.assertEqual(first.data["summary"]["files_failed"], 1)
        self.assertEqual(first.data["summary"]["files_downloaded"], 1)
        self.client.gets[VIDEO_URL] = HttpResponse(
            200, {"Content-Type": "video/mp4"}, VIDEO_BYTES
        )
        self.client.calls.clear()
        second = self.add()
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(
            self.client.calls, [("GET_LIMITED", API_URL), ("GET_LIMITED", VIDEO_URL)]
        )
        self.assertEqual(second.data["summary"]["files_failed"], 0)
        self.assertFalse(list(self.root.rglob("*.partial")))

    def test_media_redirect_cannot_leave_native_cdn(self):
        self.client.gets[PHOTO_URL] = HttpResponse(
            302, {"Location": "https://another.test/media/photo.jpg"}, b""
        )
        result = self.add()
        self.assertFalse(result.is_success)
        self.assertEqual(result.data["summary"]["files_failed"], 1)
        self.assertNotIn(
            "https://another.test/media/photo.jpg",
            [url for _, url in self.client.calls],
        )
        self.assertEqual(result.artifacts, [])

    def test_cdn_dns_cannot_resolve_to_a_private_address(self):
        with patch(
            "mediagent.core.links.resolve_host_ips",
            side_effect=lambda host: (
                ["10.0.0.2"] if host == "pbs.twimg.com" else ["1.1.1.1"]
            ),
        ):
            result = self.add()
        self.assertFalse(result.is_success)
        self.assertNotIn(("GET_LIMITED", PHOTO_URL), self.client.calls)
        self.assertEqual(result.artifacts, [])

    def test_incomplete_or_wrong_mime_photo_never_finalizes(self):
        for content, headers in [
            (PHOTO_BYTES[:-2], {"Content-Type": "image/jpeg"}),
            (
                PHOTO_BYTES,
                {
                    "Content-Type": "image/jpeg",
                    "Content-Length": str(len(PHOTO_BYTES) + 1),
                },
            ),
            (PNG_BYTES, {"Content-Type": "image/png"}),
        ]:
            with self.subTest(headers=headers):
                self.client.gets[PHOTO_URL] = HttpResponse(200, headers, content)
                result = self.add()
                self.assertFalse(result.is_success)
                self.assertEqual(result.artifacts, [])
                self.assertFalse(list(self.root.rglob("*.partial")))

    def test_rate_limit_is_failure_and_durable_queue_remains_retryable(self):
        self.client.gets[API_URL] = response({"code": 429})
        result = self.add()
        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "x_rate_limited")
        links = db.list_links(self.context.db_path)
        self.assertEqual(links[0]["skip_reason"], "x_rate_limited")
        self.assertTrue(
            db.link_resolution_retryable(
                status="skipped",
                resolution=links[0]["resolution"],
                skip_reason=links[0]["skip_reason"],
            )
        )
        self.assertEqual(result.data["asset_ids"], [])

    def test_dry_run_previews_without_db_or_media_writes(self):
        self.context.dry_run = True
        result = self.add()
        self.assertTrue(result.is_success)
        self.assertEqual(len(result.data["planned_downloads"]), 1)
        self.assertFalse(self.context.db_path.exists())
        self.assertFalse(self.context.library_dir.exists())
        self.assertEqual(self.client.calls, [("GET_LIMITED", API_URL)])

    def test_asset_remove_restore_preserves_x_source(self):
        added = self.add()
        asset_id = added.data["asset_ids"][0]
        removed = library_content_tools.remove_asset(
            self.context, {"asset_id": asset_id}
        )
        self.assertTrue(removed.is_success, removed.to_dict())
        restored = library_content_tools.restore_asset(
            self.context, {"asset_id": asset_id}
        )
        self.assertTrue(restored.is_success, restored.to_dict())
        with db.connect(self.context.db_path) as connection:
            asset = assets.get_asset(connection, asset_id)
        self.assertEqual(asset["source_count"], 1)
        self.assertEqual(self.sources(asset_id)[0]["platform"], "x")

    def test_purged_asset_is_not_revived_by_readding_x_post(self):
        added = self.add()
        asset_id = added.data["asset_ids"][0]
        self.assertTrue(
            library_content_tools.remove_asset(
                self.context, {"asset_id": asset_id}
            ).is_success
        )
        purged = lifecycle.purge_trash(
            self.context.db_path,
            library_root=self.context.library_dir,
            retention_days=0,
            dry_run=False,
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
        self.assertEqual(purged["assets_purged"], 1)
        self.client.calls.clear()
        repeated = self.add()
        self.assertEqual(repeated.data["summary"]["blocked_purged"], 1)
        self.assertEqual(repeated.data["summary"]["files_downloaded"], 0)
        self.assertNotIn(("GET_LIMITED", PHOTO_URL), self.client.calls)
        self.assertEqual(
            assets.load_asset(self.context.db_path, asset_id)["state"], "purged"
        )

    def test_real_cli_add_returns_asset_and_error_exit_codes(self):
        env = {
            "MEDIAGENT_DATA_DIR": str(self.context.data_dir),
            "MEDIAGENT_LIBRARY_DIR": str(self.context.library_dir),
            "MEDIAGENT_DB_PATH": str(self.context.db_path),
        }
        with (
            patch(
                "mediagent.cli.ToolContext.from_env",
                side_effect=lambda **kwargs: self.context,
            ),
            patch.dict("os.environ", env, clear=True),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.run(["add", POST_URL, "--json"])
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(len(result["data"]["asset_ids"]), 1)
            self.client.gets[API_URL] = response({"code": 401})
            with redirect_stdout(io.StringIO()):
                code = cli.run(["add", POST_URL, "--json"])
            self.assertNotEqual(code, 0)


class XFormatTests(unittest.TestCase):
    def test_native_format_validation_rejects_truncation_and_html(self):
        for mime, body in [
            ("image/jpeg", PHOTO_BYTES),
            ("image/png", PNG_BYTES),
            ("video/mp4", VIDEO_BYTES),
        ]:
            self.assertTrue(fxembed.valid_media_bytes(body, mime))
            self.assertFalse(fxembed.valid_media_bytes(body[:-1], mime))
            self.assertFalse(fxembed.valid_media_bytes(b"<html>login</html>", mime))
        self.assertFalse(fxembed.valid_media_bytes(b"", "video/mp4"))


if __name__ == "__main__":
    unittest.main()
