from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent.core import db
from mediagent.core.links import ResolveRequest, default_link_resolver_registry, sanitize_link_resolution_for_output
from mediagent.core.tooling import ToolContext
from mediagent.platforms.pornhub import client as pornhub_client
from mediagent.platforms.pornhub.links import parse_exact_video_link
from mediagent.tools.defaults import create_default_registry


VIDEO_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 80


def _info(viewkey: str = "ph123abc") -> dict:
    return {
        "id": viewkey,
        "extractor_key": "PornHub",
        "title": "Example video",
        "uploader": "Example author",
        "description": "Example description",
        "upload_date": "20260920",
        "duration": 12.5,
        "formats": [{"url": "https://cdn.example/video.mp4", "ext": "mp4"}],
    }


class FakeYtDlp:
    instances: list["FakeYtDlp"] = []
    error: Exception | None = None

    def __init__(self, options: dict) -> None:
        self.options = options
        self.processed = False
        type(self).instances.append(self)

    def __enter__(self) -> "FakeYtDlp":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict:
        if self.error is not None:
            raise self.error
        assert download is False
        return _info()

    def process_ie_result(self, info: dict, *, download: bool) -> dict:
        assert download is True
        self.processed = True
        template = self.options["outtmpl"]["default"]
        target = Path(template.replace("%(ext)s", "mp4"))
        target.write_bytes(VIDEO_BYTES)
        return info


class PornhubAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeYtDlp.instances = []
        FakeYtDlp.error = None

    def test_exact_links_share_one_canonical_identity(self) -> None:
        urls = (
            "https://www.pornhub.com/view_video.php?viewkey=ph123abc&foo=ignored",
            "https://m.pornhub.com/video/show?viewkey=ph123abc",
            "https://pornhub.com/embed/ph123abc/",
        )
        parsed = [parse_exact_video_link(url) for url in urls]

        self.assertTrue(all(item is not None for item in parsed))
        self.assertEqual({item.viewkey for item in parsed if item}, {"ph123abc"})
        self.assertEqual(
            {item.canonical_url for item in parsed if item},
            {"https://www.pornhub.com/view_video.php?viewkey=ph123abc"},
        )

    def test_non_exact_and_unsafe_links_are_not_accepted(self) -> None:
        self.assertIsNone(parse_exact_video_link("https://www.pornhub.com/"))
        self.assertIsNone(parse_exact_video_link("https://www.pornhub.com/playlist/123"))
        self.assertIsNone(parse_exact_video_link("http://www.pornhub.com/view_video.php?viewkey=ph123abc"))
        self.assertIsNone(parse_exact_video_link("https://user:secret@pornhub.com/embed/ph123abc"))

    def test_resolver_uses_dedicated_strategy_and_hides_runtime_context(self) -> None:
        request = ResolveRequest(host_resolver=lambda _host: ["93.184.216.34"])
        with patch(
            "mediagent.platforms.pornhub.client.probe_exact_video",
            return_value={
                "viewkey": "ph123abc",
                "canonical_url": "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                "title": "Example video",
                "author": "Example author",
                "description": None,
                "source_timestamp": "2026-09-20T00:00:00+00:00",
                "duration_seconds": 12.5,
            },
        ):
            resolution = default_link_resolver_registry().resolve(
                "https://pornhub.com/embed/ph123abc",
                request=request,
            )

        self.assertEqual(resolution["status"], "resolved")
        self.assertEqual(resolution["origin_source"], "pornhub")
        self.assertEqual(resolution["remote_id"], "ph123abc")
        self.assertEqual(
            resolution["media_candidates"][0]["download_context"]["strategy"],
            "pornhub_yt_dlp",
        )
        self.assertIsNone(
            sanitize_link_resolution_for_output(resolution)["media_candidates"][0]["download_context"]
        )

    def test_other_pornhub_pages_are_reserved_from_generic_html(self) -> None:
        resolution = default_link_resolver_registry().resolve(
            "https://www.pornhub.com/playlist/123",
            request=ResolveRequest(host_resolver=lambda _host: ["93.184.216.34"]),
        )

        self.assertEqual(resolution["status"], "skipped")
        self.assertEqual(resolution["resolver"], "reserved_platform_page")
        self.assertEqual(resolution["skip_reason"], "pornhub_url_unsupported")

    def test_explicit_retryability_controls_link_queue_classification(self) -> None:
        permanent = {
            "status": "skipped",
            "skip_reason": "pornhub_video_unavailable",
            "details": {"retryable": False},
        }
        transient = {
            "status": "skipped",
            "skip_reason": "pornhub_download_failed",
            "details": {"retryable": True},
        }

        self.assertFalse(
            db.link_resolution_retryable(
                status="skipped",
                resolution=permanent,
                skip_reason=permanent["skip_reason"],
            )
        )
        self.assertTrue(
            db.link_resolution_retryable(
                status="skipped",
                resolution=transient,
                skip_reason=transient["skip_reason"],
            )
        )

    def test_missing_optional_backend_is_retryable_for_inbox_safety(self) -> None:
        error = pornhub_client.PornhubClientError(
            "pornhub_backend_missing",
            "Pornhub exact-video support requires the optional video dependency.",
            category="validation",
        )
        with (
            patch("mediagent.core.links.resolve_host_ips", return_value=["93.184.216.34"]),
            patch("mediagent.platforms.pornhub.client.probe_exact_video", side_effect=error),
        ):
            resolution = default_link_resolver_registry().resolve(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc"
            )

        self.assertTrue(resolution["details"]["user_action_required"])
        self.assertTrue(resolution["details"]["retryable"])
        self.assertTrue(
            db.link_resolution_retryable(
                status=resolution["status"],
                resolution=resolution,
                skip_reason=resolution["skip_reason"],
            )
        )

    def test_materializer_uses_isolated_yt_dlp_options_and_atomic_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "library" / "video.mp4"
            result = pornhub_client.materialize_exact_video(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                target_path=target,
                allowed_write_roots=[root],
                ytdlp_factory=FakeYtDlp,
                url_validator=lambda _url: object(),
            )

            staging = list(target.parent.glob(".mediagent-pornhub-*"))

        self.assertEqual(result.target_path, str(target))
        self.assertEqual(result.mime_type, "video/mp4")
        self.assertEqual(result.source_timestamp, "2026-09-20T00:00:00+00:00")
        self.assertEqual(result.checksum, "sha256:" + hashlib.sha256(VIDEO_BYTES).hexdigest())
        self.assertEqual(staging, [])
        options = FakeYtDlp.instances[0].options
        self.assertTrue(options["ignoreconfig"])
        self.assertTrue(options["noplaylist"])
        self.assertEqual(options["plugin_dirs"], [])
        self.assertNotIn("cookiefile", options)
        self.assertNotIn("cookiesfrombrowser", options)

    def test_missing_backend_is_explicit_and_does_not_create_target(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            with patch("mediagent.platforms.pornhub.client.importlib.import_module", side_effect=ImportError):
                with self.assertRaises(pornhub_client.PornhubClientError) as caught:
                    pornhub_client.materialize_exact_video(
                        "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                        target_path=target,
                        allowed_write_roots=[Path(temp_dir)],
                    )

        self.assertEqual(caught.exception.code, "pornhub_backend_missing")
        self.assertFalse(target.exists())

    def test_rate_limit_and_unsafe_transport_have_stable_errors(self) -> None:
        FakeYtDlp.error = RuntimeError("HTTP Error 429: Too Many Requests at signed URL")
        with self.assertRaises(pornhub_client.PornhubClientError) as limited:
            pornhub_client.probe_exact_video(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                ytdlp_factory=FakeYtDlp,
                url_validator=lambda _url: object(),
            )
        self.assertEqual(limited.exception.code, "pornhub_rate_limited")
        self.assertNotIn("signed URL", str(limited.exception))

        class UnsafeYtDlp(FakeYtDlp):
            def extract_info(self, _url: str, *, download: bool) -> dict:
                info = _info()
                info["formats"] = [{"url": "http://127.0.0.1/private.mp4"}]
                return info

        with self.assertRaises(pornhub_client.PornhubClientError) as unsafe:
            pornhub_client.probe_exact_video(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                ytdlp_factory=UnsafeYtDlp,
            )
        self.assertEqual(unsafe.exception.code, "pornhub_unsafe_media_url")

        FakeYtDlp.error = TimeoutError("signed transport read timed out")
        with self.assertRaises(pornhub_client.PornhubClientError) as timed_out:
            pornhub_client.probe_exact_video(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                ytdlp_factory=FakeYtDlp,
                url_validator=lambda _url: object(),
            )
        self.assertEqual(timed_out.exception.code, "pornhub_timeout")
        self.assertNotIn("signed transport", str(timed_out.exception))

    def test_missing_remote_identity_is_rejected(self) -> None:
        class MissingIdentityYtDlp(FakeYtDlp):
            def extract_info(self, _url: str, *, download: bool) -> dict:
                info = _info()
                info.pop("id")
                return info

        with self.assertRaises(pornhub_client.PornhubClientError) as caught:
            pornhub_client.probe_exact_video(
                "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                ytdlp_factory=MissingIdentityYtDlp,
                url_validator=lambda _url: object(),
            )
        self.assertEqual(caught.exception.code, "pornhub_identity_mismatch")

    def test_fragment_progress_enforces_size_limit_and_cleans_staging(self) -> None:
        class OversizeYtDlp(FakeYtDlp):
            def process_ie_result(self, info: dict, *, download: bool) -> dict:
                template = self.options["outtmpl"]["default"]
                target = Path(template.replace("%(ext)s", "mp4"))
                target.write_bytes(b"x" * (1024 * 1024 + 1))
                for hook in self.options["progress_hooks"]:
                    hook({"status": "downloading", "downloaded_bytes": 512 * 1024})
                return info

        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "library" / "video.mp4"
            with self.assertRaises(pornhub_client.PornhubClientError) as caught:
                pornhub_client.materialize_exact_video(
                    "https://www.pornhub.com/view_video.php?viewkey=ph123abc",
                    target_path=target,
                    allowed_write_roots=[root],
                    max_media_bytes=1024 * 1024,
                    ytdlp_factory=OversizeYtDlp,
                    url_validator=lambda _url: object(),
                )
            staging = list(target.parent.glob(".mediagent-pornhub-*"))

        self.assertEqual(caught.exception.code, "pornhub_media_too_large")
        self.assertFalse(target.exists())
        self.assertEqual(staging, [])

    @unittest.skipUnless(importlib.util.find_spec("yt_dlp"), "optional video dependency is not installed")
    def test_default_factory_disables_user_plugins_before_ytdlp_initialization(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plugin = root / "config" / "yt-dlp" / "plugins" / "evil" / "yt_dlp_plugins" / "extractor"
            plugin.mkdir(parents=True)
            (plugin / "side_effect.py").write_text(
                "import os\nfrom pathlib import Path\nPath(os.environ['MEDIAGENT_PLUGIN_MARKER']).write_text('executed')\n",
                encoding="utf-8",
            )
            marker = root / "plugin-executed"
            script = textwrap.dedent(
                """
                from mediagent.platforms.pornhub.client import _default_ytdlp_factory
                factory = _default_ytdlp_factory(lambda _url: object())
                with factory({'quiet': True, 'skip_download': True}):
                    pass
                """
            )
            env = {
                **os.environ,
                "XDG_CONFIG_HOME": str(root / "config"),
                "MEDIAGENT_PLUGIN_MARKER": str(marker),
            }
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists(), completed.stderr)

    def test_link_sync_downloads_once_and_adopts_asset(self) -> None:
        registry = create_default_registry()
        url = "https://www.pornhub.com/view_video.php?viewkey=ph123abc"
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            library = root / "library"
            data_dir.mkdir()
            library.mkdir()
            db_path = data_dir / "mediagent.sqlite3"
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(db_path),
                },
            )

            def materialize(_url: str, *, target_path: Path, **_kwargs):
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(VIDEO_BYTES)
                return pornhub_client.PornhubMaterializedVideo(
                    viewkey="ph123abc",
                    canonical_url=url,
                    target_path=str(target_path),
                    title="Example video",
                    author="Example author",
                    description=None,
                    source_timestamp="2026-09-20T00:00:00+00:00",
                    duration_seconds=12.5,
                    mime_type="video/mp4",
                    extension=".mp4",
                    checksum="sha256:" + hashlib.sha256(VIDEO_BYTES).hexdigest(),
                    size_bytes=len(VIDEO_BYTES),
                )

            probe = {
                "viewkey": "ph123abc",
                "canonical_url": url,
                "title": "Example video",
                "author": "Example author",
                "description": None,
                "source_timestamp": "2026-09-20T00:00:00+00:00",
                "duration_seconds": 12.5,
            }
            with (
                patch("mediagent.core.links.resolve_host_ips", return_value=["93.184.216.34"]),
                patch("mediagent.platforms.pornhub.client.probe_exact_video", return_value=probe),
                patch("mediagent.tools.link_tools.pornhub_client.materialize_exact_video", side_effect=materialize) as download,
            ):
                first = asyncio.run(registry.run("link.media.sync", {"url": url}, context))
                second = asyncio.run(registry.run("link.media.sync", {"url": url}, context))

            files = db.list_media_files(db_path, platform="pornhub", remote_id="ph123abc")

        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(first.data["summary"]["downloaded"], 1)
        self.assertEqual(len(first.data["asset_ids"]), 1)
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(second.data["summary"]["skipped_items"], 1)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["file_key"], "video:ph123abc")

    def test_missing_backend_makes_link_sync_fail_instead_of_succeeding_skip(self) -> None:
        registry = create_default_registry()
        url = "https://www.pornhub.com/view_video.php?viewkey=ph123abc"
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_dir = root / "data"
            library = root / "library"
            data_dir.mkdir()
            library.mkdir()
            context = ToolContext.from_env(
                cwd=root,
                env={
                    "MEDIAGENT_DATA_DIR": str(data_dir),
                    "MEDIAGENT_LIBRARY_DIR": str(library),
                    "MEDIAGENT_DB_PATH": str(data_dir / "mediagent.sqlite3"),
                },
            )
            error = pornhub_client.PornhubClientError(
                "pornhub_backend_missing",
                "Pornhub exact-video support requires the optional video dependency.",
                category="validation",
            )
            with (
                patch("mediagent.core.links.resolve_host_ips", return_value=["93.184.216.34"]),
                patch("mediagent.platforms.pornhub.client.probe_exact_video", side_effect=error),
            ):
                result = asyncio.run(registry.run("link.media.sync", {"url": url}, context))
                preview_context = ToolContext.from_env(
                    cwd=root,
                    env=context.env,
                    dry_run=True,
                )
                preview = asyncio.run(
                    registry.run("link.media.sync", {"url": url}, preview_context)
                )

        self.assertFalse(result.is_success)
        self.assertEqual(result.error.code, "pornhub_backend_missing")
        self.assertEqual(result.error.category.value, "validation")
        self.assertEqual(result.data["summary"]["failed"], 1)
        self.assertFalse(preview.is_success)
        self.assertEqual(preview.error.code, "pornhub_backend_missing")


if __name__ == "__main__":
    unittest.main()
