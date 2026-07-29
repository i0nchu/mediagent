import asyncio
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.core import db
from mediagent.platforms.x import parser as x_parser
from mediagent.tools.defaults import create_default_registry


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeXHttpClient:
    def __init__(self) -> None:
        self.responses: list[HttpResponse] = []
        self.requests: list[tuple[str, str, dict]] = []

    def queue(self, response: HttpResponse) -> None:
        self.responses.append(response)

    def get_json(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("GET", url, headers or {}))
        return self.responses.pop(0)

    def post_form(self, url: str, data: dict[str, str], *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("POST", url, data))
        return self.responses.pop(0)


class AuthXToolTests(unittest.TestCase):
    def test_x_auth_start_generates_pkce_url(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(env={}, dry_run=True)

        result = asyncio.run(
            registry.run(
                "x.auth.start",
                {
                    "client_id": "client",
                    "redirect_uri": "http://127.0.0.1/callback",
                    "state": "fixed-state",
                },
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertIn("code_challenge_method=S256", result.data["authorization_url"])
        self.assertEqual(result.data["state"], "fixed-state")
        self.assertIn("bookmark.read", result.data["scopes"])

    def test_x_auth_start_reads_client_config_from_env(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(
            env={
                "X_CLIENT_ID": "client-from-env",
                "X_REDIRECT_URI": "http://127.0.0.1:8765/callback",
            },
            dry_run=True,
        )

        result = asyncio.run(registry.run("x.auth.start", {"state": "fixed-state"}, context))

        self.assertTrue(result.is_success)
        self.assertIn("client_id=client-from-env", result.data["authorization_url"])

    def test_x_auth_start_requires_client_config(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(env={}, dry_run=True)

        result = asyncio.run(registry.run("x.auth.start", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "auth_missing_config")
        self.assertEqual(result.error.category.value, "validation")

    def test_x_auth_exchange_redacts_token_payload(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(
            HttpResponse(
                200,
                {"x-rate-limit-remaining": "14"},
                json.dumps(
                    {
                        "token_type": "bearer",
                        "expires_in": 7200,
                        "access_token": "secret-access",
                        "refresh_token": "secret-refresh",
                        "scope": "tweet.read users.read bookmark.read offline.access",
                    }
                ).encode(),
            )
        )
        context = ToolContext.from_env(
            env={
                "X_CLIENT_ID": "client",
                "X_REDIRECT_URI": "http://127.0.0.1/callback",
            },
            http_client=fake,
        )

        result = asyncio.run(
            registry.run(
                "x.auth.exchange",
                {
                    "code": "code",
                    "code_verifier": "verifier",
                },
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertNotIn("secret-access", str(result.to_dict()))
        self.assertNotIn("secret-refresh", str(result.to_dict()))
        self.assertEqual(result.rate_limit["remaining"], 14)

    def test_x_auth_exchange_writes_explicit_credential_file(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(
            HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "token_type": "bearer",
                        "expires_in": 7200,
                        "access_token": "secret-access",
                        "refresh_token": "secret-refresh",
                        "scope": "tweet.read users.read bookmark.read offline.access",
                    }
                ).encode(),
            )
        )
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "x-oauth.json"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": temp_dir},
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "x.auth.exchange",
                    {
                        "client_id": "client",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code": "code",
                        "code_verifier": "verifier",
                        "credential_output_path": str(credential_path),
                    },
                    context,
                )
            )

            written = json.loads(credential_path.read_text(encoding="utf-8"))
            mode = credential_path.stat().st_mode & 0o777

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["credentials_written"])
        self.assertEqual(written["access_token"], "secret-access")
        self.assertEqual(written["refresh_token"], "secret-refresh")
        self.assertEqual(mode, 0o600)
        self.assertNotIn("secret-access", str(result.to_dict()))
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_x_auth_exchange_rejects_credential_file_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside_path = Path(temp_dir) / "outside" / "x-oauth.json"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": str(data_dir)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "x.auth.exchange",
                    {
                        "client_id": "client",
                        "redirect_uri": "http://127.0.0.1/callback",
                        "code": "code",
                        "code_verifier": "verifier",
                        "credential_output_path": str(outside_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(result.error.category.value, "filesystem")

    def test_auth_session_status_routes_to_x(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"data":{"id":"42","username":"example"}}'))
        context = ToolContext.from_env(
            env={
                "X_ACCESS_TOKEN": "secret",
                "X_REFRESH_TOKEN": "refresh",
                "X_SCOPES": "tweet.read users.read bookmark.read",
            },
            http_client=fake,
        )

        result = asyncio.run(
            registry.run(
                "auth.session.status",
                {
                    "provider": "x",
                    "required_scopes": ["tweet.read", "users.read", "bookmark.read"],
                },
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["account_id"], "42")
        self.assertIs(result.data["session"]["refresh_available"], True)
        self.assertNotIn("secret", str(result.to_dict()))

    def test_auth_session_status_honors_x_credential_refs(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"data":{"id":"42","username":"example"}}'))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "x-oauth.json"
            credential_path.write_text(
                json.dumps(
                    {
                        "access_token": "secret-from-ref",
                        "refresh_token": "refresh-from-ref",
                        "scope": "tweet.read users.read bookmark.read",
                    }
                ),
                encoding="utf-8",
            )
            context = ToolContext.from_env(env={}, cwd=Path(temp_dir), http_client=fake)

            result = asyncio.run(
                registry.run(
                    "auth.session.status",
                    {
                        "provider": "x",
                        "credential_refs": [
                            {"source": "file", "name": str(credential_path), "key": "access_token"},
                            {"source": "file", "name": str(credential_path), "key": "refresh_token"},
                            {"source": "file", "name": str(credential_path), "key": "scope"},
                        ],
                        "required_scopes": ["tweet.read", "users.read", "bookmark.read"],
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][2]["Authorization"], "Bearer secret-from-ref")
        self.assertNotIn("secret-from-ref", str(result.to_dict()))

    def test_auth_session_status_dry_run_does_not_call_network(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        context = ToolContext.from_env(
            env={"X_ACCESS_TOKEN": "secret"},
            http_client=fake,
            dry_run=True,
        )

        result = asyncio.run(
            registry.run(
                "auth.session.status",
                {"provider": "x", "required_scopes": ["bookmark.read"]},
                context,
            )
        )

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["would_check"])
        self.assertEqual(fake.requests, [])

    def test_x_auth_status_reads_credential_file(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"data":{"id":"42","username":"example"}}'))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "x-oauth.json"
            credential_path.write_text(
                json.dumps(
                    {
                        "access_token": "secret-from-file",
                        "refresh_token": "refresh-from-file",
                        "scope": "tweet.read users.read bookmark.read",
                    }
                ),
                encoding="utf-8",
            )
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "X_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "x.auth.status",
                    {"required_scopes": ["tweet.read", "users.read", "bookmark.read"]},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["account_id"], "42")
        self.assertEqual(fake.requests[0][2]["Authorization"], "Bearer secret-from-file")
        self.assertNotIn("secret-from-file", str(result.to_dict()))

    def test_x_auth_status_reports_expired_token(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"data":{"id":"42","username":"example"}}'))
        context = ToolContext.from_env(
            env={
                "X_ACCESS_TOKEN": "secret",
                "X_SCOPES": "tweet.read users.read bookmark.read",
                "X_TOKEN_EXPIRES_AT": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
            http_client=fake,
        )

        result = asyncio.run(registry.run("x.auth.status", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "x_auth_expired")
        self.assertEqual(result.error.category.value, "auth")
        self.assertEqual(result.data["auth_status"]["status"], "expired")

    def test_x_auth_refresh_writes_back_to_configured_credential_file(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(
            HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "token_type": "bearer",
                        "expires_in": 7200,
                        "access_token": "new-access",
                        "scope": "tweet.read users.read bookmark.read offline.access",
                    }
                ).encode(),
            )
        )
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "x-oauth.json"
            credential_path.write_text(
                json.dumps(
                    {
                        "client_id": "client",
                        "access_token": "old-access",
                        "refresh_token": "keep-refresh",
                    }
                ),
                encoding="utf-8",
            )
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "X_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(registry.run("x.auth.refresh", {}, context))
            written = json.loads(credential_path.read_text(encoding="utf-8"))

        self.assertTrue(result.is_success)
        self.assertEqual(written["access_token"], "new-access")
        self.assertEqual(written["refresh_token"], "keep-refresh")
        self.assertNotIn("new-access", str(result.to_dict()))

    def test_x_bookmarks_collect_uses_fixture_and_stores_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeXHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"data":{"id":"42","username":"example"}}'))
        fake.queue(
            HttpResponse(
                200,
                {
                    "x-rate-limit-limit": "15",
                    "x-rate-limit-remaining": "12",
                    "x-rate-limit-reset": "1234567890",
                },
                (FIXTURES / "x" / "bookmarks_response.json").read_bytes(),
            )
        )
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "X_ACCESS_TOKEN": "secret",
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )
            result = asyncio.run(
                registry.run(
                    "x.bookmarks.collect",
                    {"db_path": str(db_path), "store_cursor": True},
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="x", cursor_name="bookmarks")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["next_token"], "next-page")
        self.assertEqual(cursor["cursor_value"], "next-page")
        self.assertEqual(result.rate_limit["remaining"], 12)
        self.assertEqual(result.data["items"][0]["media_type"], "photo")
        files = result.data["items"][0]["metadata"]["files"]
        self.assertIn("https://video.twimg.com/video-high.mp4", [file["url"] for file in files])

    def test_x_parser_normalizes_fixture(self) -> None:
        payload = json.loads((FIXTURES / "x" / "bookmarks_response.json").read_text())
        items = x_parser.parse_bookmarks(payload)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "x")
        self.assertEqual(items[0]["remote_id"], "111")
        self.assertEqual(items[0]["source_url"], "https://x.com/example/status/111")
