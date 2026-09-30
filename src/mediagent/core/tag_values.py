"""Shared normalization rules for the Asset tag list."""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable


MAX_TAG_LENGTH = 80
RESERVED_TAG_PREFIXES = ("source:", "type:")


def normalize_tag(value: str) -> str:
    """Return one stable display tag or reject unsafe input."""

    tag = unicodedata.normalize("NFC", str(value)).strip()
    if not tag:
        raise ValueError("Tags must not be empty.")
    if len(tag) > MAX_TAG_LENGTH:
        raise ValueError(f"Tags must not exceed {MAX_TAG_LENGTH} characters.")
    if any(unicodedata.category(character).startswith("C") for character in tag):
        raise ValueError("Tags must not contain control characters.")
    return tag


def tag_key(tag: str) -> str:
    return unicodedata.normalize("NFC", tag).casefold()


def is_reserved_tag(tag: str) -> bool:
    return tag_key(tag).startswith(RESERVED_TAG_PREFIXES)


def canonicalize_tags(value: Any, *, strict: bool = False) -> list[str]:
    """Normalize and case-insensitively deduplicate an existing tag list.

    Strict mode is used by every write path so malformed legacy metadata is
    reported instead of being silently rewritten. Read-only callers may use
    tolerant mode without changing stored metadata.
    """

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        if strict:
            raise ValueError("Asset tag metadata must be a list of strings.")
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            if strict:
                raise ValueError("Asset tag metadata must contain only strings.")
            continue
        try:
            tag = normalize_tag(item)
        except ValueError:
            if strict:
                raise ValueError("Asset contains invalid existing tag metadata.") from None
            tag = item
        key = tag_key(tag)
        if key not in seen:
            output.append(tag)
            seen.add(key)
    return output


def normalize_tags(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Tags must be provided as a list of strings.")
    return canonicalize_tags([normalize_tag(value) for value in values], strict=True)
