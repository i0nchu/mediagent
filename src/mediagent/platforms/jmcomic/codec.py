"""JMComic mobile-API signing and encrypted-envelope decoding."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any


APP_VERSION = "2.0.30"
TOKEN_SECRET = "185Hcomic3PAPP7R"
CONTENT_TOKEN_SECRET = "18comicAPPContent"
DATA_SECRET = "185Hcomic3PAPP7R"


class JMComicCodecError(ValueError):
    """Raised for malformed or undecodable provider envelopes."""


def api_headers(timestamp: int, *, content_endpoint: bool = False, version: str = APP_VERSION) -> dict[str, str]:
    secret = CONTENT_TOKEN_SECRET if content_endpoint else TOKEN_SECRET
    return {
        "token": hashlib.md5(f"{timestamp}{secret}".encode()).hexdigest(),
        "tokenparam": f"{timestamp},{version}",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 9; mediagent) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Mobile Safari/537.36"
        ),
        "Accept-Encoding": "gzip, deflate",
    }


def decode_api_envelope(
    content: bytes | str,
    *,
    timestamp: int,
    decrypt: Callable[[bytes, bytes], bytes] | None = None,
) -> Any:
    try:
        outer = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JMComicCodecError("JMComic API returned malformed JSON.") from exc
    if not isinstance(outer, dict):
        raise JMComicCodecError("JMComic API envelope must be an object.")
    code = outer.get("code")
    if code not in (None, 200, "200"):
        raise JMComicCodecError(f"JMComic API returned code {code}.")
    data = outer.get("data", outer)
    if isinstance(data, (dict, list)):
        return data
    if data in (None, ""):
        return data
    if not isinstance(data, str):
        raise JMComicCodecError("JMComic API data has an unsupported type.")
    try:
        encrypted = base64.b64decode(data, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise JMComicCodecError("JMComic API encrypted data is not valid base64.") from exc
    key = hashlib.md5(f"{timestamp}{DATA_SECRET}".encode()).hexdigest().encode("ascii")
    clear = (decrypt or _aes_ecb_decrypt)(encrypted, key)
    clear = _remove_pkcs7(clear)
    try:
        return json.loads(clear.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JMComicCodecError("JMComic API decrypted data is malformed JSON.") from exc


def _aes_ecb_decrypt(data: bytes, key: bytes) -> bytes:
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:  # pragma: no cover - depends on optional live dependency
        raise JMComicCodecError(
            "JMComic encrypted API responses require the optional pycryptodome dependency."
        ) from exc
    return AES.new(key, AES.MODE_ECB).decrypt(data)


def _remove_pkcs7(data: bytes) -> bytes:
    if not data:
        raise JMComicCodecError("JMComic API decrypted data is empty.")
    padding = data[-1]
    if padding < 1 or padding > 16 or data[-padding:] != bytes([padding]) * padding:
        raise JMComicCodecError("JMComic API decrypted data has invalid padding.")
    return data[:-padding]
