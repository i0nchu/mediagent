import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.platforms.telegram import client as telegram_client
from mediagent.platforms.telegram import parser as telegram_parser
from mediagent.tools import telegram_tools
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
            source_messages = self.messages.get(ref["source_key"], [])
            selected = [
                dict(message)
                for message in source_messages
                if int(message["id"]) == int(ref["message_id"])
            ]
            if selected and selected[0].get("grouped_id"):
                grouped_id = selected[0]["grouped_id"]
                selected = [
                    dict(message)
                    for message in source_messages
                    if message.get("grouped_id") == grouped_id
                ]
            for message in selected:
                message["source_url"] = ref["source_url"]
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

    def test_messages_collect_full_sync_ignores_cursor_and_default_limit(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:saved_messages",
                cursor_value="14",
            )

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
        self.assertEqual(fake.calls[-1][1]["after_by_source"], {"saved_messages": None})
        self.assertEqual(result.data["summary"]["messages_scanned"], 5)
        self.assertEqual(fake.calls[-1][1]["limit"], None)

    def test_messages_sync_full_sync_ignores_existing_cursor(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake, dry_run=True)
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:saved_messages:photo",
                cursor_value="14",
            )

            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "media_types": ["photo"],
                        "full_sync": True,
                        "store_cursor": False,
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(fake.calls[-1][1]["after_by_source"], {"saved_messages": None})
        self.assertEqual(fake.calls[-1][1]["limit"], None)

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

    def test_messages_collect_expands_album_message_link(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={
                "curated": [
                    {
                        "id": 50,
                        "date": "2026-07-22T01:00:00+00:00",
                        "chat": {"id": "curated", "title": "Mediagent Inbox", "type": "channel"},
                        "text": "save this album https://t.me/source_channel/100",
                        "media": [],
                    }
                ],
                "link:source_channel": _telegram_album_messages_fixture("source_channel", start_id=100),
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
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["extracted_message_links"], 1)
        self.assertEqual(result.data["summary"]["linked_messages"], 3)
        self.assertEqual(result.data["summary"]["message_link_depth_reached"], 1)
        self.assertEqual(len(result.data["items"]), 3)
        self.assertEqual(
            [item["metadata"]["telegram"]["message_id"] for item in result.data["items"]],
            ["100", "101", "102"],
        )

    def test_messages_collect_follows_nested_message_links(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={
                "link:first_channel": [
                    {
                        "id": 100,
                        "date": "2026-07-22T01:00:00+00:00",
                        "chat": {
                            "id": "first_channel",
                            "title": "First",
                            "type": "channel",
                            "username": "first_channel",
                        },
                        "text": "nested https://t.me/second_channel/200",
                        "media": [],
                    }
                ],
                "link:second_channel": [
                    {
                        "id": 200,
                        "date": "2026-07-22T01:05:00+00:00",
                        "chat": {
                            "id": "second_channel",
                            "title": "Second",
                            "type": "channel",
                            "username": "second_channel",
                        },
                        "media": [
                            {
                                "id": "photo-200",
                                "kind": "photo",
                                "mime_type": "image/jpeg",
                                "download_ref": {
                                    "chat_id": "second_channel",
                                    "chat_username": "second_channel",
                                    "message_id": "200",
                                    "media_id": "photo-200",
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
                        "message_links": ["https://t.me/first_channel/100"],
                        "media_types": ["photo"],
                    },
                    context,
                )
            )

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["extracted_message_links"], 1)
        self.assertEqual(result.data["summary"]["linked_messages"], 1)
        self.assertEqual(result.data["summary"]["message_link_depth_reached"], 1)
        self.assertEqual(len(result.data["items"]), 1)
        self.assertEqual(result.data["items"][0]["remote_id"], "second_channel:200:photo-200")

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

    def test_album_metadata_is_scoped_by_chat_and_counts_media_entries(self) -> None:
        first_chat = _telegram_album_messages_fixture("first_chat", start_id=100)[:1]
        first_chat[0]["media"].append(
            {
                "id": "photo-extra",
                "kind": "photo",
                "mime_type": "image/jpeg",
            }
        )
        second_chat = _telegram_album_messages_fixture("second_chat", start_id=200)[:1]

        items, _summary = telegram_parser.normalize_messages(
            [*first_chat, *second_chat],
            include_intake_metadata=True,
        )

        first_albums = [item["metadata"]["telegram"]["album"] for item in items[:2]]
        second_album = items[2]["metadata"]["telegram"]["album"]
        self.assertEqual(first_albums, [
            {"group_id": "album-telegram-realistic", "position": 1, "size": 2},
            {"group_id": "album-telegram-realistic", "position": 2, "size": 2},
        ])
        self.assertEqual(
            second_album,
            {"group_id": "album-telegram-realistic", "position": 1, "size": 1},
        )

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

    def test_telegram_download_idle_timeout_allows_slow_progress(self) -> None:
        class SlowDownloadClient:
            async def download_media(self, raw_message: Any, *, file: str, progress_callback: Any = None) -> str:
                path = Path(file)
                path.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                for chunk in (b"one", b"two", b"three"):
                    await asyncio.sleep(0.01)
                    with path.open("ab") as handle:
                        handle.write(chunk)
                    written += len(chunk)
                    if progress_callback:
                        progress_callback(written, 11)
                return str(path)

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "slow.partial"

            result = asyncio.run(
                telegram_client._download_media_with_idle_timeout(
                    SlowDownloadClient(),
                    object(),
                    destination=destination,
                    idle_timeout_seconds=0.02,
                )
            )
            content = destination.read_bytes()

        self.assertEqual(result, str(destination))
        self.assertEqual(content, b"onetwothree")

    def test_telegram_download_idle_timeout_fails_without_progress(self) -> None:
        class StalledDownloadClient:
            async def download_media(self, raw_message: Any, *, file: str, progress_callback: Any = None) -> str:
                await asyncio.sleep(1)
                return file

        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "stalled.partial"

            with self.assertRaises(TimeoutError):
                asyncio.run(
                    telegram_client._download_media_with_idle_timeout(
                        StalledDownloadClient(),
                        object(),
                        destination=destination,
                        idle_timeout_seconds=0.02,
                    )
                )

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

    def test_messages_sync_reports_aggregate_item_progress(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"saved_messages": _telegram_messages_fixture()[:1]},
            downloads={
                "saved_messages:10:photo-10": {"content": b"photo-one", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir, patch(
            "mediagent.tools.telegram_tools.ProgressLogger"
        ) as progress_factory:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {"db_path": str(db_path), "chat": "saved_messages", "limit": 1},
                    context,
                )
            )

        self.assertTrue(result.is_success)
        progress_factory.return_value.report.assert_called_once_with(
            completed=1,
            pending=0,
            failed=0,
        )

    def test_messages_sync_downloads_all_album_media_from_message_link(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"link:source_channel": _telegram_album_messages_fixture("source_channel", start_id=100)},
            downloads={
                "source_channel:100:photo-100": {"content": b"photo-100", "mime_type": "image/jpeg"},
                "source_channel:101:photo-101": {"content": b"photo-101", "mime_type": "image/jpeg"},
                "source_channel:102:photo-102": {"content": b"photo-102", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)

            result = asyncio.run(
                registry.run(
                    "telegram.messages.sync",
                    {
                        "db_path": str(db_path),
                        "message_links": ["https://t.me/source_channel/100"],
                        "media_types": ["photo"],
                    },
                    context,
                )
            )
            written_media = sorted(path for path in (data_dir / "library").rglob("*.jpg"))

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["collected"], 3)
        self.assertEqual(result.data["summary"]["downloaded"], 3)
        self.assertEqual(result.data["summary"]["files_downloaded"], 3)
        self.assertEqual(result.data["message_links"][0], {"url": "https://t.me/source_channel/100", "status": "resolved", "items": 3})
        self.assertEqual(len(written_media), 3)

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

    def test_unified_inbox_sync_uses_one_root_scan_for_direct_forwarded_and_linked_media(self) -> None:
        registry = create_default_registry()
        external_url = "https://1.1.1.1/inbox.jpg"

        class UnifiedFake(FakeTelegramClient):
            def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
                self.calls.append(("http_head", {"url": url}))
                return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "8"}, b"", url)

            def get_limited(
                self,
                url: str,
                *,
                headers=None,
                timeout: float = 30.0,
                max_bytes: int = 1024 * 1024,
            ) -> HttpResponse:
                self.calls.append(("http_get", {"url": url}))
                return HttpResponse(200, {"content-type": "image/jpeg"}, b"external"[:max_bytes], url)

        direct = {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "forward": {
                "type": "channel",
                "id": "source-1",
                "name": "Source",
                "message_id": "99",
                "date": "2026-07-20T10:00:00+00:00",
                "access_hash": "must-not-persist",
            },
            "media": [
                {
                    "id": "photo-10",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "download_ref": {
                        "chat_id": "saved_messages",
                        "message_id": "10",
                        "media_id": "photo-10",
                    },
                }
            ],
        }
        link_message = {
            "id": 11,
            "date": "2026-07-21T10:01:00+00:00",
            "chat": {"id": "saved_messages", "title": "Saved Messages", "type": "saved_messages"},
            "text": f"{external_url} https://t.me/source_channel/100",
            "media": [],
        }
        linked_album = _telegram_album_messages_fixture("source_channel", start_id=100)[:2]
        fake = UnifiedFake(
            messages={"saved_messages": [direct, link_message], "link:source_channel": linked_album},
            downloads={
                "saved_messages:10:photo-10": {"content": b"direct-1", "mime_type": "image/jpeg"},
                "source_channel:100:photo-100": {"content": b"linked-1", "mime_type": "image/jpeg"},
                "source_channel:101:photo-101": {"content": b"linked-2", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="inbox:saved_messages")
            legacy_messages_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="messages:saved_messages",
            )
            legacy_links_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="links:saved_messages",
            )
            with db.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT metadata_json FROM media_items WHERE platform = ? AND remote_id = ?",
                    ("telegram", "saved_messages:10:photo-10"),
                ).fetchone()
            metadata = json.loads(row["metadata_json"])

        root_scans = [
            call
            for call in fake.calls
            if call[0] == "messages_collect" and call[1]["chats"] == ["saved_messages"]
        ]
        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(len(root_scans), 1)
        self.assertEqual(result.data["summary"]["direct_media_items"], 1)
        self.assertEqual(result.data["summary"]["forwarded_media_items"], 1)
        self.assertEqual(result.data["summary"]["external_links"], 1)
        self.assertEqual(result.data["summary"]["telegram_message_links"], 1)
        self.assertEqual(result.data["summary"]["telegram_link_media_items"], 2)
        self.assertEqual(result.data["summary"]["album_groups"], 1)
        self.assertEqual(result.data["summary"]["downloaded"], 4)
        self.assertEqual(len(result.data["asset_ids"]), 4)
        self.assertEqual(cursor["cursor_value"], "11")
        self.assertIsNone(legacy_messages_cursor)
        self.assertIsNone(legacy_links_cursor)
        self.assertNotIn("must-not-persist", json.dumps(metadata))
        self.assertEqual(metadata["telegram"]["forward_origin"]["id"], "source-1")

    def test_unified_inbox_sync_failure_does_not_advance_cursor(self) -> None:
        registry = create_default_registry()
        download_key = "saved_messages:10:photo-10"
        external_url = "https://1.1.1.1/retry-shared.jpg"

        class UnifiedFake(FakeTelegramClient):
            def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "6"}, b"", url)

            def get_limited(
                self,
                url: str,
                *,
                headers=None,
                timeout: float = 30.0,
                max_bytes: int = 1024 * 1024,
            ) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg"}, b"shared", url)

        message = _telegram_messages_fixture()[0]
        message["text"] = external_url
        fake = UnifiedFake(
            messages={"saved_messages": [message]},
            downloads={
                download_key: telegram_client.TelegramClientError("network failed"),
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="inbox:saved_messages")
            fake.downloads[download_key] = {"content": b"retried", "mime_type": "image/jpeg"}
            retry_context, _data_dir, _db_path = _telegram_context(temp_dir, fake)
            retried = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    retry_context,
                )
            )
            retried_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )
            links = db.list_links(db_path)

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_inbox_sync_partial")
        self.assertEqual(result.data["summary"]["cursor_reason"], "run_not_successful")
        self.assertIsNone(cursor)
        self.assertTrue(retried.is_success, retried.to_dict())
        self.assertEqual(retried.data["summary"]["direct_downloaded"], 1)
        self.assertEqual(retried_cursor["cursor_value"], "10")
        self.assertEqual(len(links), 1)
        self.assertEqual(len(links[0]["source_provenance"]), 1)

    def test_unified_inbox_sync_pending_nested_link_does_not_advance_cursor(self) -> None:
        registry = create_default_registry()
        root = {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"},
            "text": "https://t.me/source_a/100",
            "media": [],
        }
        linked = {
            "id": 100,
            "date": "2026-07-21T10:01:00+00:00",
            "chat": {"id": "source_a", "type": "channel", "username": "source_a"},
            "text": "https://t.me/source_b/200",
            "media": [],
        }
        fake = FakeTelegramClient(
            messages={"saved_messages": [root], "link:source_a": [linked]},
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "max_message_link_depth": 1,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="inbox:saved_messages")

        followed = [
            call[1]["message_links"]
            for call in fake.calls
            if call[0] == "messages_collect" and call[1]["message_links"]
        ]
        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "telegram_inbox_sync_partial")
        self.assertEqual(result.data["summary"]["pending_message_links"], 1)
        self.assertEqual(result.data["summary"]["cursor_reason"], "run_not_successful")
        self.assertEqual(followed, [["https://t.me/source_a/100"]])
        self.assertIsNone(cursor)

    def test_unified_inbox_sync_retryable_link_resolution_does_not_advance_cursor(self) -> None:
        registry = create_default_registry()
        external_url = "https://1.1.1.1/transient.jpg"

        class TransientLinkFake(FakeTelegramClient):
            fail_resolution = True

            def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
                if self.fail_resolution:
                    raise TimeoutError("temporary resolver timeout")
                return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "5"}, b"", url)

            def get_limited(
                self,
                url: str,
                *,
                headers=None,
                timeout: float = 30.0,
                max_bytes: int = 1024 * 1024,
            ) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg"}, b"image", url)

        message = {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"},
            "text": external_url,
            "media": [],
        }
        fake = TransientLinkFake(messages={"saved_messages": [message]})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            first = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            first_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )
            fake.fail_resolution = False
            retry_context, _data_dir, _db_path = _telegram_context(temp_dir, fake)
            second = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    retry_context,
                )
            )
            second_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )

        self.assertFalse(first.is_success)
        self.assertEqual(first.error.code, "telegram_inbox_sync_partial")
        self.assertEqual(first.data["summary"]["retryable_links_unresolved"], 1)
        self.assertIsNone(first_cursor)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["retryable_links_unresolved"], 0)
        self.assertEqual(second.data["summary"]["link_downloaded"], 1)
        self.assertEqual(second_cursor["cursor_value"], "10")

    def test_unified_inbox_sync_expands_album_at_scan_boundary(self) -> None:
        registry = create_default_registry()
        album = _telegram_album_messages_fixture("saved_messages", start_id=100)
        fake = FakeTelegramClient(
            messages={"saved_messages": album},
            downloads={
                f"saved_messages:{100 + index}:photo-{100 + index}": {
                    "content": f"album-{index}".encode(),
                    "mime_type": "image/jpeg",
                }
                for index in range(3)
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            first = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "max_messages": 1,
                    },
                    context,
                )
            )
            first_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )
            retry_context, _data_dir, _db_path = _telegram_context(temp_dir, fake)
            second = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "max_messages": 1,
                    },
                    retry_context,
                )
            )
            second_cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )
            with db.connect(db_path) as connection:
                media_item_count = connection.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]

        download_calls = [call for call in fake.calls if call[0] == "media_download"]
        root_limits = [
            call[1]["limit"]
            for call in fake.calls
            if call[0] == "messages_collect" and call[1]["chats"] == ["saved_messages"]
        ]
        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["summary"]["incomplete_album_groups"], 0)
        self.assertEqual(first.data["summary"]["direct_media_items"], 3)
        self.assertEqual(first.data["summary"]["direct_downloaded"], 3)
        self.assertEqual(first_cursor["cursor_value"], "102")
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["direct_media_items"], 0)
        self.assertEqual(second_cursor["cursor_value"], "102")
        self.assertEqual(len(download_calls), 3)
        self.assertEqual(media_item_count, 3)
        self.assertEqual(root_limits, [1 + telegram_tools.ROOT_ALBUM_LOOKAHEAD] * 2)

    def test_unified_inbox_sync_ignores_legacy_limit_without_starving_media(self) -> None:
        registry = create_default_registry()
        messages = _telegram_album_messages_fixture("saved_messages", start_id=100)
        for message in messages:
            message.pop("grouped_id", None)
        fake = FakeTelegramClient(
            messages={"saved_messages": messages[:2]},
            downloads={
                "saved_messages:100:photo-100": {"content": b"first", "mime_type": "image/jpeg"},
                "saved_messages:101:photo-101": {"content": b"second", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "limit": 1,
                    },
                    context,
                )
            )
            cursor = db.get_sync_cursor(db_path, platform="telegram", cursor_name="inbox:saved_messages")

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["summary"]["direct_downloaded"], 2)
        self.assertNotIn("intake_items_deferred", result.data["summary"])
        self.assertEqual(result.data["summary"]["cursor_reason"], "stored")
        self.assertEqual(cursor["cursor_value"], "101")

    def test_unified_inbox_sync_deduplicates_overlapping_message_link_albums(self) -> None:
        registry = create_default_registry()
        root = {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"},
            "text": "https://t.me/source_channel/100 https://t.me/source_channel/101",
            "media": [],
        }
        album = _telegram_album_messages_fixture("source_channel", start_id=100)
        fake = FakeTelegramClient(
            messages={"saved_messages": [root], "link:source_channel": album},
            downloads={
                f"source_channel:{100 + index}:photo-{100 + index}": {
                    "content": f"album-{index}".encode(),
                    "mime_type": "image/jpeg",
                }
                for index in range(3)
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )

        download_calls = [call for call in fake.calls if call[0] == "media_download"]
        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["summary"]["telegram_message_links"], 2)
        self.assertEqual(result.data["summary"]["telegram_link_media_items"], 3)
        self.assertEqual(result.data["summary"]["downloaded"], 3)
        self.assertEqual(len(download_calls), 3)
        self.assertEqual(len(result.data["asset_ids"]), 3)

    def test_unified_inbox_sync_deduplicates_direct_and_linked_content(self) -> None:
        registry = create_default_registry()
        external_url = "https://1.1.1.1/same.jpg"

        class UnifiedFake(FakeTelegramClient):
            def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "12"}, b"", url)

            def get_limited(
                self,
                url: str,
                *,
                headers=None,
                timeout: float = 30.0,
                max_bytes: int = 1024 * 1024,
            ) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg"}, b"same-content", url)

        direct = {
            "id": 10,
            "date": "2026-07-21T10:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"},
            "text": external_url,
            "media": [
                {
                    "id": "photo-10",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "download_ref": {
                        "chat_id": "saved_messages",
                        "message_id": "10",
                        "media_id": "photo-10",
                    },
                }
            ],
        }
        fake = UnifiedFake(
            messages={"saved_messages": [direct]},
            downloads={
                "saved_messages:10:photo-10": {"content": b"same-content", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            records = db.list_media_files(db_path)
            visible_images = list((data_dir / "library").rglob("*.jpg"))

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["summary"]["downloaded"], 2)
        self.assertEqual(len(result.data["asset_ids"]), 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["library_entry_id"] for record in records}), 1)
        self.assertEqual(len({record["local_path"] for record in records}), 1)
        self.assertEqual(len(visible_images), 1)

    def test_unified_inbox_sync_preserves_duplicate_link_provenance(self) -> None:
        registry = create_default_registry()
        external_url = "https://1.1.1.1/shared.jpg"

        class UnifiedFake(FakeTelegramClient):
            def head(self, url: str, *, headers=None, timeout: float = 30.0) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "6"}, b"", url)

            def get_limited(
                self,
                url: str,
                *,
                headers=None,
                timeout: float = 30.0,
                max_bytes: int = 1024 * 1024,
            ) -> HttpResponse:
                return HttpResponse(200, {"content-type": "image/jpeg"}, b"shared", url)

        messages = [
            {
                "id": message_id,
                "date": f"2026-07-21T10:0{message_id - 10}:00+00:00",
                "chat": {"id": "saved_messages", "type": "saved_messages"},
                "text": external_url,
                "media": [],
            }
            for message_id in (10, 11)
        ]
        fake = UnifiedFake(messages={"saved_messages": messages})
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            links = db.list_links(db_path)

        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(result.data["summary"]["external_links"], 1)
        self.assertEqual(len(links), 1)
        self.assertEqual(len(links[0]["source_provenance"]), 2)
        self.assertEqual(
            {entry["source_message_id"] for entry in links[0]["source_provenance"]},
            {"10", "11"},
        )

    def test_legacy_message_normalization_omits_unified_intake_metadata(self) -> None:
        message = _telegram_album_messages_fixture("saved_messages", start_id=100)[0]
        message["forward"] = {
            "type": "channel",
            "id": "source-channel",
            "message_id": "77",
        }
        message["_inbox_delivery"] = "direct"

        legacy_items, _summary = telegram_parser.normalize_messages([message])
        unified_items, _summary = telegram_parser.normalize_messages(
            [message],
            include_intake_metadata=True,
        )

        legacy = legacy_items[0]["metadata"]["telegram"]
        unified = unified_items[0]["metadata"]["telegram"]
        self.assertNotIn("album", legacy)
        self.assertNotIn("forward_origin", legacy)
        self.assertNotIn("inbox_delivery", legacy)
        self.assertIn("album", unified)
        self.assertEqual(unified["forward_origin"]["id"], "source-channel")
        self.assertEqual(unified["inbox_delivery"], "direct")

    def test_unified_inbox_sync_is_hidden_and_repeated_runs_are_idempotent(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(
            messages={"saved_messages": _telegram_messages_fixture()[:1]},
            downloads={
                "saved_messages:10:photo-10": {"content": b"photo-one", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            first = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            download_calls_after_first = sum(1 for name, _payload in fake.calls if name == "media_download")
            second = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            download_calls_after_second = sum(1 for name, _payload in fake.calls if name == "media_download")
            files = _media_files(db_path)

        public_tools = {spec.name for spec in registry.list()}
        hidden_tools = {spec.name for spec in registry.list(include_hidden=True)}
        self.assertTrue(first.is_success)
        self.assertTrue(second.is_success)
        self.assertEqual(first.data["summary"]["downloaded"], 1)
        self.assertEqual(second.data["summary"]["downloaded"], 0)
        self.assertEqual(download_calls_after_first, 1)
        self.assertEqual(download_calls_after_second, 1)
        self.assertEqual(len(files), 1)
        self.assertNotIn("telegram.inbox.sync", public_tools)
        self.assertIn("telegram.inbox.sync", hidden_tools)

    def test_unified_inbox_sync_continues_from_legacy_link_cursor(self) -> None:
        registry = create_default_registry()
        messages = [
            {
                "id": message_id,
                "date": f"2026-07-21T10:0{message_id - 10}:00+00:00",
                "chat": {"id": "saved_messages", "type": "saved_messages"},
                "media": [
                    {
                        "id": f"photo-{message_id}",
                        "kind": "photo",
                        "mime_type": "image/jpeg",
                        "download_ref": {
                            "chat_id": "saved_messages",
                            "message_id": str(message_id),
                            "media_id": f"photo-{message_id}",
                        },
                    }
                ],
            }
            for message_id in (10, 11)
        ]
        fake = FakeTelegramClient(
            messages={"saved_messages": messages},
            downloads={
                "saved_messages:11:photo-11": {"content": b"new-media", "mime_type": "image/jpeg"},
            },
        )
        with TemporaryDirectory() as temp_dir:
            context, _data_dir, db_path = _telegram_context(temp_dir, fake)
            db.initialize_database(db_path)
            db.set_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="links:saved_messages",
                cursor_value="10",
            )

            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            cursor = db.get_sync_cursor(
                db_path,
                platform="telegram",
                cursor_name="inbox:saved_messages",
            )
            fake.downloads["saved_messages:10:photo-10"] = {
                "content": b"older-media",
                "mime_type": "image/jpeg",
            }
            full_context, _data_dir, _db_path = _telegram_context(temp_dir, fake)
            full = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {
                        "db_path": str(db_path),
                        "chat": "saved_messages",
                        "full_sync": True,
                    },
                    full_context,
                )
            )

        root_scans = [
            call for call in fake.calls if call[0] == "messages_collect" and call[1]["chats"]
        ]
        self.assertTrue(result.is_success, result.to_dict())
        self.assertEqual(root_scans[0][1]["after_by_source"], {"saved_messages": 10})
        self.assertEqual(result.data["summary"]["legacy_cursor_fallbacks"], 1)
        self.assertEqual(result.data["summary"]["direct_downloaded"], 1)
        self.assertTrue(any("full sync" in warning for warning in result.warnings))
        self.assertEqual(cursor["cursor_value"], "11")
        self.assertTrue(full.is_success, full.to_dict())
        self.assertEqual(root_scans[1][1]["after_by_source"], {"saved_messages": None})
        self.assertEqual(full.data["summary"]["legacy_cursor_fallbacks"], 0)
        self.assertEqual(full.data["summary"]["direct_downloaded"], 1)

    def test_unified_inbox_sync_dry_run_has_no_side_effects(self) -> None:
        registry = create_default_registry()
        fake = FakeTelegramClient(messages={"saved_messages": _telegram_messages_fixture()[:1]})
        with TemporaryDirectory() as temp_dir:
            context, data_dir, db_path = _telegram_context(temp_dir, fake, dry_run=True)
            result = asyncio.run(
                registry.run(
                    "telegram.inbox.sync",
                    {"db_path": str(db_path), "chat": "saved_messages"},
                    context,
                )
            )
            library_files = list((data_dir / "library").rglob("*")) if (data_dir / "library").exists() else []

        self.assertTrue(result.is_success)
        self.assertEqual(result.data["summary"]["cursor_reason"], "dry_run")
        self.assertFalse(db_path.exists())
        self.assertEqual(library_files, [])


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


def _telegram_album_messages_fixture(chat_id: str, *, start_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": start_id + index,
            "date": f"2026-07-22T01:0{index}:00+00:00",
            "chat": {"id": chat_id, "title": "Source", "type": "channel", "username": chat_id},
            "grouped_id": "album-telegram-realistic",
            "media": [
                {
                    "id": f"photo-{start_id + index}",
                    "kind": "photo",
                    "mime_type": "image/jpeg",
                    "download_ref": {
                        "chat_id": chat_id,
                        "chat_username": chat_id,
                        "message_id": str(start_id + index),
                        "media_id": f"photo-{start_id + index}",
                    },
                }
            ],
        }
        for index in range(3)
    ]


def _media_files(db_path: Path) -> list[dict[str, Any]]:
    return db.list_media_files(db_path, platform="telegram")
