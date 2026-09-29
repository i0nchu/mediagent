"""Normalize Telegram message fixtures/adapters into Mediagent media items."""

from __future__ import annotations

import mimetypes
import re
from typing import Any

from mediagent.core.storage import safe_storage_segment


SUPPORTED_MEDIA_TYPES = {"photo", "video", "audio"}
MEDIA_TYPE_PREFIX = {"photo": "p", "video": "v", "audio": "a"}
TELEGRAM_MESSAGE_LINK_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:t\.me|telegram\.me)/(?:c/\d+/\d+|[A-Za-z0-9_]{3,}/\d+)(?:[/?#][^\s<>()\"']*)?",
    re.IGNORECASE,
)


def normalize_messages(
    messages: list[dict[str, Any]],
    *,
    media_types: list[str] | None = None,
    include_protected: bool = False,
    include_intake_metadata: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    allowed = set(media_types or SUPPORTED_MEDIA_TYPES)
    if include_intake_metadata:
        messages = _with_album_metadata(messages)
    items: list[dict[str, Any]] = []
    summary = {
        "messages_scanned": len(messages),
        "messages_with_media": 0,
        "items": 0,
        "skipped_protected": 0,
        "skipped_unavailable": 0,
        "skipped_unsupported": 0,
    }
    for message in messages:
        message_items, message_summary = normalize_message(
            message,
            media_types=allowed,
            include_protected=include_protected,
            include_intake_metadata=include_intake_metadata,
        )
        for key, value in message_summary.items():
            summary[key] = summary.get(key, 0) + value
        items.extend(message_items)
    summary["items"] = len(items)
    return items, summary


def normalize_message(
    message: dict[str, Any],
    *,
    media_types: set[str],
    include_protected: bool,
    include_intake_metadata: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    counters = {
        "messages_with_media": 0,
        "skipped_protected": 0,
        "skipped_unavailable": 0,
        "skipped_unsupported": 0,
    }
    if message.get("protected_content") and not include_protected:
        counters["skipped_protected"] += 1
        return [], counters
    if message.get("unavailable") or message.get("media_unavailable"):
        counters["skipped_unavailable"] += 1
        return [], counters

    media_entries = _media_entries(message.get("media"))
    if media_entries:
        counters["messages_with_media"] += 1
    items: list[dict[str, Any]] = []
    for index, media in enumerate(media_entries):
        item = normalize_media(
            message,
            media,
            index=index,
            include_intake_metadata=include_intake_metadata,
        )
        if item is None:
            counters["skipped_unsupported"] += 1
            continue
        if item["media_type"] not in media_types:
            counters["skipped_unsupported"] += 1
            continue
        items.append(item)
    return items, counters


def normalize_media(
    message: dict[str, Any],
    media: dict[str, Any],
    *,
    index: int,
    include_intake_metadata: bool = False,
) -> dict[str, Any] | None:
    media_type = media_type_for(media)
    if media_type is None:
        return None
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    chat_id = str(chat.get("id") or message.get("chat_id") or "unknown-chat")
    message_id = str(message.get("id") or message.get("message_id") or "unknown-message")
    media_id = str(media.get("id") or media.get("file_id") or index)
    remote_id = f"{safe_storage_segment(chat_id)}:{safe_storage_segment(message_id)}:{safe_storage_segment(media_id)}"
    source_timestamp = message.get("date") or message.get("timestamp")
    part = media.get("part") or f"{MEDIA_TYPE_PREFIX[media_type]}{index}"
    mime_type = media.get("mime_type") or media.get("content_type")
    extension = media.get("extension") or _extension_from_name_or_mime(media.get("file_name"), mime_type)
    download_ref = _download_ref(message, media, chat_id=chat_id, message_id=message_id, media_id=media_id, index=index)
    telegram_uri = media.get("telegram_uri") or telegram_media_uri(download_ref)
    file_info = {
        "telegram_uri": telegram_uri,
        "kind": media.get("kind") or media_type,
        "page": index,
        "part": part,
        "media_type": media_type,
        "mime_type": mime_type,
        "extension": extension,
        "file_name": media.get("file_name"),
        "size_bytes": media.get("size_bytes") or media.get("size"),
        "source_timestamp": source_timestamp,
        "download_ref": download_ref,
    }
    telegram_metadata: dict[str, Any] = {
        "chat_id": chat_id,
        "chat_title": chat.get("title"),
        "chat_type": chat.get("type"),
        "chat_username": chat.get("username"),
        "message_id": message_id,
        "grouped_id": message.get("grouped_id"),
        "media_id": media_id,
        "protected_content": bool(message.get("protected_content")),
        "edited_at": message.get("edited_at"),
    }
    if include_intake_metadata:
        album = _album_metadata(message, media_index=index)
        if album is not None:
            telegram_metadata["album"] = album
        forward_origin = _forward_origin(message.get("forward"))
        if forward_origin is not None:
            telegram_metadata["forward_origin"] = forward_origin
        if message.get("_inbox_delivery"):
            telegram_metadata["inbox_delivery"] = message["_inbox_delivery"]
    return {
        "platform": "telegram",
        "remote_id": remote_id,
        "media_type": media_type,
        "source_url": message.get("source_url") or _source_url(chat, message_id),
        "author_id": _author_id(sender, chat),
        "author_name": _author_name(sender, chat),
        "metadata": {
            "caption": message.get("caption"),
            "text_present": bool(message.get("text") or message.get("caption")),
            "telegram": telegram_metadata,
            "source_timestamp": source_timestamp,
            "files": [file_info],
        },
    }


def _with_album_metadata(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach deterministic album positions without changing caller-owned fixtures."""

    groups: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for message_index, message in enumerate(messages):
        grouped_id = message.get("grouped_id")
        media_entries = _media_entries(message.get("media"))
        if grouped_id not in (None, "") and media_entries:
            chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
            chat_id = str(chat.get("id") or message.get("chat_id") or "unknown-chat")
            group = groups.setdefault((chat_id, str(grouped_id)), [])
            group.extend((message_index, media_index) for media_index in range(len(media_entries)))
    positions: dict[int, dict[int, tuple[int, int]]] = {}
    for members in groups.values():
        for position, (message_index, media_index) in enumerate(members, start=1):
            positions.setdefault(message_index, {})[media_index] = (position, len(members))
    output: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if index not in positions:
            output.append(message)
            continue
        output.append({**message, "_album_positions": positions[index]})
    return output


def _album_metadata(message: dict[str, Any], *, media_index: int) -> dict[str, Any] | None:
    grouped_id = message.get("grouped_id")
    if grouped_id in (None, ""):
        return None
    position, size = (message.get("_album_positions") or {}).get(media_index, (None, None))
    return {
        "group_id": str(grouped_id),
        "position": position,
        "size": size,
    }


def _forward_origin(value: Any) -> dict[str, Any] | None:
    """Keep only bounded, non-secret forward provenance fields."""

    if not isinstance(value, dict):
        return None
    output: dict[str, Any] = {}
    for key in ("type", "id", "message_id", "date"):
        item = value.get(key)
        if item not in (None, ""):
            output[key] = str(item)[:256]
    for key in ("name", "author"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            output[key] = item.strip()[:256]
    return output or None


def media_type_for(media: dict[str, Any]) -> str | None:
    explicit = media.get("media_type")
    if explicit in SUPPORTED_MEDIA_TYPES:
        return str(explicit)
    kind = str(media.get("kind") or "").lower()
    mime_type = str(media.get("mime_type") or media.get("content_type") or "").lower()
    if kind in ("photo", "image"):
        return "photo"
    if kind in ("video", "animation", "gif"):
        return "video"
    if kind in ("audio", "voice"):
        return "audio"
    if mime_type.startswith("image/"):
        return "photo"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type.startswith("audio/"):
        return "audio"
    return None


def telegram_media_uri(download_ref: dict[str, Any]) -> str:
    chat_id = safe_storage_segment(download_ref.get("chat_id") or download_ref.get("chat") or "unknown-chat")
    message_id = safe_storage_segment(download_ref.get("message_id") or "unknown-message")
    media_id = safe_storage_segment(download_ref.get("media_id") or download_ref.get("media_index") or 0)
    return f"telegram://{chat_id}/{message_id}/{media_id}"


def extract_message_links(messages: list[dict[str, Any]]) -> list[str]:
    return [record["original_url"] for record in extract_message_link_records(messages)]


def extract_message_link_records(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for message in messages:
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        for field in ("text", "caption"):
            value = message.get(field)
            if not isinstance(value, str):
                continue
            for match in TELEGRAM_MESSAGE_LINK_PATTERN.finditer(value):
                link = match.group(0).rstrip(".,;:!?)]}")
                if link not in seen:
                    seen.add(link)
                    records.append(
                        {
                            "ingest_platform": "telegram",
                            "original_url": link,
                            "source_chat_id": str(chat.get("id") or message.get("chat_id") or ""),
                            "source_message_id": str(message.get("id") or message.get("message_id") or ""),
                            "source_message_date": message.get("date") or message.get("timestamp"),
                        }
                    )
    return records


def _media_entries(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _download_ref(
    message: dict[str, Any],
    media: dict[str, Any],
    *,
    chat_id: str,
    message_id: str,
    media_id: str,
    index: int,
) -> dict[str, Any]:
    existing = media.get("download_ref")
    if isinstance(existing, dict):
        return existing
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    return {
        "chat_id": chat_id,
        "chat_username": chat.get("username"),
        "message_id": message_id,
        "media_id": media_id,
        "media_index": index,
    }


def _source_url(chat: dict[str, Any], message_id: str) -> str | None:
    username = chat.get("username")
    if username:
        return f"https://t.me/{str(username).lstrip('@')}/{message_id}"
    return None


def _author_id(sender: dict[str, Any], chat: dict[str, Any]) -> str | None:
    value = sender.get("id") or chat.get("id")
    return str(value) if value is not None else None


def _author_name(sender: dict[str, Any], chat: dict[str, Any]) -> str | None:
    return sender.get("username") or sender.get("name") or chat.get("title") or chat.get("username")


def _extension_from_name_or_mime(file_name: Any, mime_type: Any) -> str:
    if file_name:
        suffix = str(file_name).rsplit(".", 1)
        if len(suffix) == 2 and 1 <= len(suffix[1]) <= 8:
            return "." + suffix[1].lower()
    if mime_type:
        guessed = mimetypes.guess_extension(str(mime_type).split(";", 1)[0].strip().lower())
        if guessed:
            return ".jpg" if guessed in (".jpeg", ".jpe") else guessed
    return ".bin"
