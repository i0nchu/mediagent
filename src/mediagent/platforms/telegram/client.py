"""Telegram client boundary.

The real adapter uses Telethon lazily. Tests can pass a fake object through
ToolContext.http_client when it implements the telegram_* methods below.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mediagent.platforms.telegram.auth import TelegramConfig


class TelegramClientError(RuntimeError):
    pass


class TelegramDependencyError(TelegramClientError):
    pass


class TelegramAuthError(TelegramClientError):
    pass


def client_from_context(context: Any, config: TelegramConfig) -> Any:
    candidate = getattr(context, "http_client", None)
    if candidate is not None and any(
        hasattr(candidate, name)
        for name in (
            "telegram_auth_login_start",
            "telegram_auth_login_complete",
            "telegram_auth_status",
            "telegram_list_dialogs",
            "telegram_collect_messages",
            "telegram_download_media",
        )
    ):
        return candidate
    return TelethonTelegramClient(config)


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class TelethonTelegramClient:
    def __init__(self, config: TelegramConfig) -> None:
        self.config = config

    async def telegram_auth_login_start(
        self,
        config: TelegramConfig,
        *,
        phone_number: str,
    ) -> dict[str, Any]:
        async with self._connected_client(config) as client:
            if await client.is_user_authorized():
                me = await client.get_me()
                return {
                    "status": "already_authorized",
                    "usable": True,
                    "account": {
                        "id": getattr(me, "id", None),
                        "username": getattr(me, "username", None),
                        "display_name": _display_name(me),
                    },
                }
            sent_code = await client.send_code_request(phone_number)
        return {
            "status": "code_sent",
            "usable": False,
            "phone_code_hash": getattr(sent_code, "phone_code_hash", None),
            "code_type": type(getattr(sent_code, "type", None)).__name__ if getattr(sent_code, "type", None) else None,
        }

    async def telegram_auth_login_complete(
        self,
        config: TelegramConfig,
        *,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        password: str | None,
    ) -> dict[str, Any]:
        try:
            from telethon.errors import SessionPasswordNeededError
        except ImportError as exc:
            raise TelegramDependencyError("Install the telethon package to use real Telegram sessions.") from exc
        async with self._connected_client(config) as client:
            try:
                me = await client.sign_in(
                    phone=phone_number,
                    code=code,
                    phone_code_hash=phone_code_hash,
                )
            except SessionPasswordNeededError:
                if not password:
                    return {
                        "status": "password_required",
                        "usable": False,
                        "password_required": True,
                    }
                me = await client.sign_in(password=password)
            authorized = await client.is_user_authorized()
        return {
            "status": "usable" if authorized else "unauthorized",
            "usable": bool(authorized),
            "password_required": False,
            "account": {
                "id": getattr(me, "id", None),
                "username": getattr(me, "username", None),
                "display_name": _display_name(me),
            }
            if me
            else None,
        }

    async def telegram_auth_status(self, config: TelegramConfig) -> dict[str, Any]:
        if config.session_path and not config.session_path.exists():
            return {
                "usable": False,
                "status": "missing_session",
                "account": None,
                "session_file_exists": False,
            }
        async with self._connected_client(config) as client:
            authorized = await client.is_user_authorized()
            if not authorized:
                return {
                    "usable": False,
                    "status": "unauthorized",
                    "account": None,
                    "session_file_exists": config.session_path.exists() if config.session_path else False,
                }
            me = await client.get_me()
            return {
                "usable": True,
                "status": "usable",
                "account": {
                    "id": getattr(me, "id", None),
                    "username": getattr(me, "username", None),
                    "display_name": _display_name(me),
                },
                "session_file_exists": config.session_path.exists() if config.session_path else False,
            }

    async def telegram_list_dialogs(
        self,
        config: TelegramConfig,
        *,
        limit: int | None,
        chat_types: list[str] | None,
    ) -> dict[str, Any]:
        dialogs: list[dict[str, Any]] = []
        allowed = set(chat_types or [])
        async with self._connected_client(config) as client:
            async for dialog in client.iter_dialogs(limit=limit):
                normalized = _dialog_to_dict(dialog)
                if allowed and normalized["type"] not in allowed:
                    continue
                dialogs.append(normalized)
        return {"dialogs": dialogs, "summary": {"dialogs": len(dialogs)}}

    async def telegram_collect_messages(
        self,
        config: TelegramConfig,
        *,
        chats: list[Any],
        after_by_source: dict[str, int | None],
        limit: int,
        message_ids_by_source: dict[str, list[int]] | None,
        message_links: list[str] | None,
        include_protected: bool,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        source_summaries: list[dict[str, Any]] = []
        link_refs = parse_message_links(message_links or [])
        async with self._connected_client(config) as client:
            for selector in chats:
                source_key = source_key_for_chat(selector)
                entity_selector = _entity_selector(selector)
                entity = await client.get_entity(entity_selector)
                selected_ids = (message_ids_by_source or {}).get(source_key)
                if selected_ids:
                    raw_messages = await client.get_messages(entity, ids=selected_ids)
                    raw_list = raw_messages if isinstance(raw_messages, list) else [raw_messages]
                    chat_messages = [_message_to_dict(item, entity=entity) for item in raw_list if item is not None]
                else:
                    after_id = after_by_source.get(source_key)
                    chat_messages = []
                    async for message in client.iter_messages(entity, min_id=after_id or 0, limit=limit, reverse=True):
                        chat_messages.append(_message_to_dict(message, entity=entity))
                messages.extend(chat_messages)
                source_summaries.append(_source_summary(source_key, chat_messages))
            for ref in link_refs:
                entity = await client.get_entity(ref["chat"])
                message = await client.get_messages(entity, ids=[ref["message_id"]])
                raw_message = message[0] if isinstance(message, list) else message
                if raw_message:
                    normalized = _message_to_dict(raw_message, entity=entity)
                    normalized["source_url"] = ref.get("source_url")
                    messages.append(normalized)
                    source_summaries.append(_source_summary(ref["source_key"], [normalized], cursor_eligible=False))
        return {
            "messages": messages,
            "source_summaries": source_summaries,
            "include_protected": include_protected,
        }

    async def telegram_download_media(
        self,
        config: TelegramConfig,
        *,
        download_ref: dict[str, Any],
        target_path: str | None = None,
        partial_path: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        destination = Path(partial_path)
        async with self._connected_client(config) as client:
            entity = await client.get_entity(download_entity_selector(download_ref))
            message = await client.get_messages(entity, ids=[int(download_ref["message_id"])])
            raw_message = message[0] if isinstance(message, list) else message
            if raw_message is None:
                raise TelegramClientError("Telegram message was not found.")
            try:
                downloaded_path = await asyncio.wait_for(
                    client.download_media(raw_message, file=str(destination)),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise TelegramClientError("Telegram download timed out.") from exc
        if downloaded_path is None:
            raise TelegramClientError("Telegram message does not have downloadable media.")
        return {"path": str(downloaded_path or destination), "mime_type": None}

    def _client(self, config: TelegramConfig) -> Any:
        try:
            from telethon import TelegramClient
        except ImportError as exc:
            raise TelegramDependencyError("Install the telethon package to use real Telegram sessions.") from exc
        if config.api_id is None or not config.api_hash or config.session_path is None:
            raise TelegramAuthError("Telegram configuration is incomplete.")
        return TelegramClient(str(config.session_path), config.api_id, config.api_hash)

    @asynccontextmanager
    async def _connected_client(self, config: TelegramConfig) -> AsyncIterator[Any]:
        client = self._client(config)
        await client.connect()
        try:
            yield client
        finally:
            await client.disconnect()


def parse_message_links(links: list[str]) -> list[dict[str, Any]]:
    refs = []
    for link in links:
        parsed = urlparse(link)
        if parsed.netloc not in {"t.me", "telegram.me"}:
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            continue
        if parts[0] == "c" and len(parts) >= 3:
            chat = int(f"-100{parts[1]}")
            message_id = _int_or_none(parts[2])
        else:
            chat = parts[0]
            message_id = _int_or_none(parts[1])
        if message_id is None:
            continue
        refs.append(
            {
                "chat": chat,
                "message_id": message_id,
                "source_key": f"link:{chat}",
                "source_url": link,
            }
        )
    return refs


def download_entity_selector(download_ref: dict[str, Any]) -> Any:
    selector = download_ref.get("chat_username") or download_ref.get("chat_id")
    if isinstance(selector, str) and selector.lstrip("-").isdigit():
        return int(selector)
    return selector


def source_key_for_chat(selector: Any) -> str:
    if isinstance(selector, dict):
        for key in ("key", "alias", "username", "id", "chat_id"):
            value = selector.get(key)
            if value not in (None, ""):
                return str(value).lstrip("@")
        return "unknown"
    return str(selector).strip().lstrip("@")


def _entity_selector(selector: Any) -> Any:
    if isinstance(selector, dict):
        value = selector.get("alias")
        if value in ("saved", "saved_messages", "me"):
            return "me"
        for key in ("username", "id", "chat_id", "invite_link"):
            if selector.get(key) not in (None, ""):
                return selector[key]
        return selector
    text = str(selector).strip()
    if text in ("saved", "saved_messages", "me"):
        return "me"
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _source_summary(
    source_key: str,
    messages: list[dict[str, Any]],
    *,
    cursor_eligible: bool = True,
) -> dict[str, Any]:
    ids = [_int_or_none(message.get("id")) for message in messages]
    ids = [item for item in ids if item is not None]
    return {
        "source_key": source_key,
        "messages": len(messages),
        "next_message_id": str(max(ids)) if ids else None,
        "cursor_eligible": cursor_eligible,
    }


def _dialog_to_dict(dialog: Any) -> dict[str, Any]:
    entity = getattr(dialog, "entity", dialog)
    chat_id = getattr(entity, "id", None)
    username = getattr(entity, "username", None)
    chat_type = _chat_type(entity, getattr(dialog, "name", None))
    return {
        "id": str(chat_id) if chat_id is not None else None,
        "title": getattr(dialog, "name", None) or getattr(entity, "title", None) or _display_name(entity),
        "type": chat_type,
        "username": username,
        "selector": f"@{username}" if username else str(chat_id) if chat_id is not None else None,
        "access_hash_present": getattr(entity, "access_hash", None) is not None,
    }


def _message_to_dict(message: Any, *, entity: Any) -> dict[str, Any]:
    chat_id = _peer_id(entity, getattr(message, "chat_id", None))
    chat = {
        "id": str(chat_id) if chat_id is not None else None,
        "title": getattr(entity, "title", None) or _display_name(entity),
        "type": _chat_type(entity, None),
        "username": getattr(entity, "username", None),
    }
    media = _media_to_dict(message)
    return {
        "id": getattr(message, "id", None),
        "date": getattr(message, "date", None).isoformat() if getattr(message, "date", None) else None,
        "edited_at": getattr(message, "edit_date", None).isoformat() if getattr(message, "edit_date", None) else None,
        "text": getattr(message, "message", None),
        "caption": getattr(message, "message", None),
        "chat": chat,
        "sender": {"id": getattr(message, "sender_id", None)},
        "grouped_id": str(getattr(message, "grouped_id", "")) if getattr(message, "grouped_id", None) else None,
        "protected_content": bool(getattr(message, "noforwards", False)),
        "media": [media] if media else [],
    }


def _media_to_dict(message: Any) -> dict[str, Any] | None:
    if getattr(message, "photo", None):
        return {
            "id": getattr(message.photo, "id", None),
            "kind": "photo",
            "mime_type": "image/jpeg",
        }
    document = getattr(message, "document", None)
    if not document:
        return None
    mime_type = getattr(document, "mime_type", None)
    attributes = getattr(document, "attributes", []) or []
    file_name = None
    kind = "document"
    for attribute in attributes:
        name = type(attribute).__name__.lower()
        if name.endswith("documentattributevideo"):
            kind = "video"
        elif name.endswith("documentattributeaudio"):
            kind = "audio"
        elif name.endswith("documentattributefilename"):
            file_name = getattr(attribute, "file_name", None)
    return {
        "id": getattr(document, "id", None),
        "kind": kind,
        "mime_type": mime_type,
        "file_name": file_name,
        "size_bytes": getattr(document, "size", None),
    }


def _chat_type(entity: Any, name: str | None) -> str:
    if getattr(entity, "is_self", False) or name == "Saved Messages":
        return "saved_messages"
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if type(entity).__name__.lower().endswith("chat"):
        return "group"
    return "private"


def _peer_id(entity: Any, fallback: Any) -> Any:
    try:
        from telethon.utils import get_peer_id
    except ImportError:
        return getattr(entity, "id", None) or fallback
    try:
        return get_peer_id(entity)
    except Exception:
        return getattr(entity, "id", None) or fallback


def _display_name(entity: Any) -> str | None:
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    title = getattr(entity, "title", None)
    name = " ".join(part for part in (first, last) if part)
    return name or title or getattr(entity, "username", None)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
