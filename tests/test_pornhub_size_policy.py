from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from contextlib import chdir, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mediagent import cli
from mediagent.core import db, link_delivery_jobs
from mediagent.core.tooling import ErrorCategory, ToolContext, ToolResult
from mediagent.platforms.pornhub import client as pc
from mediagent.tools import telegram_tools
from mediagent.tools.defaults import create_default_registry
from test_pornhub_adapter import FakeYtDlp, VIDEO_BYTES, _info
from test_telegram_delivery import DeliveryFake, run_sync
from test_telegram_tools import _telegram_context

MIB = 1024 * 1024
GIB = 1024 * MIB
URL = 'https://www.pornhub.com/view_video.php?viewkey=ph123abc'


class PornhubSizePolicyTests(unittest.TestCase):
    def setUp(self):
        FakeYtDlp.instances = []
        FakeYtDlp.error = None

    def materialize(self, root, factory, limit=GIB):
        return pc.materialize_exact_video(
            URL, target_path=root / 'video.mp4', allowed_write_roots=[root],
            ytdlp_factory=factory, url_validator=lambda _url: object(),
            max_media_bytes=limit,
        )

    def test_configuration_default_environment_and_explicit_precedence(self):
        self.assertEqual(pc.DEFAULT_MAX_MEDIA_BYTES, 8 * GIB)
        self.assertEqual(pc.configured_media_limit({}), 8 * GIB)
        self.assertEqual(pc.configured_media_limit({pc.MAX_MEDIA_BYTES_ENV: ''}), 8 * GIB)
        self.assertEqual(pc.configured_media_limit({pc.MAX_MEDIA_BYTES_ENV: str(16 * GIB)}), 16 * GIB)
        self.assertEqual(pc.configured_media_limit({pc.MAX_MEDIA_BYTES_ENV: 'invalid'}, GIB), GIB)
        for invalid in ('no-limit', '0', str(MIB - 1), str(64 * GIB + 1), '1.5', '９９９９９９９', True):
            with self.subTest(invalid=invalid), self.assertRaises(pc.PornhubClientError) as caught:
                pc.configured_media_limit({pc.MAX_MEDIA_BYTES_ENV: invalid})
            self.assertEqual(caught.exception.code, 'pornhub_size_limit_invalid')
            self.assertFalse(caught.exception.details['retryable'])

    def test_known_selected_size_is_rejected_before_transfer_and_keeps_existing_target(self):
        class KnownLarge(FakeYtDlp):
            def extract_info(self, _url, *, download):
                return {**_info(), 'filesize': 1424035926}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'video.mp4'
            target.write_bytes(b'prior valid content')
            with self.assertRaises(pc.PornhubClientError) as caught:
                self.materialize(root, KnownLarge)
            self.assertEqual(target.read_bytes(), b'prior valid content')
            self.assertFalse(FakeYtDlp.instances[0].processed)
            self.assertEqual(list(root.glob('.mediagent-pornhub-*')), [])
        self.assertEqual(caught.exception.code, 'pornhub_media_too_large')
        self.assertEqual(caught.exception.category, 'validation')
        self.assertEqual(caught.exception.details, {'size_bytes': 1424035926, 'max_media_bytes': GIB, 'retryable': False})

    def test_selected_audio_and_video_sizes_are_combined(self):
        class Combined(FakeYtDlp):
            def extract_info(self, _url, *, download):
                return {**_info(), 'requested_formats': [{'filesize': 700 * MIB}, {'filesize': 700 * MIB}]}
        with TemporaryDirectory() as directory:
            with self.assertRaises(pc.PornhubClientError) as caught:
                self.materialize(Path(directory), Combined)
        self.assertEqual(caught.exception.details['size_bytes'], 1400 * MIB)

    def test_unselected_formats_and_size_estimates_do_not_reject_small_selected_video(self):
        class Estimates(FakeYtDlp):
            def extract_info(self, _url, *, download):
                info = _info()
                info['formats'][0]['filesize'] = 64 * GIB
                info['filesize_approx'] = 64 * GIB
                return info
        with TemporaryDirectory() as directory:
            result = self.materialize(Path(directory), Estimates)
        self.assertEqual(result.size_bytes, len(VIDEO_BYTES))

    def test_new_default_allows_previously_over_one_gib_known_format(self):
        class KnownLarge(FakeYtDlp):
            def extract_info(self, _url, *, download):
                return {**_info(), 'filesize': 1424035926}
        with TemporaryDirectory() as directory:
            result = self.materialize(Path(directory), KnownLarge, pc.DEFAULT_MAX_MEDIA_BYTES)
        self.assertEqual(result.mime_type, 'video/mp4')
        self.assertEqual(FakeYtDlp.instances[0].options['max_filesize'], 8 * GIB)

    @unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'optional video dependency is not installed')
    def test_real_ytdlp_content_length_skip_keeps_size_error_without_reading_large_body(self):
        from yt_dlp.networking import Response
        reads = []
        bodies = []
        base = pc._default_ytdlp_factory(lambda _url: object())

        class Body(io.BytesIO):
            def read(self, *args):
                reads.append(True)
                return super().read(*args)

        class LargeResponse(base):
            def extract_info(self, url, *, download):
                return {'id': 'ph123abc', 'title': 'fixture', 'extractor_key': 'PornHub',
                        'extractor': 'pornhub', 'webpage_url': url, 'url': 'https://1.1.1.1/fixture.mp4',
                        'ext': 'mp4', 'protocol': 'https', 'format_id': 'fixture', 'duration': 1}

            def urlopen(self, request):
                body = Body(b'')
                bodies.append(body)
                return Response(body, 'https://1.1.1.1/fixture.mp4',
                                {'Content-Length': '1424035926', 'Content-Type': 'video/mp4'}, status=200)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(pc.PornhubClientError) as caught:
                self.materialize(root, LargeResponse)
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(caught.exception.code, 'pornhub_media_too_large')
        self.assertFalse(caught.exception.details['retryable'])
        self.assertEqual(reads, [])
        self.assertTrue(bodies[0].closed)

    def test_unknown_size_fragment_guard_survives_backend_exception_wrapping(self):
        class Wrapped(FakeYtDlp):
            def process_ie_result(self, info, *, download):
                try:
                    for hook in self.options['progress_hooks']:
                        hook({'downloaded_bytes': MIB + 1, 'status': 'downloading'})
                except Exception:
                    raise RuntimeError('backend wrapper with private signed URL') from None
                return info
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(pc.PornhubClientError) as caught:
                self.materialize(root, Wrapped, MIB)
            self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(caught.exception.code, 'pornhub_media_too_large')
        self.assertEqual(caught.exception.category, 'validation')
        self.assertNotIn('signed URL', str(caught.exception))

    def test_genuine_empty_output_retains_invalid_output_network_error(self):
        class Empty(FakeYtDlp):
            def process_ie_result(self, info, *, download):
                return info
        with TemporaryDirectory() as directory:
            with self.assertRaises(pc.PornhubClientError) as caught:
                self.materialize(Path(directory), Empty)
        self.assertEqual(caught.exception.code, 'pornhub_download_invalid')
        self.assertEqual(caught.exception.category, 'network')

    def test_add_and_link_sync_use_environment_with_legacy_explicit_override(self):
        registry = create_default_registry()
        for tool, payload, expected in (
            ('media.add', {'input': URL}, 16 * GIB),
            ('link.media.sync', {'url': URL}, 16 * GIB),
            ('link.media.sync', {'url': URL, 'max_media_bytes': GIB}, GIB),
        ):
            with self.subTest(tool=tool, expected=expected), TemporaryDirectory() as directory:
                root = Path(directory)
                context = ToolContext.from_env(cwd=root, env={
                    'MEDIAGENT_DATA_DIR': str(root), 'MEDIAGENT_DB_PATH': str(root / 'db.sqlite3'),
                    pc.MAX_MEDIA_BYTES_ENV: str(16 * GIB),
                })
                with patch('mediagent.core.links.resolve_host_ips', return_value=['93.184.216.34']), \
                     patch.object(pc, 'probe_exact_video', return_value={}), \
                     patch.object(pc, 'materialize_exact_video', side_effect=pc._size_limit_error(expected, 32 * GIB)) as download:
                    result = asyncio.run(registry.run(tool, payload, context))
                self.assertFalse(result.is_success)
                self.assertEqual(download.call_args.kwargs['max_media_bytes'], expected)
                self.assertEqual(result.error.code, 'pornhub_media_too_large')
                self.assertEqual(result.error.category, ErrorCategory.VALIDATION)
                self.assertEqual(result.error.details['max_media_bytes'], expected)
                self.assertFalse(result.error.details['retryable'])

    def test_real_cli_loads_limit_from_env_file_with_process_env_precedence(self):
        for override in (None, str(2 * GIB)):
            with self.subTest(override=override), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / '.env').write_text(
                    f'MEDIAGENT_DATA_DIR={root / "data"}\n'
                    f'MEDIAGENT_DB_PATH={root / "db.sqlite3"}\n'
                    f'{pc.MAX_MEDIA_BYTES_ENV}={16 * GIB}\n'
                )
                env = {pc.MAX_MEDIA_BYTES_ENV: override} if override else {}
                output = io.StringIO()
                with patch.dict(os.environ, env, clear=True), chdir(root), \
                     redirect_stdout(output), redirect_stderr(io.StringIO()), \
                     patch('mediagent.core.links.resolve_host_ips', return_value=['93.184.216.34']), \
                     patch.object(pc, '_default_ytdlp_factory', return_value=FakeYtDlp):
                    code = cli.run(['add', URL, '--json'])
                self.assertEqual(code, 0, output.getvalue())
                self.assertEqual(json.loads(output.getvalue())['status'], 'success')
                self.assertEqual(FakeYtDlp.instances[-1].options['max_filesize'], int(override) if override else 16 * GIB)

    def test_error_policy_never_copies_transport_context(self):
        from mediagent.tools import link_tools
        result = ToolResult.failure('fixture', 'fixture', details={
            'retryable': False, 'size_bytes': 2 * GIB, 'max_media_bytes': GIB,
            'headers': {'Authorization': 'private'}, 'url': 'https://private.example/signed',
        })
        self.assertEqual(link_tools._download_failure_policy(result), {
            'retryable': False, 'details': {'size_bytes': 2 * GIB, 'max_media_bytes': GIB},
        })

    def test_telegram_stops_policy_retries_then_explicit_retry_uses_raised_limit(self):
        fake = DeliveryFake(mode='success', native=False)
        fake.messages['saved_messages'][0]['text'] = URL
        registry = create_default_registry()
        with TemporaryDirectory() as directory:
            context, _, database = _telegram_context(directory, fake, env_overrides={pc.MAX_MEDIA_BYTES_ENV: str(GIB)})
            with patch('mediagent.core.links.resolve_host_ips', return_value=['93.184.216.34']), \
                 patch.object(pc, 'probe_exact_video', return_value={}) as probe, \
                 patch.object(pc, 'materialize_exact_video', side_effect=pc._size_limit_error(GIB, 1424035926)) as download:
                first = run_sync(context)
                second = run_sync(context)
            self.assertTrue(first.is_success)
            self.assertTrue(second.is_success)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(link_delivery_jobs.summary(database)['failed'], 1)
            job = link_delivery_jobs.list_jobs(database)[0]
            self.assertEqual(job['last_error_code'], 'pornhub_media_too_large')
            self.assertEqual(job['attempt_count'], 1)
            with db.connect(database) as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM media_files WHERE status="downloaded"').fetchone()[0], 0)
            context.env = {**context.env, pc.MAX_MEDIA_BYTES_ENV: str(16 * GIB)}
            retried = asyncio.run(registry.run('link.retry', {'link_ids': [job['link_id']]}, context))
            self.assertTrue(retried.is_success)
            with patch('mediagent.core.links.resolve_host_ips', return_value=['93.184.216.34']), \
                 patch.object(pc, 'probe_exact_video', return_value={}), \
                 patch.object(pc, '_default_ytdlp_factory', return_value=FakeYtDlp):
                third = run_sync(context)
            self.assertTrue(third.is_success, third.to_dict())
            self.assertEqual(FakeYtDlp.instances[-1].options['max_filesize'], 16 * GIB)
            self.assertEqual(link_delivery_jobs.summary(database)['succeeded'], 1)
            self.assertEqual(link_delivery_jobs.summary(database)['failed'], 0)

    def test_mixed_retryable_and_terminal_failures_keep_bounded_retry(self):
        result = ToolResult.failure('link_media_sync_partial', 'fixture', data={
            'links': [{'resolution': {'status': 'resolved'}}],
            'items': [{'errors': [{'code': 'policy', 'retryable': False}, {'code': 'timeout'}]}],
        })
        self.assertEqual(telegram_tools._link_delivery_outcome(result)[0], 'retry')


if __name__ == '__main__':
    unittest.main()
