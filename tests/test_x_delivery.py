"""X exact-post intake through the durable Telegram delivery pipeline."""

import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent import cli
from mediagent.core import db, link_delivery_jobs
from mediagent.core.http import HttpResponse
from mediagent.tools.defaults import create_default_registry
from tests.test_telegram_tools import FakeTelegramClient, _telegram_context
from tests.test_x_add import (
    API_URL, PHOTO_URL, POST_ID, POST_URL, VIDEO_BYTES, VIDEO_URL,
    fake_client, payload, response,
)


class XDeliveryClient(FakeTelegramClient):
    def __init__(self, http):
        self.http = http
        super().__init__(messages={"saved_messages": [{
            "id": 10, "date": "2026-10-19T00:00:00+00:00",
            "chat": {"id": "saved_messages", "type": "saved_messages"},
            "media": [], "text": POST_URL,
        }]})

    def get_limited(self, url, **options):
        return self.http.get_limited(url, **options)


class XDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.http = fake_client()
        self.client = XDeliveryClient(self.http)
        self.context, _, self.database = _telegram_context(
            self.temp.name, self.client,
            env_overrides={"MEDIAGENT_TELEGRAM_INBOX_CHAT": "saved_messages"},
        )
        self.dns = patch("mediagent.core.links.resolve_host_ips", return_value=["1.1.1.1"])
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def sync(self):
        return asyncio.run(create_default_registry().run(
            "telegram.inbox.sync", {"chat": "saved_messages", "attempts": 1}, self.context,
        ))

    def test_successful_x_delivery_keeps_later_message_provenance_without_redownload(self):
        first = self.sync()
        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(link_delivery_jobs.summary(self.database)["succeeded"], 1)
        self.assertEqual(db.get_sync_cursor(
            self.database, platform="telegram", cursor_name="inbox:saved_messages",
        )["cursor_value"], "10")
        self.http.calls.clear()
        self.client.messages["saved_messages"].append({
            **self.client.messages["saved_messages"][0], "id": 11,
        })
        second = self.sync()
        self.assertTrue(second.is_success, second.to_dict())
        self.assertEqual(self.http.calls, [])
        self.assertEqual(len(db.list_links(self.database)[0]["source_provenance"]), 2)
        with db.connect(self.database) as connection:
            item = connection.execute(
                "SELECT metadata_json FROM media_items WHERE platform='x' AND remote_id=?", (POST_ID,),
            ).fetchone()
            self.assertIsNotNone(item)
            provenance = json.loads(item["metadata_json"])["source_provenance"]
            self.assertEqual({source["message_id"] for source in provenance}, {"10", "11"})

    def test_auth_backlog_returns_cli_zero_then_explicit_retry_uses_self_hosted_endpoint(self):
        self.http.gets[API_URL] = response({"code": 401})
        output = io.StringIO()
        with (
            patch.dict(os.environ, {**self.context.env, "MEDIAGENT_ENV_FILE": ""}, clear=True),
            patch.object(cli.ToolContext, "from_env", return_value=self.context),
            redirect_stdout(output), redirect_stderr(io.StringIO()),
        ):
            code = cli.run(["sync", "telegram", "--summary-json"])
        self.assertEqual(code, 0, output.getvalue())
        summary = json.loads(output.getvalue())["data"]["summary"]
        self.assertTrue(summary["intake_complete"])
        self.assertEqual(summary["link_backlog"]["blocked"], 1)
        self.assertEqual(len(self.http.calls), 1)
        self.sync()
        self.assertEqual(len(self.http.calls), 1)

        base = "https://endpoint.test/2"
        self.context.env.update(MEDIAGENT_X_ENDPOINT_MODE="self_hosted", MEDIAGENT_X_ENDPOINT_URL=base)
        self.http.gets[f"{base}/status/{POST_ID}"] = response(payload(protected=True))
        link = db.list_links(self.database)[0]
        self.assertEqual(link_delivery_jobs.requeue(self.database, [link["id"]], blocked_auth=True), 1)
        self.http.calls.clear()
        recovered = self.sync()
        self.assertTrue(recovered.is_success, recovered.to_dict())
        self.assertEqual(link_delivery_jobs.summary(self.database)["succeeded"], 1)
        self.assertNotIn(("GET_LIMITED", API_URL), self.http.calls)
        self.assertEqual(len(recovered.data["asset_ids"]), 1)

    def test_partial_delivery_retries_failed_attachment_without_redownloading_photo(self):
        self.http.gets[API_URL] = response(payload(entries=[
            {"type": "photo", "url": PHOTO_URL}, {"type": "video", "url": VIDEO_URL},
        ]))
        self.http.gets[VIDEO_URL] = HttpResponse(200, {"Content-Type": "video/mp4"}, b"truncated")
        first = self.sync()
        self.assertTrue(first.is_success, first.to_dict())
        self.assertEqual(link_delivery_jobs.summary(self.database)["deferred"], 1)
        with db.connect(self.database) as connection:
            connection.execute("UPDATE link_delivery_jobs SET next_attempt_at='2000-01-01T00:00:00+00:00'")
        self.http.gets[VIDEO_URL] = HttpResponse(200, {"Content-Type": "video/mp4"}, VIDEO_BYTES)
        self.http.calls.clear()
        recovered = self.sync()
        self.assertTrue(recovered.is_success, recovered.to_dict())
        self.assertEqual(link_delivery_jobs.summary(self.database)["succeeded"], 1)
        self.assertNotIn(("GET_LIMITED", PHOTO_URL), self.http.calls)
        self.assertIn(("GET_LIMITED", VIDEO_URL), self.http.calls)
        with db.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM media_files WHERE status='downloaded'",
            ).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
