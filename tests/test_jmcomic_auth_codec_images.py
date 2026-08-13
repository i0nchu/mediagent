from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from mediagent.platforms.jmcomic.auth import JMComicSession, load_config, load_session, save_session
from mediagent.platforms.jmcomic.codec import api_headers, decode_api_envelope
from mediagent.platforms.jmcomic.images import (
    materialize_page_content,
    restore_vertical_slices,
    scramble_segment_count,
)


class JMComicAuthTests(unittest.TestCase):
    def test_shell_friendly_jmcomic_aliases_are_supported(self) -> None:
        config = load_config(
            env={
                "JMCOMIC_USERNAME": "alias-account",
                "JMCOMIC_PASSWORD": "alias-secret",
                "JMCOMIC_SESSION_FILE": "/tmp/jmcomic-alias.json",
            },
            cwd=Path.cwd(),
        )
        self.assertEqual(config.username, "alias-account")
        self.assertEqual(config.password, "alias-secret")
        self.assertEqual(config.session_file, "/tmp/jmcomic-alias.json")

    def test_valid_env_names_take_precedence_over_compatibility_names(self) -> None:
        config = load_config(
            env={
                "MEDIAGENT_JMCOMIC_USERNAME": "preferred",
                "MEDIAGENT_JMCOMIC_PASSWORD": "preferred-secret",
                "18COMIC_USERNAME": "compat",
                "18COMIC_PASSWORD": "compat-secret",
            },
            cwd=Path.cwd(),
        )
        self.assertEqual(config.username, "preferred")
        self.assertEqual(config.password, "preferred-secret")

    def test_session_cookie_jar_is_atomic_reusable_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            env = {"MEDIAGENT_JMCOMIC_SESSION_FILE": str(path)}
            save_session(
                JMComicSession({"AVS": "opaque", "session": "saved"}, username="account"),
                env=env,
                cwd=Path(directory),
            )
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            loaded = load_session(env=env, cwd=Path(directory))
            self.assertEqual(loaded.cookies["AVS"], "opaque")
            self.assertEqual(loaded.username, "account")
            self.assertNotIn("password", path.read_text(encoding="utf-8").lower())


class JMComicCodecTests(unittest.TestCase):
    def test_request_headers_are_deterministic_and_endpoint_specific(self) -> None:
        regular = api_headers(1700000000)
        content = api_headers(1700000000, content_endpoint=True)
        self.assertEqual(regular["tokenparam"], "1700000000,2.0.30")
        self.assertNotEqual(regular["token"], content["token"])

    def test_plain_and_encrypted_envelopes_are_decoded_behind_one_boundary(self) -> None:
        self.assertEqual(
            decode_api_envelope('{"code": 200, "data": {"id": "1"}}', timestamp=1),
            {"id": "1"},
        )
        clear = json.dumps({"id": "2"}).encode()
        padding = 16 - len(clear) % 16
        padded = clear + bytes([padding]) * padding
        envelope = json.dumps({"code": 200, "data": base64.b64encode(padded).decode()})
        self.assertEqual(
            decode_api_envelope(envelope, timestamp=1, decrypt=lambda data, _key: data),
            {"id": "2"},
        )


class JMComicImageTests(unittest.TestCase):
    def test_scramble_segment_count_matches_threshold_behavior(self) -> None:
        self.assertEqual(scramble_segment_count(scramble_id=999999, photo_id=100, filename="1.jpg"), 0)
        self.assertEqual(scramble_segment_count(scramble_id=1, photo_id=250000, filename="1.jpg"), 10)
        self.assertEqual(scramble_segment_count(scramble_id=1, photo_id=500000, filename="1.jpg") % 2, 0)

    def test_vertical_slice_restore_reverses_provider_slice_order(self) -> None:
        source = Image.new("RGB", (2, 4))
        # Provider order is bottom half first, top half second.
        for y, color in enumerate([(0, 0, 255), (0, 0, 255), (255, 0, 0), (255, 0, 0)]):
            for x in range(2):
                source.putpixel((x, y), color)
        encoded = BytesIO()
        source.save(encoded, format="PNG")
        restored = Image.open(BytesIO(restore_vertical_slices(encoded.getvalue(), segment_count=2)))
        self.assertEqual(restored.getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(restored.getpixel((0, 3)), (0, 0, 255))
        materialized = materialize_page_content(
            encoded.getvalue(),
            {"provider": "jmcomic", "vertical_segments": 2},
        )
        self.assertEqual(Image.open(BytesIO(materialized)).getpixel((0, 0)), (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
