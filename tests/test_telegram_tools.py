import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mediagent.core import db
from mediagent.core.tooling import ToolContext
from mediagent.platforms.telegram import client as telegram_client
from mediagent.tools.defaults import create_default_registry


class FakeTelegramClient:
    def __init__(
        self,
        *,
        auth_payload: dict[str, Any] | None = None,
        dialogs: list[dict[str, Any]] | None = None,
        messages: dict[str, list[dict[str, Any]]] | None = None,
        downloads: dict[str, bytes | dict[str, Any] | Exception] | None = None,
    ) -> None:
        self.auth_payload = auth_payload or {
            "usable": True,
            "status": "usable",
            "account": {"id": 42, "username": "media_user", "display_name": "Media User"},
        }
        self.dialogs = dialogs or []
        self.messages = messages or {}
        self.downloads = downloads or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def telegram_auth_login_start(self, config: Any, *, phone_number: str) -> dict[str, Any]:
        self.calls.append(("auth_login_start", {"phone_number_present": bool(phone_number)}))
        return {
            "status": "code_sent",
            "usable": False,
            "phone_code_hash": "fake-phone-code-hash",
            "code_type": "sent_app",
        }

    def telegram_auth_login_complete(
        self,
        config: Any,
        *,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        password: str | None,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "auth_login_complete",
                {
                    "phone_number_present": bool(phone_number),
                    "code_present": bool(code),
                    "phone_code_hash": phone_code_hash,
                    "password_present": bool(password),
                },
            )
        )
        return self.auth_payload

    def telegram_auth_status(self, config: Any) -> dict[str, Any]:
        self.calls.append(("auth_status", config.safe_metadata()))
        return self.auth_payload

    def telegram_list_dialogs(
        self,
        config: Any,
        *,
        limit: int | None,
        chat_types: list[str] | None,
    ) -> dict[str, Any]:
        self.calls.append(("dialogs_list", {"limit": limit, "chat_types": chat_types or []}))
        allowed = set(chat_types or [])
        dialogs = [dialog for dialog in self.dialogs if not allowed or dialog["type"] in allowed]
        if limit:
            dialogs = dialogs[:limit]
        return {"dialogs": dialogs, "summary": {"dialogs": len(dialogs)}}

    def telegram_collect_messages(
        self,
        config: Any,
        *,
        chats: list[Any],
        after_by_source: dict[str, int | None],
        limit: int | None,
        message_ids_by_source: dict[str, list[int]] | None,
        message_links: list[str] | None,
        include_protected: bool,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "messages_collect",
                {
                    "chats": chats,
                    "after_by_source": after_by_source,
                    "limit": limit,
                    "message_links": message_links or [],
                    "include_protected": include_protected,
                },
            )
        )
        messages: list[dict[str, Any]] = []
        source_summaries: list[dict[str, Any]] = []
        for chat in chats:
            source_key = telegram_client.source_key_for_chat(chat)
            after = after_by_source.get(source_key)
            selected_ids = set((message_ids_by_source or {}).get(source_key) or [])
            selected = []
            for message in self.messages.get(source_key, []):
                message_id = int(message["id"])
                if selected_ids and message_id not in selected_ids:
                    continue
                if after is not None and message_id <= after:
                    continue
                selected.append(message)
            selected = sorted(selected, key=lambda item: int(item["id"]))
            if limit is not None:
                selected = selected[:limit]
            messages.extend(selected)
            source_summaries.append(
                {
                    "source_key": source_key,
                    "messages": len(selected),
                    "next_message_id": str(max(int(item["id"]) for item in selected)) if selected else None,
                }
            )
        for ref in telegram_client.parse_message_links(message_links or []):
            selected = [
                message
                for message in self.messages.get(ref["source_key"], [])
                if int(message["id"]) == int(ref["message_id"])
            ]
            messages.extend(selected)
            source_summaries.append(
                {
                    "source_key": ref["source_key"],
                    "messages": len(selected),
                    "next_message_id": str(max(int(item["id"]) for item in selected)) if selected else None,
                    "cursor_eligible": False,
                }
            )
        return {"messages": messages, "source_summaries": source_summaries}

    def telegram_download_media(
        self,
        config: Any,
        *,
        download_ref: dict[str, Any],
        target_path: str | None = None,
        partial_path: str | None = None,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        key = f"{download_ref['chat_id']}:{download_ref['message_id']}:{download_ref['media_id']}"
        self.calls.append(
            (
                "media_download",
                {
                    "key": key,
                    "target_path": target_path,
                    "partial_path": partial_path,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        payload = self.downloads[key]
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, dict) and "stream_content" in payload:
            if partial_path is None:
                raise AssertionError("partial_path is required for streamed fake downloads")
            Path(partial_path).parent.mkdir(parents=True, exist_ok=True)
            Path(partial_path).write_bytes(payload["stream_content"])
            if payload.get("cancel_after_stream"):
                raise asyncio.CancelledError()
            if payload.get("raise_after_stream"):
                raise telegram_client.TelegramClientError("stream failed")
            return {
                "path": partial_path,
                "mime_type": payload.get("mime_type"),
            }
        if isinstance(payload, bytes):
            return {"content": payload, "mime_type": None}
        return payload


class TelegramToolTests(unittest.TestCase):
    def test_auth_login_start_sends_code_without_exposing_phone(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient()
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(registry.run("telegram.auth.login", {"mode": "start"}, context))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["status"], "code_sent")
        self.assertEqual(result.data["phone_code_hash"], "fake-phone-code-hash")
        self.assertEqual(fake.calls[0][0], "auth_login_start")
        self.assertNotIn("+886912345678", str(result.to_dict()))
        self.assertNotIn("secret-api-hash", str(result.to_dict()))

    def test_auth_login_dry_run_without_config_is_preview_only(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(env={}, cwd=Path(temp_dir), dry_run=True)

            result = asyncio.run(registry.run("telegram.auth.login", {"mode": "start"}, context))

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["would_send_code"])
        self.assertFalse(result.data["phone_number_present"])
        self.assertFalse(result.data["config"]["api_hash_present"])

    def test_auth_login_complete_accepts_password_ref(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient()
        with TemporaryDirectory() as temp_dir:
            password_path = Path(temp_dir) / "password.txt"
            password_path.write_text("secret-2fa", encoding="utf-8")
            context, _data_dir, _db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.auth.login",
                    {
                        "mode": "complete",
                        "code": "12345",
                        "phone_code_hash": "fake-phone-code-hash",
                        "password_ref": {"source": "file", "name": str(password_path)},
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["usable"])
        self.assertEqual(fake.calls[0][0], "auth_login_complete")
        self.assertTrue(fake.calls[0][1]["password_present"])
        self.assertNotIn("12345", str(result.to_dict()))
        self.assertNotIn("secret-2fa", str(result.to_dict()))

    def test_auth_login_rejects_inline_password_without_leaking_value(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient()
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.auth.login",
                    {
                        "mode": "complete",
                        "code": "12345",
                        "phone_code_hash": "fake-phone-code-hash",
                        "password": "secret-inline-2fa",
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_auth_inline_password_not_supported")
        self.assertEqual(fake.calls, [])
        self.assertNotIn("secret-inline-2fa", str(result.to_dict()))

    def test_auth_login_complete_requires_code_and_hash(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient())

            result = asyncio.run(registry.run("telegram.auth.login", {"mode": "complete"}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_auth_login_missing_code")

    def test_auth_status_reports_usable_fake_session_without_secrets(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient())

            result = asyncio.run(registry.run("telegram.auth.status", {}, context))

        self.assertTrue(result.is_success)
        self.assertTrue(result.data["usable"])
        self.assertEqual(result.data["session"]["provider"], "telegram")
        self.assertEqual(result.data["session"]["account_id"], "42")
        self.assertNotIn("secret-api-hash", str(result.to_dict()))
        self.assertNotIn("+886912345678", str(result.to_dict()))

    def test_auth_status_rejects_missing_config(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            context = ToolContext.from_env(env={"MEDIAGENT_DATA_DIR": str(data_dir)}, cwd=Path(temp_dir))

            result = asyncio.run(registry.run("telegram.auth.status", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_auth_missing_config")
        self.assertIn("TELEGRAM_API_ID", result.data["missing"])

    def test_auth_status_rejects_session_path_outside_write_roots(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            outside = Path(temp_dir) / "outside" / "telegram.session"
            context = ToolContext.from_env(
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "TELEGRAM_API_ID": "12345",
                    "TELEGRAM_API_HASH": "secret-api-hash",
                    "TELEGRAM_SESSION_FILE": str(outside),
                },
                cwd=Path(temp_dir),
                http_client=FakeTelegramClient(),
            )

            result = asyncio.run(registry.run("telegram.auth.status", {}, context))

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_credential_path")

    def test_dialogs_list_filters_chat_types(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            dialogs=[
                {"id": "me", "title": "Saved Messages", "type": "saved_messages", "username": None},
                {"id": "10", "title": "Private", "type": "private", "username": "friend"},
                {"id": "20", "title": "Trusted Channel", "type": "channel", "username": "trusted"},
            ]
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.dialogs.list",
                    {"chat_types": ["channel"], "limit": 10},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(len(result.data["dialogs"]), 1)
        self.assertEqual(result.data["dialogs"][0]["title"], "Trusted Channel")
        self.assertNotIn("message text", str(result.to_dict()).lower())

    def test_messages_collect_normalizes_media_and_stores_scoped_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.collect",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "media_types": ["photo"],
                        "store_cursor": True,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:saved_messages:photo",
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["messages_scanned"], 5)
        self.assertEqual(result.data["summary"]["items"], 3)
        self.assertEqual(result.data["summary"]["skipped_protected"], 1)
        self.assertEqual(result.data["items"][1]["metadata"]["telegram"]["grouped_id"], "album-77")
        self.assertEqual(result.data["items"][0]["metadata"]["files"][0]["download_ref"]["message_id"], "10")
        self.assertEqual(cursor["cursor_value"], "14")

    def test_inbox_collect_links_full_sync_does_not_apply_default_message_limit(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={
                "saved_messages": [
                    {
                        "id": 10,
                        "date": "2026-07-21T10:00:00+00:00",
                        "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
                        "caption": "https://example.com/one.jpg",
                    },
                    {
                        "id": 11,
                        "date": "2026-07-21T10:05:00+00:00",
                        "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
                        "caption": "https://example.com/two.jpg",
                    },
                ]
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.collect_links",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "full_sync": True,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["messages_scanned"], 2)
        self.assertEqual(result.data["summary"]["links_found"], 2)
        self.assertEqual(fake.calls[-1][1]["limit"], None)

    def test_messages_collect_ignores_full_sync_outside_inbox_tools(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.collect",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "full_sync": True,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.calls[-1][1]["limit"], 100)

    def test_messages_collect_can_extract_media_from_curated_link_channel(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={
                "curated": [
                    {
                        "id": 50,
                        "date": "2026-07-22T01:00:00+00:00",
                        "chat": {"id": "curated", "title": "Mediagent Inbox", "type": "channel"},
                        "text": "save this https://t.me/source_channel/100",
                        "media": [],
                    }
                ],
                "link:source_channel": [
                    {
                        "id": 100,
                        "date": "2026-07-22T01:05:00+00:00",
                        "chat": {"id": "source_channel", "title": "Source", "type": "channel", "username": "source_channel"},
                        "media": [
                            {
                                "id": "photo-100",
                                "kind": "photo",
                                "mime_type": "image/jpeg",
                                "download_ref": {
                                    "chat_id": "source_channel",
                                    "chat_username": "source_channel",
                                    "message_id": "100",
                                    "media_id": "photo-100",
                                },
                            }
                        ],
                    }
                ],
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.collect",
                    {
                        "db_path": str(db_path),
                        "chat": "curated",
                        "extract_message_links": True,
                        "media_types": ["photo"],
                        "store_cursor": True,
                    },
                    context,
                )
            )
            inbox_cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="messages:curated:photo")
            link_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:link-source_channel:photo",
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["extracted_message_links"], 1)
        self.assertEqual(result.data["summary"]["linked_messages"], 1)
        self.assertEqual(len(result.data["items"]), 1)
        self.assertEqual(result.data["items"][0]["remote_id"], "source_channel:100:photo-100")
        self.assertEqual(inbox_cursor["cursor_value"], "50")
        self.assertIsNone(link_cursor)

    def test_telegram_message_links_include_private_channel_links(self) -> None:
        refs = telegram_client.parse_message_links(
            [
                "https://t.me/source_channel/100",
                "https://t.me/c/123456789/55?single",
                "https://example.com/not-telegram/1",
            ]
        )

        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["chat"], "source_channel")
        self.assertEqual(refs[0]["message_id"], 100)
        self.assertEqual(refs[1]["chat"], -100123456789)
        self.assertEqual(refs[1]["message_id"], 55)

    def test_telegram_download_entity_selector_converts_numeric_chat_id(self) -> None:
        self.assertEqual(telegram_client.download_entity_selector({"chat_id": "-100123456789"}), -100123456789)
        self.assertEqual(
            telegram_client.download_entity_selector({"chat_id": "-100123456789", "chat_username": "source"}),
            "source",
        )

    def test_telegram_entity_selector_converts_numeric_dialog_selector(self) -> None:
        self.assertEqual(telegram_client._entity_selector("3779502941"), 3779502941)
        self.assertEqual(telegram_client._entity_selector("-1003779502941"), -1003779502941)
        self.assertEqual(telegram_client._entity_selector("saved_messages"), "me")
        self.assertEqual(telegram_client._entity_selector("@source_channel"), "@source_channel")

    def test_media_download_writes_final_file_and_removes_partial(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            downloads={
                "saved_messages:10:photo-10": {
                    "content": b"image-bytes",
                    "mime_type": "image/jpeg",
                }
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, fake)
            target_path = data_dir / "library" / "telegram" / "photo.jpg"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {
                            "chat_id": "saved_messages",
                            "message_id": "10",
                            "media_id": "photo-10",
                        },
                        "target_path": str(target_path),
                        "expected_mime_prefix": "image/",
                    },
                    context,
                )
            )

            content = target_path.read_bytes()
            partial_exists = target_path.with_name(target_path.name + ".partial").exists()

        self.assertTrue(result.is_success)
        self.assertEqual(content, b"image-bytes")
        self.assertFalse(partial_exists)
        self.assertEqual(result.data["size_bytes"], 11)
        self.assertTrue(result.data["checksum"].startswith("sha256:"))

    def test_media_download_accepts_streamed_partial_file_without_buffering(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            downloads={
                "saved_messages:12:video-12": {
                    "stream_content": b"video-bytes",
                    "mime_type": "video/mp4",
                }
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, fake)
            target_path = data_dir / "library" / "telegram" / "video.mp4"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {
                            "chat_id": "saved_messages",
                            "message_id": "12",
                            "media_id": "video-12",
                        },
                        "target_path": str(target_path),
                        "expected_mime_prefix": "video/",
                    },
                    context,
                )
            )

            content = target_path.read_bytes()
            partial_exists = target_path.with_name(target_path.name + ".partial").exists()

        self.assertTrue(result.is_success)
        self.assertEqual(content, b"video-bytes")
        self.assertFalse(partial_exists)
        self.assertEqual(result.data["size_bytes"], 11)
        self.assertEqual(result.data["mime_type"], "video/mp4")
        self.assertTrue(fake.calls[0][1]["partial_path"].endswith("video.mp4.partial"))

    def test_media_download_removes_partial_when_stream_fails(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            downloads={
                "saved_messages:12:video-12": {
                    "stream_content": b"incomplete-video",
                    "mime_type": "video/mp4",
                    "raise_after_stream": True,
                }
            }
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, fake)
            target_path = data_dir / "library" / "telegram" / "video.mp4"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {
                            "chat_id": "saved_messages",
                            "message_id": "12",
                            "media_id": "video-12",
                        },
                        "target_path": str(target_path),
                        "expected_mime_prefix": "video/",
                    },
                    context,
                )
            )

            partial_exists = target_path.with_name(target_path.name + ".partial").exists()
            target_exists = target_path.exists()

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_download_failed")
        self.assertFalse(partial_exists)
        self.assertFalse(target_exists)

    def test_media_download_rejects_unsafe_path(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient())
            outside = Path(temp_dir) / "outside.jpg"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {
                            "chat_id": "saved_messages",
                            "message_id": "10",
                            "media_id": "photo-10",
                        },
                        "target_path": str(outside),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "unsafe_path")

    def test_media_download_rejects_media_without_download_ref_as_validation_error(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient())
            target_path = data_dir / "library" / "missing-ref.bin"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "media": {},
                        "target_path": str(target_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_download_missing_ref")
        self.assertEqual(result.error.category.value, "validation")

    def test_media_download_rejects_empty_direct_download_ref_in_dry_run(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient(), dry_run=True)
            target_path = data_dir / "library" / "empty-ref.bin"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {},
                        "target_path": str(target_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_download_missing_ref")
        self.assertEqual(result.error.category.value, "validation")

    def test_media_download_rejects_partial_direct_download_ref_in_dry_run(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context, data_dir, _db_path = _telegram_context(temp_dir, FakeTelegramClient(), dry_run=True)
            target_path = data_dir / "library" / "partial-ref.bin"

            result = asyncio.run(
                registry.run(
                    "telegram.media.download",
                    {
                        "download_ref": {"chat_id": "saved_messages"},
                        "target_path": str(target_path),
                    },
                    context,
                )
            )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_download_missing_ref")
        self.assertEqual(result.error.category.value, "validation")

    def test_messages_sync_downloads_records_cursor_and_deduplicates(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"saved_messages": _telegram_messages_fixture()},
            downloads={
                "saved_messages:10:photo-10": {"content": b"photo-one", "mime_type": "image/jpeg"},
                "saved_messages:11:photo-11-a": {"content": b"photo-two", "mime_type": "image/jpeg"},
                "saved_messages:11:photo-11-b": {"content": b"photo-tre", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            first = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "media_types": ["photo"],
                    },
                    context,
                )
            )
            files_after_first = _media_files(db_path)
            cursor_after_first = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:saved_messages:photo",
            )
            second = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "media_types": ["photo"],
                    },
                    context,
                )
            )
            written_media = sorted(path for path in (data_dir / "library").rglob("*.jpg"))

        self.assertTrue(first.is_success)
        self.assertEqual(first.data["summary"]["downloaded"], 3)
        self.assertEqual(first.data["summary"]["files_downloaded"], 3)
        self.assertEqual(len(files_after_first), 3)
        self.assertEqual(cursor_after_first["cursor_value"], "14")
        self.assertTrue(second.is_success)
        self.assertEqual(second.data["summary"]["queued"], 0)
        self.assertEqual(len(written_media), 3)
        self.assertTrue(str(written_media[0]).endswith(".jpg"))
        self.assertIn("/library/telegram/photo/2026/07/", str(written_media[0]))

    def test_messages_sync_partial_failure_does_not_advance_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"saved_messages": _telegram_messages_fixture()[:2]},
            downloads={
                "saved_messages:10:photo-10": {"content": b"photo-one", "mime_type": "image/jpeg"},
                "saved_messages:11:photo-11-a": telegram_client.TelegramClientError("network failed"),
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "limit": 2,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="messages:saved_messages")

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_messages_sync_partial")
        self.assertIsNone(cursor)

    def test_messages_sync_cancellation_records_failed_state_and_removes_partial(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"saved_messages": _telegram_messages_fixture()[:1]},
            downloads={
                "saved_messages:10:photo-10": {
                    "stream_content": b"incomplete-photo",
                    "mime_type": "image/jpeg",
                    "cancel_after_stream": True,
                }
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "limit": 1,
                    },
                    context,
                )
            )
            statuses = db.get_media_statuses(
                db_path,
                [{"platform": "telegram", "remote_id": "saved_messages:10:photo-10"}],
            )
            files = _media_files(db_path)
            partial_files = list((data_dir / "library").rglob("*.partial"))
            with db.connect(db_path) as connection:
                runs = connection.execute(
                    "SELECT status FROM runs WHERE name = ?",
                    ("telegram.messages.sync",),
                ).fetchall()

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_messages_sync_failed")
        self.assertTrue(result.data["summary"]["cancelled"])
        self.assertEqual(result.data["summary"]["failed"], 1)
        self.assertEqual(result.data["summary"]["files_failed"], 1)
        self.assertEqual(statuses[("telegram", "saved_messages:10:photo-10")], "failed")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["status"], "failed")
        self.assertEqual(partial_files, [])
        self.assertEqual([row["status"] for row in runs], ["failed"])

    def test_messages_sync_dry_run_with_fake_client_does_not_write(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()})
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake, dry_run=True)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "media_types": ["photo"],
                    },
                    context,
                )
            )

            db_exists = db_path.exists()
            media_files = list((data_dir / "library").rglob("*")) if (data_dir / "library").exists() else []

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["queued"], 3)
        self.assertEqual(len(result.data["planned_downloads"]), 3)
        self.assertFalse(db_exists)
        self.assertEqual(media_files, [])


def _telegram_context(
    temp_dir: str,
    fake: FakeTelegramClient,
    *,
    dry_run: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> tuple[ToolContext, Path, Path]:
    data_dir = Path(temp_dir) / "data"
    db_path = data_dir / "mediagent.sqlite3"
    session_path = data_dir / "credentials" / "telegram.session"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text("fake-session", encoding="utf-8")
    env = {
        "MEDIAGENT_DATA_DIR": str(data_dir),
        "MEDIAGENT_DB_PATH": str(db_path),
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "secret-api-hash",
        "TELEGRAM_PHONE_NUMBER": "+886912345678",
        "TELEGRAM_SESSION_FILE": str(session_path),
    }
    env.update(env_overrides or {})
    context = ToolContext.from_env(
        env=env,
        cwd=Path(temp_dir),
        dry_run=dry_run,
        http_client=fake,
    )
    return context, data_dir, db_path


def _telegram_messages_fixture() -> list[dict[str, Any]]:
    return [
        {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "sender": {"id": "42", "username": "media_user"},
            "caption": "first",
            "media": [
                {
                    "id": "photo-10",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "file_name": "first.jpg",
                    "download_ref": {
                        "chat_id": "saved_messages",
                        "message_id": "10",
                        "media_id": "photo-10",
                    },
                }
            ],
        },
        {
            "id": 11,
            "date": "2026-07-21T10:05:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "sender": {"id": "42", "username": "media_user"},
            "grouped_id": "album-77",
            "media": [
                {
                    "id": "photo-11-a",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "download_ref": {
                        "chat_id": "saved_messages",
                        "message_id": "11",
                        "media_id": "photo-11-a",
                    },
                },
                {
                    "id": "photo-11-b",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "download_ref": {
                        "chat_id": "saved_messages",
                        "message_id": "11",
                        "media_id": "photo-11-b",
                    },
                },
            ],
        },
        {
            "id": 12,
            "date": "2026-07-21T10:10:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "media": [{"id": "video-12", "kind": "video", "mime_type": "video/mp4"}],
        },
        {
            "id": 13,
            "date": "2026-07-21T10:15:00+00:00",
            "protected_content": True,
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "media": [{"id": "photo-13", "kind": "photo", "mime_type": "image/jpeg"}],
        },
        {
            "id": 14,
            "date": "2026-07-21T10:20:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "media": [{"id": "doc-14", "kind": "document", "mime_type": "application/pdf"}],
        },
    ]


def _media_files(db_path: Path) -> list[dict[str, Any]]:
    return db.list_media_files(db_path, platform="telegram")
