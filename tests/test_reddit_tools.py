import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.platforms.reddit import parser as reddit_parser
from mediagent.tools.defaults import create_default_registry


class FakeRedditHttpClient:
    def __init__(self) -> None:
        self.responses: list[HttpResponse] = []
        self.requests: list[tuple[str, str, dict, dict]] = []

    def queue(self, response: HttpResponse) -> None:
        self.responses.append(response)

    def get_json(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("GET", url, {}, headers or {}))
        return self.responses.pop(0)

    def post_form(self, url: str, data: dict[str, str], *, headers=None, timeout: float = 30.0) -> HttpResponse:
        self.requests.append(("POST", url, data, headers or {}))
        return self.responses.pop(0)


class RedditToolTests(unittest.TestCase):
    def test_reddit_auth_start_builds_authorization_url(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(
            env={
                "REDDIT_CLIENT_ID": "client-id",
                "REDDIT_REDIRECT_URI": "http://127.0.0.1:8765/reddit/callback",
            },
            dry_run=True,
        )

        result = asyncio.run(
            registry.run(
                "reddit.auth.start",
                {"state": "fixed-state"},
                context,
            )
        )

        self.assertTrue(result.is_success)
        parsed = urlparse(result.data["authorization_url"])
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "www.reddit.com")
        self.assertEqual(params["client_id"], ["client-id"])
        self.assertEqual(params["state"], ["fixed-state"])
        self.assertEqual(params["duration"], ["permanent"])
        self.assertIn("identity history", params["scope"])

    def test_reddit_auth_start_requires_client_config(self) -> None:
        registry = create_default_registry()
        context = ToolContext.from_env(env={}, dry_run=True)

        result = asyncio.run(registry.run("reddit.auth.start", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "reddit_auth_missing_config")
        self.assertEqual(result.error.category.value, "validation")

    def test_reddit_auth_exchange_writes_credential_file_and_redacts_result(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        fake.queue(
            HttpResponse(
                200,
                {"x-ratelimit-remaining": "88.5"},
                json.dumps(
                    {
                        "access_token": "secret-access",
                        "refresh_token": "secret-refresh",
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "scope": "identity history",
                    }
                ).encode(),
            )
        )
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "reddit-oauth.json"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "REDDIT_CLIENT_ID": "client-id",
                    "REDDIT_CLIENT_SECRET": "client-secret",
                    "REDDIT_REDIRECT_URI": "http://127.0.0.1:8765/reddit/callback",
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                    "REDDIT_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(registry.run("reddit.auth.exchange", {"code": "one-time-code"}, context))
            written = json.loads(credential_path.read_text(encoding="utf-8"))
            mode = credential_path.stat().st_mode & 0o777

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][0], "POST")
        self.assertEqual(fake.requests[0][2]["grant_type"], "authorization_code")
        self.assertEqual(fake.requests[0][2]["code"], "one-time-code")
        self.assertIn("Authorization", fake.requests[0][3])
        self.assertEqual(fake.requests[0][3]["User-Agent"], "linux:mediagent:test (by /u/test_user)")
        self.assertEqual(written["access_token"], "secret-access")
        self.assertEqual(written["refresh_token"], "secret-refresh")
        self.assertEqual(written["scope"], "identity history")
        self.assertEqual(mode, 0o600)
        self.assertEqual(result.rate_limit["remaining"], 88.5)
        self.assertNotIn("secret-access", str(result.to_dict()))
        self.assertNotIn("secret-refresh", str(result.to_dict()))
        self.assertNotIn("one-time-code", str(result.to_dict()))

    def test_reddit_auth_exchange_rejects_credential_file_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside_path = Path(temp_dir) / "outside" / "reddit-oauth.json"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "REDDIT_CLIENT_ID": "client-id",
                    "REDDIT_REDIRECT_URI": "http://127.0.0.1:8765/reddit/callback",
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                },
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(
                registry.run(
                    "reddit.auth.exchange",
                    {
                        "code": "one-time-code",
                        "credential_output_path": str(outside_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(result.error.category.value, "filesystem")

    def test_reddit_auth_exchange_redacts_failed_authorization_code_payload(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        fake.queue(
            HttpResponse(
                400,
                {},
                json.dumps(
                    {
                        "error": "invalid_grant",
                        "message": "bad callback code SECRET_AUTH_CODE",
                        "code": "SECRET_AUTH_CODE",
                        "nested": {"authorization_code": "SECRET_AUTH_CODE"},
                    }
                ).encode(),
            )
        )
        context = ToolContext.from_env(
            env={
                "REDDIT_CLIENT_ID": "client-id",
                "REDDIT_REDIRECT_URI": "http://127.0.0.1:8765/reddit/callback",
                "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
            },
            http_client=fake,
        )

        result = asyncio.run(registry.run("reddit.auth.exchange", {"code": "SECRET_AUTH_CODE"}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "reddit_auth_exchange_failed")
        self.assertEqual(result.data["response"]["code"], "<redacted>")
        self.assertEqual(result.data["response"]["nested"]["authorization_code"], "<redacted>")
        self.assertNotIn("SECRET_AUTH_CODE", str(result.to_dict()))

    def test_reddit_auth_exchange_rejects_generic_user_agent(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        context = ToolContext.from_env(
            env={
                "REDDIT_CLIENT_ID": "client-id",
                "REDDIT_REDIRECT_URI": "http://127.0.0.1:8765/reddit/callback",
                "REDDIT_USER_AGENT": "mediagent/0.1.0",
            },
            http_client=fake,
        )

        result = asyncio.run(registry.run("reddit.auth.exchange", {"code": "one-time-code"}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "reddit_invalid_user_agent")
        self.assertEqual(result.error.category.value, "validation")
        self.assertEqual(fake.requests, [])

    def test_reddit_auth_refresh_preserves_existing_refresh_token(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        fake.queue(
            HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "access_token": "new-secret-access",
                        "token_type": "bearer",
                        "expires_in": 3600,
                        "scope": "identity history",
                    }
                ).encode(),
            )
        )
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "reddit-oauth.json"
            credential_path.parent.mkdir(parents=True)
            credential_path.write_text(
                json.dumps({"refresh_token": "existing-secret-refresh"}),
                encoding="utf-8",
            )
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "REDDIT_CLIENT_ID": "client-id",
                    "REDDIT_CLIENT_SECRET": "client-secret",
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                    "REDDIT_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(registry.run("reddit.auth.refresh", {}, context))
            written = json.loads(credential_path.read_text(encoding="utf-8"))

        self.assertTrue(result.is_success)
        self.assertEqual(fake.requests[0][2]["grant_type"], "refresh_token")
        self.assertEqual(fake.requests[0][2]["refresh_token"], "existing-secret-refresh")
        self.assertEqual(written["access_token"], "new-secret-access")
        self.assertEqual(written["refresh_token"], "existing-secret-refresh")
        self.assertNotIn("new-secret-access", str(result.to_dict()))
        self.assertNotIn("existing-secret-refresh", str(result.to_dict()))

    def test_reddit_auth_status_reports_usable_session_without_leaking_token(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"id":"abc123","name":"media_user"}'))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "reddit-oauth.json"
            credential_path.parent.mkdir(parents=True)
            credential_path.write_text(
                json.dumps(
                    {
                        "access_token": "secret-access",
                        "refresh_token": "secret-refresh",
                        "scope": "identity history",
                    }
                ),
                encoding="utf-8",
            )
            context = ToolContext.from_env(
                env={
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                    "REDDIT_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(registry.run("reddit.auth.status", {"required_scopes": ["history"]}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["session"]["account_id"], "media_user")
        self.assertTrue(result.data["session"]["refresh_available"])
        self.assertNotIn("secret-access", str(result.to_dict()))
        self.assertNotIn("secret-refresh", str(result.to_dict()))

    def test_reddit_saved_collect_normalizes_media_and_stores_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        fake.queue(HttpResponse(200, {}, b'{"id":"abc123","name":"media_user"}'))
        fake.queue(HttpResponse(200, {"x-ratelimit-remaining": "87"}, json.dumps(_saved_payload()).encode()))
        with TemporaryDirectory() as temp_dir:
            credential_path = Path(temp_dir) / "credentials" / "reddit-oauth.json"
            credential_path.parent.mkdir(parents=True)
            credential_path.write_text(
                json.dumps(
                    {
                        "access_token": "secret-access",
                        "refresh_token": "secret-refresh",
                        "scope": "identity history",
                    }
                ),
                encoding="utf-8",
            )
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": temp_dir,
                    "MEDIAGENT_DB_PATH": str(db_path),
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                    "REDDIT_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "reddit.saved.collect",
                    {
                        "username": "me",
                        "limit": 25,
                        "store_cursor": True,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="reddit", cursor_name="saved:media_user")
            media_files = db.list_media_files(db_path)

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["username"], "media_user")
        self.assertEqual(result.data["summary"]["entries"], 6)
        self.assertEqual(result.data["summary"]["items"], 4)
        self.assertEqual(result.data["summary"]["skipped_comments"], 1)
        self.assertEqual(result.data["summary"]["unsupported_media"], 1)
        self.assertTrue(result.data["summary"]["cursor_stored"])
        self.assertEqual(result.data["summary"]["next_after"], "t3_after_cursor")
        self.assertEqual(cursor["cursor_value"], "t3_after_cursor")
        self.assertEqual(media_files, [])
        self.assertEqual(fake.requests[1][0], "GET")
        self.assertIn("limit=25", fake.requests[1][1])
        self.assertIn("raw_json=1", fake.requests[1][1])
        self.assertEqual(len(result.data["items"]), 4)
        self.assertEqual(result.data["items"][0]["remote_id"], "t3_image")
        self.assertEqual(result.data["items"][1]["metadata"]["files"][1]["id"], "gallery_b")
        self.assertEqual(result.data["items"][2]["media_type"], "video")
        self.assertEqual(result.data["items"][3]["metadata"]["reddit"]["source_kind"], "comment")
        self.assertNotIn("secret-access", str(result.to_dict()))

    def test_reddit_saved_collect_dry_run_does_not_create_database(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mediagent.sqlite3"
            context = ToolContext.from_env(
                env={"MEDIAGENT_DB_PATH": str(db_path)},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            result = asyncio.run(registry.run("reddit.saved.collect", {"db_path": str(db_path)}, context))

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["would_collect"])
        self.assertFalse(db_path.exists())

    def test_reddit_saved_collect_rejects_db_path_outside_write_roots(self) -> None:
        registry = create_default_registry()
        fake = FakeRedditHttpClient()
        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as outside_dir:
            data_dir = Path(temp_dir) / "data"
            outside_db_path = Path(outside_dir) / "reddit.sqlite3"
            credential_path = data_dir / "credentials" / "reddit-oauth.json"
            credential_path.parent.mkdir(parents=True)
            credential_path.write_text(
                json.dumps({"access_token": "secret-access", "scope": "identity history"}),
                encoding="utf-8",
            )
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "REDDIT_USER_AGENT": "linux:mediagent:test (by /u/test_user)",
                    "REDDIT_CREDENTIALS_FILE": str(credential_path),
                },
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "reddit.saved.collect",
                    {
                        "username": "media_user",
                        "db_path": str(outside_db_path),
                        "store_cursor": True,
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_db_path")
        self.assertEqual(result.error.category.value, "filesystem")
        self.assertEqual(fake.requests, [])
        self.assertFalse(outside_db_path.exists())

    def test_reddit_parser_filters_by_media_type(self) -> None:
        items, summary = reddit_parser.parse_saved_listing(_saved_payload(), media_types=["video"])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["remote_id"], "t3_video")
        self.assertEqual(summary["skipped_media_type"], 3)


def _saved_payload() -> dict:
    return {
        "kind": "Listing",
        "data": {
            "after": "t3_after_cursor",
            "before": None,
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_image",
                        "id": "image",
                        "title": "single image",
                        "subreddit": "Media",
                        "subreddit_id": "t5_media",
                        "author": "artist",
                        "author_fullname": "t2_artist",
                        "created_utc": 1784707200,
                        "permalink": "/r/Media/comments/image/single_image/",
                        "over_18": False,
                        "url_overridden_by_dest": "https://i.redd.it/image.jpg",
                    },
                },
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_gallery",
                        "id": "gallery",
                        "title": "gallery",
                        "subreddit": "Media",
                        "author": "artist",
                        "created_utc": 1784707201,
                        "permalink": "/r/Media/comments/gallery/gallery/",
                        "over_18": False,
                        "is_gallery": True,
                        "gallery_data": {
                            "items": [
                                {"media_id": "gallery_a"},
                                {"media_id": "gallery_b"},
                            ]
                        },
                        "media_metadata": {
                            "gallery_a": {
                                "status": "valid",
                                "m": "image/jpg",
                                "s": {"u": "https://i.redd.it/gallery_a.jpg", "x": 800, "y": 600},
                            },
                            "gallery_b": {
                                "status": "valid",
                                "m": "image/png",
                                "s": {"u": "https://i.redd.it/gallery_b.png", "x": 640, "y": 480},
                            },
                        },
                    },
                },
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_video",
                        "id": "video",
                        "title": "hosted video",
                        "subreddit": "Media",
                        "author": "video_author",
                        "created_utc": 1784707202,
                        "permalink": "/r/Media/comments/video/hosted_video/",
                        "over_18": True,
                        "secure_media": {
                            "reddit_video": {
                                "fallback_url": "https://v.redd.it/video/DASH_720.mp4",
                                "width": 1280,
                                "height": 720,
                                "duration": 12,
                                "is_gif": False,
                            }
                        },
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "name": "t1_direct_comment",
                        "id": "direct_comment",
                        "body": "saved for later https://cdn.example.test/photo.webp",
                        "author": "commenter",
                        "author_fullname": "t2_commenter",
                        "subreddit": "Media",
                        "permalink": "/r/Media/comments/post/_/direct_comment/",
                        "created_utc": 1784707203,
                    },
                },
                {
                    "kind": "t1",
                    "data": {
                        "name": "t1_plain_comment",
                        "id": "plain_comment",
                        "body": "no direct media here",
                        "author": "commenter",
                    },
                },
                {
                    "kind": "t3",
                    "data": {
                        "name": "t3_embed",
                        "id": "embed",
                        "title": "unsupported embed",
                        "post_hint": "rich:video",
                        "url_overridden_by_dest": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    },
                },
            ],
        },
    }
