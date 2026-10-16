import asyncio
import json
import os
import sqlite3
import unittest
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent import cli
from mediagent.core import db, link_delivery_jobs
from mediagent.core.http import HttpResponse
from mediagent.tools import telegram_tools
from mediagent.tools.defaults import create_default_registry
from mediagent.platforms.instagram.auth import instagram_error_details
from test_telegram_tools import FakeTelegramClient, _telegram_context, _telegram_messages_fixture


class DeliveryFake(FakeTelegramClient):
    def __init__(self, *, mode="timeout", native=False):
        message = _telegram_messages_fixture()[0] if native else {
            "id": 10, "date": "2026-10-16T00:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"}, "media": [],
        }
        message["text"] = "https://1.1.1.1/external.jpg"
        super().__init__(messages={"saved_messages": [message]},
                         downloads={"saved_messages:10:photo-10": b"native image"})
        self.mode = mode
        self.head_calls = 0
        self.get_calls = 0

    def head(self, url, **kwargs):
        self.head_calls += 1
        if self.mode == "timeout":
            raise TimeoutError("synthetic private diagnostic must not be persisted")
        if self.mode in {"404", "auth"}:
            return HttpResponse(404 if self.mode == "404" else 401,
                                {"content-type": "image/jpeg"}, b"", url)
        return HttpResponse(200, {"content-type": "image/jpeg", "content-length": "8"}, b"", url)

    def get_limited(self, url, **kwargs):
        self.get_calls += 1
        if self.mode == "download_failed":
            raise TimeoutError("synthetic download failure")
        return HttpResponse(200, {"content-type": "image/jpeg"}, b"external", url)


def run_sync(context, **options):
    return asyncio.run(create_default_registry().run(
        "telegram.inbox.sync", {"chat": "saved_messages", "attempts": 1, **options}, context,
    ))


def due(database):
    with db.connect(database) as connection:
        connection.execute("UPDATE link_delivery_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00', lease_expires_at='2000-01-01T00:00:00+00:00'")


class TelegramDeliveryTests(unittest.TestCase):
    def test_instagram_unavailable_is_not_assumed_deleted_and_auth_is_not_hot_retried(self):
        for code, retryable in (("instagram_media_unavailable", True), ("instagram_resolve_failed", True),
                                ("instagram_rate_limited", True), ("instagram_media_not_found", False),
                                ("instagram_login_required", False), ("instagram_checkpoint_required", False)):
            with self.subTest(code=code):
                self.assertEqual(instagram_error_details(code)["retryable"], retryable)

    def test_real_cli_returns_zero_for_retryable_terminal_and_auth_backlog(self):
        for mode, status in (("timeout", "deferred"), ("404", "failed"), ("auth", "blocked")):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                fake = DeliveryFake(mode=mode, native=True)
                context, _, database = _telegram_context(directory, fake, env_overrides={
                    "MEDIAGENT_TELEGRAM_INBOX_CHAT": "saved_messages",
                })
                output = StringIO()
                with patch.dict(os.environ, {**context.env, "MEDIAGENT_ENV_FILE": ""}, clear=True), \
                     patch.object(cli.ToolContext, "from_env", return_value=context), \
                     redirect_stdout(output), redirect_stderr(StringIO()):
                    exit_code = cli.run(["sync", "telegram", "--summary-json"])
                payload = json.loads(output.getvalue())
                self.assertEqual(exit_code, 0, payload)
                self.assertTrue(payload["data"]["summary"]["intake_complete"])
                self.assertEqual(payload["data"]["summary"]["link_backlog"][status], 1)
                self.assertEqual(db.get_sync_cursor(database, platform="telegram", cursor_name="inbox:saved_messages")["cursor_value"], "10")

    def test_backoff_is_obeyed_then_backlog_drains_without_new_telegram_messages(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(native=True)
            context, _, database = _telegram_context(directory, fake)
            self.assertTrue(run_sync(context).is_success)
            for _ in range(4):
                self.assertTrue(run_sync(context, full_sync=True).is_success)
            self.assertEqual(fake.head_calls, 1)
            due(database)
            fake.mode = "success"
            third = run_sync(context)
            self.assertTrue(third.is_success, third.to_dict())
            self.assertEqual(third.data["summary"]["external_links"], 0)
            self.assertEqual(third.data["summary"]["links_succeeded"], 1)
            self.assertEqual(third.data["summary"]["link_downloaded"], 1)
            self.assertEqual(fake.head_calls, 2)
            self.assertEqual(sum(name == "media_download" for name, _ in fake.calls), 1)

    def test_resolution_success_does_not_lose_failed_download_obligation(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="download_failed")
            context, _, database = _telegram_context(directory, fake)
            first = run_sync(context)
            self.assertTrue(first.is_success)
            self.assertEqual(db.list_links(database)[0]["status"], "resolved")
            self.assertEqual(link_delivery_jobs.summary(database)["deferred"], 1)
            due(database)
            fake.mode = "success"
            second = run_sync(context)
            self.assertTrue(second.is_success)
            self.assertEqual(second.data["summary"]["link_downloaded"], 1)
            self.assertEqual(link_delivery_jobs.summary(database)["succeeded"], 1)

    def test_completed_delivery_merges_later_provenance_without_any_network(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="success")
            context, _, database = _telegram_context(directory, fake)
            self.assertTrue(run_sync(context).is_success)
            second_message = {**fake.messages["saved_messages"][0], "id": 11}
            fake.messages["saved_messages"].append(second_message)
            self.assertTrue(run_sync(context).is_success)
            self.assertEqual((fake.head_calls, fake.get_calls), (1, 1))
            with db.connect(database) as connection:
                rows = connection.execute("SELECT metadata_json FROM media_items WHERE platform != 'telegram'").fetchall()
                self.assertEqual(len(rows), 1)
                sources = json.loads(rows[0]["metadata_json"])["source_provenance"]
            self.assertEqual({source["message_id"] for source in sources}, {"10", "11"})

    def test_process_cancel_after_intake_cursor_is_recovered_from_expired_delivery_lease(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="success", native=True)
            context, _, database = _telegram_context(directory, fake)
            with patch.object(telegram_tools, "inbox_sync_links", side_effect=asyncio.CancelledError()):
                with self.assertRaises(asyncio.CancelledError):
                    run_sync(context)
            self.assertEqual(db.get_sync_cursor(database, platform="telegram", cursor_name="inbox:saved_messages")["cursor_value"], "10")
            self.assertEqual(link_delivery_jobs.summary(database)["running"], 1)
            due(database)
            recovered = run_sync(context)
            self.assertTrue(recovered.is_success)
            self.assertEqual(link_delivery_jobs.summary(database)["succeeded"], 1)
            self.assertEqual(sum(name == "media_download" for name, _ in fake.calls), 1)

    def test_enqueue_or_checkpoint_failure_returns_nonzero_and_preserves_replay(self):
        for name in ("enqueue", "set_sync_cursor"):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                fake = DeliveryFake(mode="success")
                context, _, database = _telegram_context(directory, fake)
                target = link_delivery_jobs if name == "enqueue" else db
                with patch.object(target, name, side_effect=sqlite3.OperationalError("synthetic database failure")):
                    result = run_sync(context)
                self.assertFalse(result.is_success)
                self.assertIsNone(db.get_sync_cursor(database, platform="telegram", cursor_name="inbox:saved_messages"))
                self.assertTrue(run_sync(context).is_success)
                self.assertEqual(fake.get_calls, 1)

    def test_intake_can_enqueue_without_delivery_and_invalid_budget_does_not_collect(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="success")
            context, _, database = _telegram_context(directory, fake)
            result = run_sync(context, link_batch_size=0)
            self.assertTrue(result.is_success)
            self.assertEqual(result.data["summary"]["links_pending"], 1)
            self.assertEqual(fake.head_calls, 0)
            old_calls = len(fake.calls)
            for options in ({"link_batch_size": -1}, {"link_batch_size": 101}, {"link_time_budget_seconds": 0}):
                self.assertFalse(run_sync(context, **options).is_success)
            self.assertEqual(len(fake.calls), old_calls)
            self.assertTrue(run_sync(context).is_success)
            self.assertEqual(fake.head_calls, 1)

    def test_backlog_and_explicit_retry_cli_use_env_and_do_not_download(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="404")
            context, _, database = _telegram_context(directory, fake)
            run_sync(context)
            for arguments in (["link", "backlog", "--json"], ["link", "retry", "1", "--dry-run", "--json"],
                              ["link", "retry", "1", "--json"]):
                with patch.dict(os.environ, {**context.env, "MEDIAGENT_ENV_FILE": ""}, clear=True), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    self.assertEqual(cli.run(arguments), 0)
            self.assertEqual(fake.head_calls, 1)
            self.assertEqual(link_delivery_jobs.summary(database)["pending"], 1)

    def test_repeat_add_after_telegram_delivery_returns_existing_asset_and_merges_source(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="success")
            context, _, database = _telegram_context(directory, fake)
            first = run_sync(context)
            result = asyncio.run(create_default_registry().run("media.add", {
                "input": "https://1.1.1.1/external.jpg",
            }, context))
            self.assertTrue(result.is_success, result.to_dict())
            self.assertEqual(result.data["asset_ids"], first.data["asset_ids"])
            self.assertEqual((fake.head_calls, fake.get_calls), (1, 1))
            self.assertEqual(len(db.list_links(database)[0]["source_provenance"]), 2)

    def test_explicit_add_repairs_missing_completed_delivery_using_same_asset(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="success")
            context, _, database = _telegram_context(directory, fake)
            first = run_sync(context)
            with db.connect(database) as connection:
                path = Path(connection.execute("SELECT local_path FROM media_files WHERE status='downloaded'").fetchone()[0])
            path.unlink()
            result = asyncio.run(create_default_registry().run("media.add", {
                "input": "https://1.1.1.1/external.jpg", "repair": True,
            }, context))
            self.assertTrue(result.is_success, result.to_dict())
            self.assertTrue(path.is_file())
            self.assertEqual(result.data["asset_ids"], first.data["asset_ids"])
            self.assertEqual((fake.head_calls, fake.get_calls), (2, 2))
            self.assertEqual(link_delivery_jobs.summary(database)["succeeded"], 1)

    def test_legacy_link_only_failure_records_actual_delivery_status_once(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake(mode="download_failed")
            context, _, database = _telegram_context(directory, fake)
            result = asyncio.run(create_default_registry().run("telegram.inbox.sync_links", {
                "chat": "saved_messages", "attempts": 1,
            }, context))
            self.assertFalse(result.is_success)
            with db.connect(database) as connection:
                rows = connection.execute("SELECT status,summary_json FROM runs WHERE name='telegram.inbox.sync_links'").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "partial")
            self.assertEqual(json.loads(rows[0]["summary_json"])["failed"], 1)
            self.assertIsNone(db.get_sync_cursor(database, platform="telegram", cursor_name="links:saved_messages"))

    def test_explicit_legacy_url_does_not_bypass_managed_delivery_backoff(self):
        with TemporaryDirectory() as directory:
            fake = DeliveryFake()
            context, _, database = _telegram_context(directory, fake)
            run_sync(context)
            result = asyncio.run(create_default_registry().run("link.media.sync", {
                "url": "https://1.1.1.1/external.jpg",
            }, context))
            self.assertTrue(result.is_success)
            self.assertEqual(result.data["summary"]["delivery_managed"], 1)
            self.assertEqual(fake.head_calls, 1)
            self.assertEqual(link_delivery_jobs.list_jobs(database)[0]["attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
