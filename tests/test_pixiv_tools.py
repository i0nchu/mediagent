import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.platforms.pixiv import client as pixiv_client
from mediagent.platforms.pixiv import links as pixiv_links
from mediagent.platforms.pixiv import parser as pixiv_parser
from mediagent.tools.defaults import create_default_registry


FIXTURES = Path(__file__).resolve().parent / "fixtures"
PUBLIC_TEST_IP = "1.1.1.1"


class FakePixivHttpClient:
    def __init__(self) -> None:
        self.responses: list[HttpResponse] = []
        self.requests: list[tuple[str, str, dict]] = []

    def queue(self, response: HttpResponse) -> None:
        self.responses.append(response)

    def get_json(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("GET", url, headers or {}))
        return self.responses.pop(0)

    def get(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("GET", url, headers or {}))
        return self.responses.pop(0)

    def post_form(self, url: str, data: dict[str, str], *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("POST", url, data))
        return self.responses.pop(0)


class PixivToolTests(unittest.TestCase):
    def test_pixiv_auth_login_start_generates_manual_url(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(env={}, dry_run=True)

        result = asyncio.run(registry.run("pixiv.auth.login", {}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["mode"], "start")
        self.assertIn("https://app-api.pixiv.net/web/v1/login?", result.data["authorization_url"])
        self.assertIn("code_challenge_method=S256", result.data["authorization_url"])
        self.assertIn("client=pixiv-android", result.data["authorization_url"])
        self.assertIn("code_verifier", result.data)
        self.assertEqual(
            result.data["redirect_uri"],
            "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback",
        )

    def test_pixiv_auth_login_exchange_writes_credential_file(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": temp_dir},
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.auth.login",
                    {
                        "code": "one-time-code",
                        "code_verifier": "pkce-verifier",
                        "credential_output_path": str(credential_path),
                    },
                    context,
                )
            )
            written = json.loads(credential_path.read_text(encoding="utf-8"))
            mode = credential_path.stat().st_mode & 0o777

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["credentials_written"])
        self.assertEqual(fake.requests[0][0], "POST")
        self.assertEqual(fake.requests[0][2]["grant_type"], "authorization_code")
        self.assertEqual(fake.requests[0][2]["code"], "one-time-code")
        self.assertEqual(fake.requests[0][2]["code_verifier"], "pkce-verifier")
        self.assertEqual(written["access_token"], "secret-access")
        self.assertEqual(written["refresh_token"], "secret-refresh")
        self.assertEqual(written["user_id"], "99")
        self.assertEqual(mode, 0o600)
        self.assertNotIn("one-time-code", str(result.to_dict()))
        self.assertNotIn("secret-access", str(result.to_dict()))
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_pixiv_auth_login_exchange_accepts_callback_url(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        context = ToolContext.from_env(env={}, http_client=fake)

        result = asyncio.run(
            registry.run(
                "pixiv.auth.login",
                {
                    "callback_url": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=x&code=one-time-code&via=login",
                    "code_verifier": "pkce-verifier",
                },
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][2]["code"], "one-time-code")
        self.assertNotIn("one-time-code", str(result.to_dict()))

    def test_pixiv_auth_login_exchange_rejects_callback_url_without_code(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(env={})

        result = asyncio.run(
            registry.run(
                "pixiv.auth.login",
                {
                    "callback_url": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=x",
                    "code_verifier": "pkce-verifier",
                },
                context,
            )
        )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_auth_login_missing_code")
        self.assertNotIn("state=x", str(result.to_dict()))

    def test_pixiv_auth_login_exchange_dry_run_does_not_call_network(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        context = ToolContext.from_env(env={}, dry_run=True, http_client=fake)

        result = asyncio.run(
            registry.run(
                "pixiv.auth.login",
                {
                    "code": "one-time-code",
                    "code_verifier": "pkce-verifier",
                },
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["mode"], "exchange")
        self.assertTrue(result.data["would_exchange"])
        self.assertEqual(fake.requests, [])

    def test_pixiv_auth_login_exchange_redacts_failed_response_codes(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(
            HttpResponse(
                400,
                {},
                json.dumps(
                    {
                        "error": {
                            "message": "bad code one-time-code",
                            "code": "upstream-auth-code",
                        }
                    }
                ).encode(),
            )
        )
        context = ToolContext.from_env(env={}, http_client=fake)

        result = asyncio.run(
            registry.run(
                "pixiv.auth.login",
                {
                    "code": "one-time-code",
                    "code_verifier": "pkce-verifier",
                },
                context,
            )
        )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_auth_login_exchange_failed")
        self.assertEqual(result.data["status_code"], 400)
        self.assertNotIn("one-time-code", str(result.to_dict()))
        self.assertNotIn("upstream-auth-code", str(result.to_dict()))

    def test_pixiv_auth_login_exchange_rejects_credential_file_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside_path = Path(temp_dir) / "outside" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.auth.login",
                    {
                        "code": "one-time-code",
                        "code_verifier": "pkce-verifier",
                        "credential_output_path": str(outside_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(result.error.category.value, "filesystem")

    def test_pixiv_auth_status_uses_refresh_token_without_writing(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        context = ToolContext.from_env(
            env={"PIXIV_REFRESH_TOKEN": "secret-refresh"},
            http_client=fake,
        )

        result = asyncio.run(registry.run("pixiv.auth.status", {}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["account_id"], "99")
        self.assertFalse(result.data["credentials_written"])
        self.assertNotIn("secret-refresh", str(result.to_dict()))
        self.assertNotIn("secret-access", str(result.to_dict()))

    def test_pixiv_auth_status_refreshes_expired_access_token(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        context = ToolContext.from_env(
            env={
                "PIXIV_ACCESS_TOKEN": "expired-access",
                "PIXIV_REFRESH_TOKEN": "secret-refresh",
                "PIXIV_USER_ID": "99",
                "PIXIV_TOKEN_EXPIRES_AT": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
            http_client=fake,
        )

        result = asyncio.run(registry.run("pixiv.auth.status", {}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][0], "POST")
        self.assertEqual(result.data["session"]["account_id"], "99")
        self.assertNotIn("expired-access", str(result.to_dict()))

    def test_generic_auth_session_status_routes_to_pixiv(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        context = ToolContext.from_env(
            env={"PIXIV_REFRESH_TOKEN": "secret-refresh"},
            http_client=fake,
        )

        result = asyncio.run(registry.run("auth.session.status", {"provider": "pixiv"}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["provider"], "pixiv")
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_generic_auth_session_status_accepts_pixiv_access_token_from_env(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"user":{"id":99}}'))
        context = ToolContext.from_env(
            env={
                "PIXIV_ACCESS_TOKEN": "secret-access",
                "PIXIV_USER_ID": "99",
                "PIXIV_TOKEN_EXPIRES_AT": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
            http_client=fake,
        )

        result = asyncio.run(registry.run("auth.session.status", {"provider": "pixiv"}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["status"], "usable")
        self.assertEqual(fake.requests[0][0], "GET")
        self.assertEqual(fake.requests[0][2]["Authorization"], "Bearer secret-access")
        self.assertNotIn("secret-access", str(result.to_dict()))

    def test_generic_auth_session_status_honors_pixiv_credential_refs(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "pixiv-oauth.json"
            credential_path.write_text(json.dumps({"refresh_token": "secret-from-ref"}), encoding="utf-8")
            context = ToolContext.from_env(env={}, cwd=Path(temp_dir), http_client=fake)

            result = asyncio.run(
                registry.run(
                    "auth.session.status",
                    {
                        "provider": "pixiv",
                        "credential_refs": [
                            {"source": "file", "name": str(credential_path), "key": "refresh_token"},
                        ],
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][2]["refresh_token"], "secret-from-ref")
        self.assertEqual(result.data["session"]["provider"], "pixiv")
        self.assertNotIn("secret-from-ref", str(result.to_dict()))

    def test_generic_auth_session_status_accepts_pixiv_access_token_credential_refs(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"user":{"id":99}}'))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "pixiv-oauth.json"
            credential_path.write_text(
                json.dumps(
                    {
                        "access_token": "secret-access-from-ref",
                        "user_id": "99",
                        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            context = ToolContext.from_env(env={}, cwd=Path(temp_dir), http_client=fake)

            result = asyncio.run(
                registry.run(
                    "auth.session.status",
                    {
                        "provider": "pixiv",
                        "credential_refs": [
                            {"source": "file", "name": str(credential_path), "key": "access_token"},
                            {"source": "file", "name": str(credential_path), "key": "user_id"},
                            {"source": "file", "name": str(credential_path), "key": "expires_at"},
                        ],
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["status"], "usable")
        self.assertEqual(fake.requests[0][2]["Authorization"], "Bearer secret-access-from-ref")
        self.assertNotIn("secret-access-from-ref", str(result.to_dict()))

    def test_generic_auth_session_refresh_routes_to_pixiv(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "auth.session.refresh",
                    {
                        "provider": "pixiv",
                        "credential_output_path": str(credential_path),
                    },
                    context,
                )
            )
            written = json.loads(credential_path.read_text(encoding="utf-8"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["provider"], "pixiv")
        self.assertEqual(written["user_id"], "99")
        self.assertNotIn("secret-access", str(result.to_dict()))

    def test_pixiv_auth_refresh_writes_credential_file(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.auth.refresh",
                    {"credential_output_path": str(credential_path)},
                    context,
                )
            )
            written = json.loads(credential_path.read_text(encoding="utf-8"))
            mode = credential_path.stat().st_mode & 0o777

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["credentials_written"])
        self.assertEqual(written["access_token"], "secret-access")
        self.assertEqual(written["refresh_token"], "secret-refresh")
        self.assertEqual(written["user_id"], "99")
        self.assertEqual(mode, 0o600)
        self.assertNotIn("secret-access", str(result.to_dict()))

    def test_pixiv_auth_refresh_rejects_credential_file_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside_path = Path(temp_dir) / "outside" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.auth.refresh",
                    {"credential_output_path": str(outside_path)},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(result.error.category.value, "filesystem")

    def test_pixiv_artwork_id_parses_canonical_localized_and_query_urls(self) -> None:
        self.assertEqual(pixiv_links.pixiv_artwork_id("143734851"), "143734851")
        self.assertEqual(pixiv_links.pixiv_artwork_id("https://www.pixiv.net/artworks/143734851"), "143734851")
        self.assertEqual(pixiv_links.pixiv_artwork_id("https://www.pixiv.net/en/artworks/143734851"), "143734851")
        self.assertEqual(pixiv_links.pixiv_artwork_id("https://www.pixiv.net/member_illust.php?illust_id=143734851"), "143734851")
        self.assertIsNone(pixiv_links.pixiv_artwork_id("https://example.com/artworks/143734851"))

    def test_pixiv_client_get_illust_detail_request_shape(self) -> None:
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1001")}).encode("utf-8")))

        payload, _, status_code = pixiv_client.get_illust_detail(
            http_client=fake,
            access_token="secret-access",
            illust_id="1001",
        )

        self.assertEqual(status_code, 200)
        self.assertIn("illust", payload)
        self.assertIn("/v1/illust/detail?illust_id=1001", fake.requests[0][1])
        self.assertEqual(fake.requests[0][2]["Authorization"], "Bearer secret-access")

    def test_pixiv_artwork_link_resolver_resolves_multipage_artwork(self) -> None:
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1002")}).encode("utf-8")))
        with TemporaryDirectory() as temp_dir:
            context = _pixiv_access_context(temp_dir, fake)

            result = asyncio.run(
                create_default_registry().run(
                    "pixiv.link.resolve",
                    {"url": "https://www.pixiv.net/en/artworks/1002?foo=bar"},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        resolution = result.data["resolution"]
        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["resolver"], "pixiv_artwork_link")
        self.assertEqual(resolution["canonical_url"], "https://www.pixiv.net/artworks/1002")
        self.assertEqual(resolution["remote_id"], "1002")
        self.assertEqual(resolution["media_count"], 2)
        self.assertEqual([candidate["part"] for candidate in resolution["media_candidates"]], ["p0", "p1"])
        self.assertIsNone(resolution["media_candidates"][0]["runtime_headers"])
        self.assertNotIn("secret-access", json.dumps(result.to_dict()))

    def test_pixiv_link_resolve_rejects_non_pixiv_host(self) -> None:
        with TemporaryDirectory() as temp_dir:
            context = _pixiv_access_context(temp_dir, FakePixivHttpClient())

            result = asyncio.run(
                create_default_registry().run(
                    "pixiv.link.resolve",
                    {"url": "https://example.com/artworks/1002"},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_artwork_unsupported_url")

    def test_pixiv_link_resolve_reports_missing_credentials(self) -> None:
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(env={"MEDIAGENT_DATA_DIR": temp_dir}, cwd=Path(temp_dir))

            result = asyncio.run(
                create_default_registry().run(
                    "pixiv.link.resolve",
                    {"illust_id": "1002"},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_auth_missing_credentials")
        self.assertEqual(result.error.details["recommended_tool"], "pixiv.auth.login")

    def test_pixiv_link_resolve_rejects_outside_credential_file_before_read(self) -> None:
        fake = FakePixivHttpClient()
        with TemporaryDirectory() as temp_dir:
            outside = Path(temp_dir) / "outside" / "pixiv-oauth.json"
            outside.parent.mkdir()
            outside.write_text(json.dumps({"access_token": "secret-access"}), encoding="utf-8")
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(Path(temp_dir) / "data"),
                    "PIXIV_CREDENTIALS_FILE": str(outside),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                create_default_registry().run(
                    "pixiv.link.resolve",
                    {"url": "https://www.pixiv.net/artworks/1002"},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(fake.requests, [])

    def test_link_media_sync_downloads_pixiv_artwork_with_referer_and_dedupes_rerun(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1002")}).encode("utf-8")))
        fake.queue(HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "7"}, b"png-one"))
        fake.queue(HttpResponse(200, {"Content-Type": "image/png", "Content-Length": "7"}, b"png-two"))
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1002")}).encode("utf-8")))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = _pixiv_access_context(temp_dir, fake, extra={"MEDIAGENT_DB_PATH": str(db_path)})

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {
                        "db_path": str(db_path),
                        "url": "https://www.pixiv.net/artworks/1002",
                        "write_sidecar_metadata": True,
                    },
                    context,
                )
            )
            second_result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {
                        "db_path": str(db_path),
                        "url": "https://www.pixiv.net/artworks/1002",
                    },
                    context,
                )
            )
            files = db.list_media_files(db_path, platform="pixiv", remote_id="1002")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["files_downloaded"], 2)
        self.assertTrue(second_result.is_success)
        self.assertEqual(second_result.data["summary"]["files_downloaded"], 0)
        self.assertEqual(len(files), 2)
        self.assertTrue(all(file["library_relative_path"].startswith("pixiv/photo/2026/01/") for file in files))
        download_requests = [request for request in fake.requests if request[1].startswith(f"https://{PUBLIC_TEST_IP}/")]
        self.assertEqual(len(download_requests), 2)
        self.assertTrue(all(request[2].get("Referer") == "https://www.pixiv.net/" for request in download_requests))

    def test_link_media_sync_dedupes_pixiv_artwork_against_existing_bookmark_item(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1001")}).encode("utf-8")))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            db.initialize_database(db_path)
            item = pixiv_parser.parse_illust(_pixiv_illust("1001"))
            db.upsert_media_item(db_path, item)
            db.update_media_item_status(db_path, platform="pixiv", remote_id="1001", status="downloaded")
            context = _pixiv_access_context(temp_dir, fake, extra={"MEDIAGENT_DB_PATH": str(db_path)})

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {
                        "db_path": str(db_path),
                        "url": "https://www.pixiv.net/artworks/1001",
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["queued"], 0)
        self.assertEqual(result.data["summary"]["skipped_items"], 1)
        self.assertEqual([request[0] for request in fake.requests], ["GET"])

    def test_pixiv_artwork_resolver_supports_ugoira_zip_candidate(self) -> None:
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, json.dumps({"illust": _pixiv_illust("1003")}).encode("utf-8")))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "ugoira_metadata_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            context = _pixiv_access_context(temp_dir, fake)

            result = asyncio.run(
                create_default_registry().run(
                    "pixiv.link.resolve",
                    {"illust_id": "1003"},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        candidate = result.data["resolution"]["media_candidates"][0]
        self.assertEqual(result.data["resolution"]["media_type"], "video")
        self.assertEqual(candidate["media_type"], "video")
        self.assertEqual(candidate["mime_type"], "application/zip")
        self.assertEqual(candidate["extension"], ".zip")

    def test_pixiv_bookmarks_collect_uses_fixture_and_stores_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "ugoira_metadata_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            credential_path = Path(temp_dir) / "credentials" / "pixiv-oauth.json"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_CREDENTIALS_FILE": str(credential_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.collect",
                    {"restrict": "public", "store_cursor": True},
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="pixiv", cursor_name="bookmarks:public")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["items"], 3)
        self.assertEqual(result.data["summary"]["next_max_bookmark_id"], "555")
        self.assertEqual(cursor["cursor_value"], "555")
        self.assertEqual(result.data["items"][1]["metadata"]["files"][1]["page"], 1)
        self.assertEqual(result.data["items"][2]["media_type"], "video")
        self.assertEqual(result.data["items"][2]["metadata"]["files"][0]["kind"], "ugoira_zip")
        self.assertIn("v1/user/bookmarks/illust", fake.requests[1][1])
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_pixiv_bookmarks_collect_handles_rate_limit(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(429, {"x-rate-limit-remaining": "0"}, b'{"error":{"message":"limited"}}'))
        context = ToolContext.from_env(
            env={"PIXIV_REFRESH_TOKEN": "secret-refresh"},
            http_client=fake,
        )

        result = asyncio.run(registry.run("pixiv.bookmarks.collect", {"user_id": "99"}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_rate_limited")
        self.assertEqual(result.error.category.value, "rate_limit")

    def test_pixiv_bookmarks_collect_handles_restricted_or_expired_session(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(403, {}, b'{"error":{"message":"restricted"}}'))
        context = ToolContext.from_env(
            env={"PIXIV_REFRESH_TOKEN": "secret-refresh"},
            http_client=fake,
        )

        result = asyncio.run(registry.run("pixiv.bookmarks.collect", {"user_id": "99"}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_auth_failed")
        self.assertEqual(result.error.category.value, "auth")

    def test_pixiv_bookmarks_sync_downloads_files_and_marks_downloaded(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two2"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_CREDENTIALS_FILE": str(credential_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 2,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )
            media_files = _media_files(db_path)
            statuses = _media_item_statuses(db_path)
            written_media = sorted(path for path in target_dir.rglob("*") if path.suffix == ".png")
            written_metadata = sorted(path for path in target_dir.rglob("*.json"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["collected"], 3)
        self.assertEqual(result.data["summary"]["discovered"], 2)
        self.assertEqual(result.data["summary"]["downloaded"], 2)
        self.assertEqual(result.data["summary"]["files_downloaded"], 3)
        self.assertEqual(result.data["summary"]["bytes_written"], 12)
        self.assertEqual(statuses[("pixiv", "1001")], "downloaded")
        self.assertEqual(statuses[("pixiv", "1002")], "downloaded")
        self.assertEqual(len(media_files), 3)
        self.assertEqual(len(written_media), 3)
        self.assertEqual(len(written_metadata), 0)
        self.assertEqual(
            sorted(path.relative_to(target_dir).as_posix() for path in written_media),
            [
                "pixiv/photo/2026/01/20260101__pixiv__1001__p0.png",
                "pixiv/photo/2026/01/20260102__pixiv__1002__p0.png",
                "pixiv/photo/2026/01/20260102__pixiv__1002__p1.png",
            ],
        )
        self.assertTrue(all(file["storage_layout"] == "scanner-friendly-v2" for file in media_files))
        self.assertTrue(all(file["file_health"] == "valid" for file in media_files))
        self.assertTrue(all(file["library_relative_path"].startswith("pixiv/photo/2026/01/") for file in media_files))
        self.assertTrue(
            all(
                request[2].get("Referer") == "https://www.pixiv.net/"
                for request in fake.requests
                if "i.pximg.net" in request[1]
            )
        )
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_pixiv_bookmarks_sync_skips_downloaded_items_on_second_run(self) -> None:
        registry = create_default_registry()
        first_fake = FakePixivHttpClient()
        first_fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        first_fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        first_fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            target_dir = data_dir / "downloads"
            env = {
                "MEDIAGENT_DATA_DIR": str(data_dir),
                "MEDIAGENT_DB_PATH": str(db_path),
                "PIXIV_CREDENTIALS_FILE": str(credential_path),
                "PIXIV_REFRESH_TOKEN": "secret-refresh",
            }
            first_context = ToolContext.from_env(env=env, cwd=Path(temp_dir), http_client=first_fake)
            first = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 1,
                        "include_ugoira_metadata": False,
                    },
                    first_context,
                )
            )

            second_fake = FakePixivHttpClient()
            second_fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
            second_context = ToolContext.from_env(env=env, cwd=Path(temp_dir), http_client=second_fake)
            second = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 1,
                        "include_ugoira_metadata": False,
                    },
                    second_context,
                )
            )

        self.assertTrue(first.is_success)
        self.assertTrue(second.is_success)
        self.assertEqual(second.data["summary"]["queued"], 0)
        self.assertEqual(second.data["summary"]["skipped"], 1)
        self.assertEqual(second.data["summary"]["files_downloaded"], 0)
        self.assertTrue(all("i.pximg.net" not in request[1] for request in second_fake.requests))

    def test_pixiv_bookmarks_sync_does_not_advance_cursor_when_limit_truncates_page(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_CREDENTIALS_FILE": str(credential_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 1,
                        "store_cursor": True,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="pixiv", cursor_name="bookmarks:public")

        self.assertTrue(result.is_success)
        self.assertIsNone(cursor)
        self.assertFalse(result.data["summary"]["cursor_stored"])
        self.assertEqual(result.data["summary"]["cursor_reason"], "limit_truncated")
        self.assertIn("cursor was not advanced", result.warnings[0])

    def test_pixiv_bookmarks_sync_stores_cursor_after_full_success(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two2"))
        fake.queue(HttpResponse(200, {"content-type": "image/jpeg", "content-length": "4"}, b"ugo1"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_CREDENTIALS_FILE": str(credential_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 3,
                        "store_cursor": True,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="pixiv", cursor_name="bookmarks:public")

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["summary"]["cursor_stored"])
        self.assertEqual(result.data["summary"]["cursor_value"], "555")
        self.assertEqual(cursor["cursor_value"], "555")
        self.assertEqual(cursor["metadata"]["items"], 3)

    def test_pixiv_bookmarks_sync_stores_scoped_cursor_after_media_type_filter(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two2"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            credential_path = data_dir / "credentials" / "pixiv-oauth.json"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_CREDENTIALS_FILE": str(credential_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "media_types": ["photo"],
                        "store_cursor": True,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )
            scoped_cursor = db.get_sync_cursor(db_path, platform="pixiv", cursor_name="bookmarks:public:photo")
            unscoped_cursor = db.get_sync_cursor(db_path, platform="pixiv", cursor_name="bookmarks:public")

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["summary"]["cursor_stored"])
        self.assertEqual(result.data["summary"]["cursor_reason"], "stored")
        self.assertEqual(result.data["summary"]["media_type_filtered"], 2)
        self.assertEqual(result.data["summary"]["downloaded"], 2)
        self.assertEqual(result.data["summary"]["files_downloaded"], 3)
        self.assertEqual(scoped_cursor["cursor_value"], "555")
        self.assertEqual(scoped_cursor["metadata"]["items"], 2)
        self.assertIsNone(unscoped_cursor)

    def test_pixiv_bookmarks_sync_dry_run_plans_without_writes(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 1,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )

            self.assertFalse(db_path.exists())
            self.assertFalse(target_dir.exists())

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(result.data["summary"]["planned_files"], 1)
        self.assertEqual(len(result.data["planned_downloads"]), 1)
        self.assertTrue(all("i.pximg.net" not in request[1] for request in fake.requests))

    def test_pixiv_bookmarks_sync_platform_root_does_not_duplicate_platform_layer(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            pixiv_dir = data_dir / "pixiv"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "MEDIAGENT_PIXIV_LIBRARY_DIR": str(pixiv_dir),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "limit": 1,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["target_dir"], str(pixiv_dir))
        self.assertEqual(
            result.data["planned_downloads"][0]["relative_path"],
            "photo/2026/01/20260101__pixiv__1001__p0.png",
        )
        self.assertFalse(result.data["planned_downloads"][0]["target_path"].startswith(str(pixiv_dir / "pixiv")))

    def test_pixiv_bookmarks_sync_can_collect_multiple_pages_and_filter_media_types(self) -> None:
        registry = create_default_registry()
        first_page = json.loads((FIXTURES / "pixiv" / "bookmarks_response.json").read_text(encoding="utf-8"))
        second_page = {
            "illusts": [
                {
                    **first_page["illusts"][0],
                    "id": 2001,
                    "title": "Second page image",
                    "create_date": "2026-01-04T00:00:00+09:00",
                    "meta_single_page": {
                        "original_image_url": "https://i.pximg.net/img-original/img/2026/01/04/00/00/00/2001_p0.png"
                    },
                    "meta_pages": [],
                }
            ],
            "next_url": None,
        }
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, json.dumps(first_page).encode()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, json.dumps(second_page).encode()))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "max_pages": 2,
                        "media_types": ["photo"],
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["pages_scanned"], 2)
        self.assertFalse(result.data["summary"]["max_pages_reached"])
        self.assertEqual(result.data["summary"]["collected"], 4)
        self.assertEqual(result.data["summary"]["media_type_filtered"], 3)
        self.assertEqual(result.data["summary"]["planned_files"], 4)
        self.assertEqual(result.data["summary"]["queued"], 3)

    def test_pixiv_bookmarks_sync_partial_failure_marks_item_partial(self) -> None:
        registry = create_default_registry()
        fake = FakePixivHttpClient()
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "token_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {}, (FIXTURES / "pixiv" / "bookmarks_response.json").read_bytes()))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"one1"))
        fake.queue(HttpResponse(200, {"content-type": "image/png", "content-length": "4"}, b"two1"))
        fake.queue(HttpResponse(500, {}, b"failed"))
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            target_dir = data_dir / "downloads"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(target_dir),
                        "limit": 2,
                        "include_ugoira_metadata": False,
                    },
                    context,
                )
            )
            statuses = _media_item_statuses(db_path)

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pixiv_bookmarks_sync_partial")
        self.assertEqual(result.data["summary"]["downloaded"], 1)
        self.assertEqual(result.data["summary"]["partial"], 1)
        self.assertEqual(result.data["summary"]["files_failed"], 1)
        self.assertEqual(statuses[("pixiv", "1001")], "downloaded")
        self.assertEqual(statuses[("pixiv", "1002")], "partial")

    def test_pixiv_bookmarks_sync_rejects_target_dir_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside_dir = Path(temp_dir) / "outside"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_DB_PATH": str(data_dir / "mediagent.sqlite3"),
                    "PIXIV_REFRESH_TOKEN": "secret-refresh",
                },
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "pixiv.bookmarks.sync",
                    {
                        "target_dir": str(outside_dir),
                        "limit": 1,
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_path")
        self.assertEqual(result.error.category.value, "filesystem")

    def test_pixiv_parser_normalizes_bookmarks_fixture(self) -> None:
        payload = json.loads((FIXTURES / "pixiv" / "bookmarks_response.json").read_text(encoding="utf-8"))
        ugoira = json.loads((FIXTURES / "pixiv" / "ugoira_metadata_response.json").read_text(encoding="utf-8"))

        items, next_cursor = pixiv_parser.parse_bookmarks(payload, ugoira_metadata_by_id={"1003": ugoira})

        self.assertEqual(next_cursor, "555")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["platform"], "pixiv")
        self.assertEqual(items[0]["remote_id"], "1001")
        self.assertEqual(items[0]["source_url"], "https://www.pixiv.net/artworks/1001")
        self.assertEqual(items[1]["metadata"]["files"][0]["url"].rsplit("/", 1)[-1], "1002_p0.png")
        self.assertEqual(items[2]["metadata"]["ugoira_metadata"]["ugoira_metadata"]["frames"][0]["delay"], 80)


def _media_item_statuses(db_path: Path) -> dict[tuple[str, str], str]:
    with db.connect(db_path) as connection:
        rows = connection.execute("SELECT platform, remote_id, status FROM media_items").fetchall()
    return {(row["platform"], row["remote_id"]): row["status"] for row in rows}


def _media_files(db_path: Path) -> list[dict[str, Any]]:
    with db.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT remote_url, local_path, mime_type, size_bytes, checksum, status,
                   library_relative_path, storage_layout, file_health
            FROM media_files
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _pixiv_access_context(
    temp_dir: str,
    fake: FakePixivHttpClient,
    *,
    extra: dict[str, str] | None = None,
) -> ToolContext:
    data_dir = Path(temp_dir) / "data"
    env = {
        "MEDIAGENT_DATA_DIR": str(data_dir),
        "MEDIAGENT_DB_PATH": str(data_dir / "mediagent.sqlite3"),
        "PIXIV_ACCESS_TOKEN": "secret-access",
        "PIXIV_USER_ID": "42",
        "PIXIV_TOKEN_EXPIRES_AT": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    env.update(extra or {})
    return ToolContext.from_env(env=env, cwd=Path(temp_dir), http_client=fake)


def _pixiv_illust(illust_id: str) -> dict[str, Any]:
    payload = json.loads((FIXTURES / "pixiv" / "bookmarks_response.json").read_text(encoding="utf-8"))
    for illust in payload["illusts"]:
        if str(illust["id"]) == str(illust_id):
            cloned = json.loads(json.dumps(illust))
            _replace_pixiv_urls(cloned)
            return cloned
    raise AssertionError(f"Missing Pixiv fixture illust: {illust_id}")


def _replace_pixiv_urls(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str) and item.startswith("https://i.pximg.net/"):
                value[key] = _test_media_url(item)
            else:
                _replace_pixiv_urls(item)
    elif isinstance(value, list):
        for item in value:
            _replace_pixiv_urls(item)


def _test_media_url(url: str) -> str:
    filename = Path(url.split("?", 1)[0]).name
    return f"https://{PUBLIC_TEST_IP}/{filename}"
