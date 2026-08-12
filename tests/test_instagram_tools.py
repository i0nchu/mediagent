import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediagent.core.http import HttpResponse
from mediagent.core import db
from mediagent.core.links import ResolveRequest, default_link_resolver_registry, resolution_to_media_item
from mediagent.core.tooling import ToolContext
from mediagent.platforms.instagram import client as instagram_client
from mediagent.tools.defaults import create_default_registry


PUBLIC_TEST_IP = "1.1.1.1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeInstagramClient:
    def __init__(self) -> None:
        self.status_payload: dict[str, Any] = {"status": "usable", "account_id": "123"}
        self.login_payload: dict[str, Any] = {"status": "usable", "account_id": "123"}
        self.resolution_payloads: dict[str, dict[str, Any]] = {}
        self.gets: dict[str, HttpResponse] = {}
        self.saved_pages: dict[str | None, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    def instagram_auth_status(self, *, session_file: str | None, timeout: float = 30.0) -> dict[str, Any]:
        self.calls.append(("instagram_auth_status", session_file))
        return self.status_payload

    def instagram_login(
        self,
        *,
        username: str,
        password: str,
        session_file: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append(("instagram_login", session_file))
        path = Path(session_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"sessionid": "secret-session"}), encoding="utf-8")
        os.chmod(path, 0o600)
        return self.login_payload

    def instagram_resolve_media(
        self,
        *,
        url: str,
        shortcode: str,
        session_file: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append(("instagram_resolve_media", shortcode))
        return self.resolution_payloads[shortcode]

    def instagram_saved_page(self, *, session_file: str, cursor: str | None, amount: int, timeout: float) -> dict[str, Any]:
        self.calls.append(("instagram_saved_page", cursor))
        return self.saved_pages[cursor]

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


class InstagramToolTests(unittest.TestCase):
    def test_saved_tool_examples_are_direct_cli_inputs(self) -> None:
        registry = create_default_registry()
        examples = {
            "instagram.saved.collect": "instagram.saved.collect.json",
            "instagram.saved.sync": "instagram.saved.sync.json",
            "instagram.saved.sync.full": "instagram.saved.sync.full.json",
        }
        with TemporaryDirectory() as temp_dir:
            regular = _ready_saved_context(temp_dir, FakeInstagramClient())
            context = ToolContext.from_env(
                env=regular.env,
                cwd=Path(temp_dir),
                dry_run=True,
                http_client=regular.http_client,
            )
            for label, filename in examples.items():
                payload = json.loads((PROJECT_ROOT / "examples" / "tools" / filename).read_text(encoding="utf-8"))
                self.assertNotIn("tool", payload, label)
                self.assertNotIn("input", payload, label)
                tool_name = "instagram.saved.collect" if label == "instagram.saved.collect" else "instagram.saved.sync"
                result = asyncio.run(registry.run(tool_name, payload, context))
                self.assertTrue(result.is_success, label)

    def test_real_saved_page_adapter_does_not_truncate_before_opaque_cursor(self) -> None:
        class StubClient:
            def load_settings(self, path: Path) -> None:
                self.path = path

            def collection_medias_v1_chunk(self, collection: str, max_id: str = "") -> tuple[list[str], str]:
                return ["first", "second", "third"], "opaque-next"

        with TemporaryDirectory() as temp_dir:
            session = Path(temp_dir) / "instagram_session.json"
            session.write_text("{}", encoding="utf-8")
            with patch("instagrapi.Client", StubClient):
                items, cursor = instagram_client.get_saved_page(
                    env={"INSTAGRAM_SESSION_FILE": str(session)},
                    cwd=Path(temp_dir),
                    amount=1,
                )

        self.assertEqual(items, ["first", "second", "third"])
        self.assertEqual(cursor, "opaque-next")

    def test_saved_collect_rejects_outside_db_path_before_network(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [], "next_cursor": None}}
        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as outside_dir:
            context = _ready_saved_context(temp_dir, fake)
            outside_db = Path(outside_dir) / "outside.sqlite3"
            result = asyncio.run(
                registry.run(
                    "instagram.saved.collect",
                    {"db_path": str(outside_db), "store_cursor": True},
                    context,
                )
            )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "unsafe_db_path")
            self.assertEqual(fake.calls, [])
            self.assertFalse(outside_db.exists())

    def test_saved_collect_paginates_dedupes_and_redacts_runtime_urls(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        first = _saved_post("SavedA", resources=1); duplicate = _saved_post("SavedA", resources=1)
        carousel = _saved_post("SavedB", resources=3)
        fake.saved_pages = {None: {"items": [first], "next_cursor": "opaque-2"},
                            "opaque-2": {"items": [duplicate, carousel], "next_cursor": None}}
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake)
            result = asyncio.run(registry.run("instagram.saved.collect", {"full_sync": True}, context))
        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["pages_fetched"], 2)
        self.assertEqual(result.data["summary"]["items"], 2)
        self.assertEqual(result.data["summary"]["resources"], 4)
        self.assertNotIn(PUBLIC_TEST_IP, json.dumps(result.to_dict()))

    def test_saved_collect_limit_does_not_expose_cursor_past_unreturned_page_items(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        fake.saved_pages = {
            None: {
                "items": [
                    _saved_post("First", resources=1),
                    _saved_post("Second", resources=1),
                    _saved_post("Third", resources=1),
                ],
                "next_cursor": "unsafe-next-page",
            }
        }
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "instagram.saved.collect",
                    {"limit": 1, "store_cursor": True},
                    context,
                )
            )
            db_path = context.db_path
            assert db_path
            cursor = db.get_sync_cursor(db_path, platform="instagram", cursor_name="saved")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["stop_reason"], "limit_reached")
        self.assertIsNone(result.data["summary"]["next_cursor"])
        self.assertIsNone(cursor)

    def test_saved_collect_stores_cursor_after_complete_bounded_page(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        fake.saved_pages = {
            None: {
                "items": [_saved_post("CompletePage", resources=1)],
                "next_cursor": "safe-next-page",
            }
        }
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "instagram.saved.collect",
                    {"max_pages": 1, "store_cursor": True},
                    context,
                )
            )
            db_path = context.db_path
            assert db_path
            cursor = db.get_sync_cursor(db_path, platform="instagram", cursor_name="saved")

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["stop_reason"], "max_pages_reached")
        self.assertEqual(result.data["summary"]["next_cursor"], "safe-next-page")
        self.assertTrue(result.data["summary"]["cursor_stored"])
        self.assertEqual(cursor["cursor_value"], "safe-next-page")

    def test_saved_collect_dry_run_does_not_read_session_or_network(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        with TemporaryDirectory() as temp_dir:
            regular = _ready_saved_context(temp_dir, fake)
            context = ToolContext.from_env(env=regular.env, cwd=Path(temp_dir), dry_run=True, http_client=fake)
            result = asyncio.run(registry.run("instagram.saved.collect", {"limit": 2}, context))
        self.assertTrue(result.is_success); self.assertEqual(fake.calls, [])

    def test_saved_collect_preserves_auth_and_rate_limit_errors(self) -> None:
        registry = create_default_registry()
        for code, category in (("instagram_login_required", "auth"), ("instagram_rate_limited", "rate_limit")):
            fake = FakeInstagramClient(); fake.saved_pages = {None: {"status": "error", "error_code": code}}
            with TemporaryDirectory() as temp_dir:
                result = asyncio.run(registry.run("instagram.saved.collect", {}, _ready_saved_context(temp_dir, fake)))
            self.assertFalse(result.is_success); self.assertEqual(result.error.code, code)
            self.assertEqual(result.error.category.value, category)

    def test_saved_sync_downloads_carousel_then_dedupes(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [_saved_post("SavedC", resources=3)], "next_cursor": None}}
        for suffix, mime in (("0.jpg", "image/jpeg"), ("1.jpg", "image/jpeg"), ("2.mp4", "video/mp4")):
            url = f"https://{PUBLIC_TEST_IP}/{suffix}"; fake.gets[url] = HttpResponse(200, {"Content-Type": mime}, b"media", url)
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake)
            first = asyncio.run(registry.run("instagram.saved.sync", {}, context))
            second = asyncio.run(registry.run("instagram.saved.sync", {}, context))
        self.assertTrue(first.is_success); self.assertEqual(first.data["summary"]["files"], 3)
        self.assertTrue(second.is_success); self.assertEqual(second.data["summary"]["queued"], 0)
        self.assertEqual([call[0] for call in fake.calls].count("GET_LIMITED"), 3)

    def test_saved_sync_stops_on_known_but_full_sync_scans_past_it(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [_saved_post("Known", resources=1)], "next_cursor": "page-2"},
                            "page-2": {"items": [_saved_post("Older", resources=1)], "next_cursor": None}}
        url = f"https://{PUBLIC_TEST_IP}/0.jpg"; fake.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg"}, b"media", url)
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake); db_path = context.db_path; assert db_path
            first = asyncio.run(registry.run("instagram.saved.sync", {"max_pages": 1}, context))
            fake.calls.clear()
            recurring = asyncio.run(registry.run("instagram.saved.sync", {"stop_on_known": True, "max_pages": 5}, context))
            recurring_calls = list(fake.calls); fake.calls.clear()
            full = asyncio.run(registry.run("instagram.saved.sync", {"full_sync": True, "stop_on_known": True}, context))
        self.assertTrue(first.is_success); self.assertEqual(recurring.data["summary"]["known"], 1)
        self.assertEqual([call[0] for call in recurring_calls].count("instagram_saved_page"), 1)
        self.assertTrue(full.is_success); self.assertEqual(full.data["summary"]["pages_fetched"], 2)

    def test_saved_sync_retries_failed_item_and_repairs_missing_file(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [_saved_post("RetryMe", resources=1)], "next_cursor": None}}
        url = f"https://{PUBLIC_TEST_IP}/0.jpg"
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake); db_path = context.db_path; assert db_path
            failed = asyncio.run(registry.run("instagram.saved.sync", {}, context))
            fake.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg"}, b"media", url)
            retried = asyncio.run(registry.run("instagram.saved.sync", {"retry_failed": True}, context))
            record = db.list_media_files(db_path, platform="instagram", remote_id="RetryMe")[0]
            Path(record["local_path"]).unlink()
            repaired = asyncio.run(registry.run("instagram.saved.sync", {"repair_missing_files": True}, context))
        self.assertFalse(failed.is_success); self.assertTrue(retried.is_success)
        self.assertTrue(repaired.is_success); self.assertEqual(repaired.data["summary"]["repaired"], 1)

    def test_saved_sync_does_not_advance_cursor_at_truncated_boundary(self) -> None:
        registry = create_default_registry(); fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [_saved_post("CursorA", resources=1)], "next_cursor": "unsafe-next"}}
        url = f"https://{PUBLIC_TEST_IP}/0.jpg"; fake.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg"}, b"media", url)
        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake); db_path = context.db_path; assert db_path
            result = asyncio.run(registry.run("instagram.saved.sync", {"max_pages": 1, "store_cursor": True}, context))
            cursor = db.get_sync_cursor(db_path, platform="instagram", cursor_name="saved")
        self.assertTrue(result.is_success); self.assertFalse(result.data["summary"]["cursor_stored"])
        self.assertIsNone(cursor)

    def test_saved_sync_partial_carousel_failure_does_not_store_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        fake.saved_pages = {None: {"items": [_saved_post("Partial", resources=3)], "next_cursor": None}}
        for suffix in ("0.jpg", "1.jpg"):
            url = f"https://{PUBLIC_TEST_IP}/{suffix}"
            fake.gets[url] = HttpResponse(200, {"Content-Type": "image/jpeg"}, b"media", url)

        with TemporaryDirectory() as temp_dir:
            context = _ready_saved_context(temp_dir, fake)
            db_path = context.db_path
            assert db_path
            result = asyncio.run(
                registry.run(
                    "instagram.saved.sync",
                    {"full_sync": True, "store_cursor": True},
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="instagram", cursor_name="saved")

        self.assertFalse(result.is_success)
        self.assertEqual(result.data["summary"]["partial"], 1)
        self.assertFalse(result.data["summary"]["cursor_stored"])
        self.assertIsNone(cursor)

    def test_auth_status_reports_missing_session_with_agent_decidable_error(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context = _instagram_context(temp_dir)

            result = asyncio.run(registry.run("instagram.auth.status", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "instagram_session_missing")
        self.assertEqual(result.error.category.value, "auth")
        self.assertEqual(result.error.details["recommended_tool"], "instagram.auth.ensure_session")
        self.assertNotIn("secret", str(result.to_dict()).lower())

    def test_auth_status_rejects_outside_session_file_before_client_callback(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        with TemporaryDirectory() as temp_dir:
            outside_dir = Path(temp_dir) / "outside"
            outside_dir.mkdir()
            outside_session = outside_dir / "instagram_session.json"
            outside_session.write_text("{}", encoding="utf-8")
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={"INSTAGRAM_SESSION_FILE": str(outside_session)},
            )

            result = asyncio.run(registry.run("instagram.auth.status", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(fake.calls, [])

    def test_auth_login_writes_session_with_fake_client_without_leaking_password(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        with TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={
                    "INSTAGRAM_ACCOUNT": "test-user",
                    "INSTAGRAM_SECRET": "super-secret-password",
                    "INSTAGRAM_SESSION_FILE": str(session_file),
                },
            )

            result = asyncio.run(registry.run("instagram.auth.login", {}, context))
            mode = session_file.stat().st_mode & 0o777

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["credentials_written"])
        self.assertEqual(mode, 0o600)
        self.assertEqual(fake.calls[0][0], "instagram_login")
        self.assertNotIn("super-secret-password", str(result.to_dict()))
        self.assertNotIn("secret-session", str(result.to_dict()))

    def test_ensure_session_respects_login_cooldown(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        fake.status_payload = {
            "status": "invalid",
            "error_code": "instagram_login_required",
            "metadata": {"reason": "login_required"},
        }
        with TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{}", encoding="utf-8")
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={
                    "INSTAGRAM_ACCOUNT": "test-user",
                    "INSTAGRAM_SECRET": "super-secret-password",
                    "INSTAGRAM_SESSION_FILE": str(session_file),
                },
            )

            first = asyncio.run(
                registry.run(
                    "instagram.auth.ensure_session",
                    {"cooldown_seconds": 3600},
                    context,
                )
            )
            second = asyncio.run(
                registry.run(
                    "instagram.auth.ensure_session",
                    {"cooldown_seconds": 3600},
                    context,
                )
            )

        self.assertTrue(first.is_success)
        self.assertEqual(first.data["login_attempted"], True)
        self.assertFalse(second.is_success)
        self.assertEqual(second.error.code, "instagram_login_required")
        self.assertIn("next_attempt_at", second.error.details)
        self.assertEqual([call[0] for call in fake.calls].count("instagram_login"), 1)

    def test_instagram_link_resolver_keeps_img_index_as_metadata_and_resolves_full_post(self) -> None:
        fake = FakeInstagramClient()
        _add_instagram_carousel(fake, shortcode="DSpCqHBiUI1")
        with TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{}", encoding="utf-8")
            env = _instagram_env(temp_dir, {"INSTAGRAM_SESSION_FILE": str(session_file)})

            result = default_link_resolver_registry().resolve(
                "https://www.instagram.com/p/DSpCqHBiUI1/?img_index=4&igsh=tracking",
                request=ResolveRequest(http_client=fake, env=env, cwd=Path(temp_dir)),
            )
            item = resolution_to_media_item(result)

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["remote_id"], "DSpCqHBiUI1")
        self.assertEqual(result["media_count"], 3)
        self.assertEqual(result["details"]["instagram"]["requested_img_index"], 4)
        self.assertEqual([candidate["media_type"] for candidate in result["media_candidates"]], ["photo", "photo", "video"])
        self.assertEqual([file["part"] for file in item["metadata"]["files"]], ["p0", "p1", "v0"])
        self.assertIn("_runtime", item)

    def test_instagram_link_resolve_tool_sanitizes_runtime_download_context(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        _add_instagram_carousel(fake, shortcode="DbUrx_Qk3Qn")
        with TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{}", encoding="utf-8")
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={"INSTAGRAM_SESSION_FILE": str(session_file)},
            )

            result = asyncio.run(
                registry.run(
                    "instagram.link.resolve",
                    {"url": "https://www.instagram.com/p/DbUrx_Qk3Qn/"},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn(PUBLIC_TEST_IP, serialized)
        self.assertIn("download_context", serialized)
        self.assertIn("mediagent_resource=0", serialized)

    def test_instagram_link_resolve_rejects_outside_session_file_before_client_callback(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        _add_instagram_carousel(fake, shortcode="DbUrx_Qk3Qn")
        with TemporaryDirectory() as temp_dir:
            outside_dir = Path(temp_dir) / "outside"
            outside_dir.mkdir()
            outside_session = outside_dir / "instagram_session.json"
            outside_session.write_text("{}", encoding="utf-8")
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={"INSTAGRAM_SESSION_FILE": str(outside_session)},
            )

            result = asyncio.run(
                registry.run(
                    "instagram.link.resolve",
                    {"url": "https://www.instagram.com/p/DbUrx_Qk3Qn/"},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")
        self.assertEqual(fake.calls, [])

    def test_instagram_link_resolve_rejects_non_instagram_direct_media(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        with TemporaryDirectory() as temp_dir:
            session_file = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{}", encoding="utf-8")
            context = _instagram_context(
                temp_dir,
                http_client=fake,
                extra_env={"INSTAGRAM_SESSION_FILE": str(session_file)},
            )

            result = asyncio.run(
                registry.run(
                    "instagram.link.resolve",
                    {"url": f"https://{PUBLIC_TEST_IP}/photo.jpg"},
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "instagram_media_unsupported")
        self.assertEqual(result.error.details["reason"], "unsupported_instagram_url")
        self.assertEqual(fake.calls, [])

    def test_link_media_sync_downloads_instagram_post_without_persisting_runtime_urls(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        _add_instagram_carousel(fake, shortcode="DbUrx_Qk3Qn")
        fake.gets[f"https://{PUBLIC_TEST_IP}/ig-a.jpg"] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "7"},
            b"jpg-one",
            f"https://{PUBLIC_TEST_IP}/ig-a.jpg",
        )
        fake.gets[f"https://{PUBLIC_TEST_IP}/ig-b.jpg"] = HttpResponse(
            200,
            {"Content-Type": "image/jpeg", "Content-Length": "7"},
            b"jpg-two",
            f"https://{PUBLIC_TEST_IP}/ig-b.jpg",
        )
        fake.gets[f"https://{PUBLIC_TEST_IP}/ig-c.mp4"] = HttpResponse(
            200,
            {"Content-Type": "video/mp4", "Content-Length": "9"},
            b"mp4-three",
            f"https://{PUBLIC_TEST_IP}/ig-c.mp4",
        )
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            session_file = data_dir / "credentials" / "instagram_session.json"
            session_file.parent.mkdir(parents=True)
            session_file.write_text("{}", encoding="utf-8")
            context = ToolContext.from_env(
                env=_instagram_env(
                    temp_dir,
                    {
                        "MEDIAGENT_DATA_DIR": str(data_dir),
                        "MEDIAGENT_DB_PATH": str(db_path),
                        "INSTAGRAM_SESSION_FILE": str(session_file),
                    },
                ),
                cwd=Path(temp_dir),
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {
                        "db_path": str(db_path),
                        "url": "https://www.instagram.com/p/DbUrx_Qk3Qn/",
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
                        "url": "https://www.instagram.com/p/DbUrx_Qk3Qn/",
                    },
                    context,
                )
            )
            db_text = db_path.read_text(encoding="latin-1")
            with sqlite3.connect(db_path) as connection:
                connection.row_factory = sqlite3.Row
                item_row = connection.execute("SELECT metadata_json FROM media_items").fetchone()
                file_rows = connection.execute(
                    "SELECT remote_url, local_path, status FROM media_files ORDER BY remote_url"
                ).fetchall()
            file_existence = [(Path(row["local_path"]), Path(row["local_path"]).exists()) for row in file_rows]

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["files_downloaded"], 3)
        self.assertTrue(second_result.is_success)
        self.assertEqual(second_result.data["summary"]["files_downloaded"], 0)
        self.assertEqual([call[0] for call in fake.calls].count("GET_LIMITED"), 3)
        self.assertEqual(len(file_rows), 3)
        self.assertTrue(all(row["status"] == "downloaded" for row in file_rows))
        self.assertTrue(all("instagram.com/p/DbUrx_Qk3Qn/" in row["remote_url"] for row in file_rows))
        self.assertNotIn(PUBLIC_TEST_IP, db_text)
        self.assertNotIn("download_context", item_row["metadata_json"])
        for path, exists in file_existence:
            self.assertTrue(exists)
            self.assertIn("/library/instagram/", str(path))

    def test_link_media_sync_rejects_outside_instagram_session_file_before_client_callback(self) -> None:
        registry = create_default_registry()
        fake = FakeInstagramClient()
        _add_instagram_carousel(fake, shortcode="DbUrx_Qk3Qn")
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            db_path = data_dir / "mediagent.sqlite3"
            outside_dir = Path(temp_dir) / "outside"
            outside_dir.mkdir()
            outside_session = outside_dir / "instagram_session.json"
            outside_session.write_text("{}", encoding="utf-8")
            context = ToolContext.from_env(
                env=_instagram_env(
                    temp_dir,
                    {
                        "MEDIAGENT_DATA_DIR": str(data_dir),
                        "MEDIAGENT_DB_PATH": str(db_path),
                        "INSTAGRAM_SESSION_FILE": str(outside_session),
                    },
                ),
                cwd=Path(temp_dir),
                dry_run=True,
                http_client=fake,
            )

            result = asyncio.run(
                registry.run(
                    "link.media.sync",
                    {
                        "db_path": str(db_path),
                        "url": "https://www.instagram.com/p/DbUrx_Qk3Qn/",
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["skipped_links"], 1)
        resolution = result.data["links"][0]["resolution"]
        self.assertEqual(resolution["skip_reason"], "unsafe_credential_path")
        self.assertEqual(resolution["details"]["error_code"], "unsafe_credential_path")
        self.assertEqual(fake.calls, [])


def _instagram_env(temp_dir: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    data_dir = Path(temp_dir) / "data"
    env = {
        "MEDIAGENT_DATA_DIR": str(data_dir),
        "MEDIAGENT_DB_PATH": str(data_dir / "mediagent.sqlite3"),
        "INSTAGRAM_SESSION_FILE": str(data_dir / "credentials" / "instagram_session.json"),
    }
    env.update(extra or {})
    return env


def _instagram_context(
    temp_dir: str,
    *,
    http_client: Any | None = None,
    extra_env: dict[str, str] | None = None,
) -> ToolContext:
    return ToolContext.from_env(
        env=_instagram_env(temp_dir, extra_env),
        cwd=Path(temp_dir),
        http_client=http_client,
    )


def _add_instagram_carousel(fake: FakeInstagramClient, *, shortcode: str) -> None:
    fake.resolution_payloads[shortcode] = {
        "status": "resolved",
        "media_type": "photo",
        "source_timestamp": "2026-07-28T00:00:00+00:00",
        "metadata": {
            "author_id": "author-123",
            "author": "creator",
            "caption_text": "public caption",
        },
        "resources": [
            {
                "index": 0,
                "media_type": "photo",
                "mime_type": "image/jpeg",
                "extension": ".jpg",
                "download_url": f"https://{PUBLIC_TEST_IP}/ig-a.jpg",
                "resource_id": "a",
            },
            {
                "index": 1,
                "media_type": "photo",
                "mime_type": "image/jpeg",
                "extension": ".jpg",
                "download_url": f"https://{PUBLIC_TEST_IP}/ig-b.jpg",
                "resource_id": "b",
            },
            {
                "index": 2,
                "media_type": "video",
                "mime_type": "video/mp4",
                "extension": ".mp4",
                "download_url": f"https://{PUBLIC_TEST_IP}/ig-c.mp4",
                "resource_id": "c",
            },
        ],
    }


def _saved_post(shortcode: str, *, resources: int) -> dict[str, Any]:
    values = []
    for index in range(resources):
        video = index == 2
        values.append({"index": index, "media_type": "video" if video else "photo",
                       "mime_type": "video/mp4" if video else "image/jpeg",
                       "extension": ".mp4" if video else ".jpg",
                       "download_url": f"https://{PUBLIC_TEST_IP}/{index}.{'mp4' if video else 'jpg'}",
                       "resource_id": f"resource-{index}"})
    return {"status": "resolved", "shortcode": shortcode, "source_timestamp": "2026-07-28T00:00:00+00:00",
            "metadata": {"author": "fixture-author", "caption_text": "fixture caption"}, "resources": values}


def _ready_saved_context(temp_dir: str, fake: FakeInstagramClient) -> ToolContext:
    session = Path(temp_dir) / "data" / "credentials" / "instagram_session.json"
    session.parent.mkdir(parents=True); session.write_text("{}", encoding="utf-8")
    return _instagram_context(temp_dir, http_client=fake, extra_env={"INSTAGRAM_SESSION_FILE": str(session)})
