from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent.core import asset_tagging_jobs, assets, db
from mediagent.core.tooling import Permission, ToolContext, ToolResult
from mediagent.tools import tagging_tools
from mediagent.tools.defaults import create_default_registry


class FakeLLM:
    def __init__(self, response: str = '{"tags":["Landscape","night"]}', error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        if self.error is not None:
            raise self.error
        return self.response


class TaggingToolTests(unittest.TestCase):
    def test_registry_contains_hidden_auto_tag_tool(self) -> None:
        definition = create_default_registry().get("library.asset.tags.auto")

        self.assertTrue(definition.spec.hidden)
        self.assertFalse(definition.spec.dry_run_supported)
        self.assertIn(Permission.READ_CREDENTIALS, definition.spec.permissions)

    def test_finalize_disabled_is_side_effect_free(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="false")
            primary = ToolResult.success({"asset_ids": [asset_id]})
            with patch("mediagent.tools.tagging_tools.build_llm_client") as factory:
                result = asyncio.run(tagging_tools.finalize_add_result(context, {}, primary))

            self.assertIs(result, primary)
            self.assertFalse(result.data["tagging"]["enabled"])
            factory.assert_not_called()
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_tagging_jobs").fetchone()[0], 0)

    def test_empty_queue_does_not_build_llm_client(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _db_path, _asset_id, context = self._workspace(temp_dir, auto_tag="false")
            with patch("mediagent.tools.tagging_tools.build_llm_client") as factory:
                result = asyncio.run(tagging_tools.auto_tag_assets(context, {}))

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["tagging"]["requested"], 0)
            factory.assert_not_called()

    def test_finalize_generates_safe_equal_priority_tags_once(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true")
            llm = FakeLLM()
            with patch("mediagent.tools.tagging_tools.build_llm_client", return_value=llm):
                first = asyncio.run(
                    tagging_tools.finalize_add_result(
                        context,
                        {},
                        ToolResult.success({"asset_ids": [asset_id]}),
                    )
                )
                second = asyncio.run(
                    tagging_tools.finalize_add_result(
                        context,
                        {},
                        ToolResult.success({"asset_ids": [asset_id]}),
                    )
                )

            self.assertEqual(first.data["tagging"]["tagged"], 1)
            self.assertEqual(first.data["tagging"]["tags_added"], 2)
            self.assertEqual(second.data["tagging"]["unchanged"], 1)
            self.assertEqual(len(llm.calls), 1)
            prompt = llm.calls[0][0]
            self.assertIn("Visible title", prompt)
            self.assertNotIn("https://private.example", prompt)
            self.assertNotIn("remote-secret-id", prompt)
            self.assertNotIn("TOKEN-SECRET", prompt)
            self.assertNotIn("/private/library/file.jpg", prompt)
            asset = assets.load_asset(db_path, asset_id)
            self.assertEqual(asset["metadata"]["tags"], ["manual", "Landscape", "night"])
            with db.connect(db_path) as connection:
                job = connection.execute("SELECT * FROM asset_tagging_jobs").fetchone()
            self.assertEqual(job["status"], "succeeded")

    def test_llm_failure_keeps_primary_success_and_retry_state(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true")
            llm = FakeLLM(error=RuntimeError("request timed out with token=SECRET"))
            primary = ToolResult.success({"asset_ids": [asset_id]})
            with patch("mediagent.tools.tagging_tools.build_llm_client", return_value=llm):
                result = asyncio.run(tagging_tools.finalize_add_result(context, {}, primary))

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["tagging"]["failed"], 1)
            self.assertTrue(result.warnings)
            asset = assets.load_asset(db_path, asset_id)
            self.assertEqual(asset["metadata"]["tags"], ["manual"])
            with db.connect(db_path) as connection:
                job = connection.execute("SELECT * FROM asset_tagging_jobs").fetchone()
            self.assertEqual(job["status"], "failed")
            self.assertTrue(job["retryable"])
            self.assertEqual(job["last_error_code"], "llm_timeout")
            self.assertNotIn("SECRET", job["last_error"])

    def test_invalid_llm_configuration_defers_without_hiding_download(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true")
            primary = ToolResult.success({"asset_ids": [asset_id]})
            with patch(
                "mediagent.tools.tagging_tools.build_llm_client",
                side_effect=ValueError("invalid endpoint with secret"),
            ):
                result = asyncio.run(tagging_tools.finalize_add_result(context, {}, primary))

            self.assertTrue(result.is_success)
            self.assertTrue(result.data["tagging"]["configuration_error"])
            self.assertEqual(result.data["tagging"]["deferred"], 1)
            with db.connect(db_path) as connection:
                job = connection.execute("SELECT * FROM asset_tagging_jobs").fetchone()
            self.assertEqual(job["status"], "pending")

    def test_invalid_batch_size_still_persists_retryable_job(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true")
            context.env["MEDIAGENT_AUTO_TAG_BATCH_SIZE"] = "invalid"

            result = asyncio.run(
                tagging_tools.finalize_add_result(
                    context,
                    {},
                    ToolResult.success({"asset_ids": [asset_id]}),
                )
            )

            self.assertTrue(result.is_success)
            self.assertTrue(result.data["tagging"]["configuration_error"])
            self.assertEqual(result.data["tagging"]["deferred"], 1)
            with db.connect(db_path) as connection:
                job = connection.execute("SELECT * FROM asset_tagging_jobs").fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "pending")

    def test_one_invalid_asset_does_not_poison_later_jobs(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, first_id, context = self._workspace(temp_dir, auto_tag="false")
            second_id = "asset_second"
            timestamp = datetime(2026, 9, 27, tzinfo=UTC).isoformat()
            with db.connect(db_path) as connection:
                connection.execute(
                    "UPDATE assets SET metadata_json = ? WHERE id = ?",
                    (json.dumps({"title": "Full", "tags": [f"tag-{i}" for i in range(128)]}), first_id),
                )
                connection.execute(
                    """
                    INSERT INTO assets (id, media_type, state, metadata_json, created_at, updated_at)
                    VALUES (?, 'image', 'active', ?, ?, ?)
                    """,
                    (second_id, json.dumps({"title": "Healthy", "tags": []}), timestamp, timestamp),
                )
            llm = FakeLLM(response='{"tags":["new-tag"]}')
            with patch("mediagent.tools.tagging_tools.build_llm_client", return_value=llm):
                result = asyncio.run(
                    tagging_tools.auto_tag_assets(
                        context,
                        {"asset_ids": [first_id, second_id], "force": True, "limit": 2},
                    )
                )

            self.assertFalse(result.is_success)
            self.assertEqual(result.data["tagging"]["failed"], 1)
            self.assertEqual(result.data["tagging"]["tagged"], 1)
            self.assertEqual(len(llm.calls), 2)
            self.assertIn("new-tag", assets.load_asset(db_path, second_id)["metadata"]["tags"])
            with db.connect(db_path) as connection:
                statuses = {
                    row["asset_id"]: row["status"]
                    for row in connection.execute("SELECT asset_id, status FROM asset_tagging_jobs")
                }
            self.assertEqual(statuses[first_id], "failed")
            self.assertEqual(statuses[second_id], "succeeded")

    def test_lost_lease_is_deferred_without_crashing_worker(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="false")

            class LeaseStealingLLM(FakeLLM):
                def generate(inner_self, prompt: str, *, system: str | None = None) -> str:
                    with db.connect(db_path) as connection:
                        connection.execute(
                            """
                            UPDATE asset_tagging_jobs
                            SET lease_owner = 'other-worker', lease_token = 'other-token'
                            WHERE asset_id = ?
                            """,
                            (asset_id,),
                        )
                    return super().generate(prompt, system=system)

            with patch(
                "mediagent.tools.tagging_tools.build_llm_client",
                return_value=LeaseStealingLLM(),
            ):
                result = asyncio.run(
                    tagging_tools.auto_tag_assets(
                        context,
                        {"asset_ids": [asset_id], "force": True},
                    )
                )

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["tagging"]["deferred"], 1)
            self.assertEqual(result.data["tagging"]["tagged"], 0)
            self.assertEqual(assets.load_asset(db_path, asset_id)["metadata"]["tags"], ["manual"])

    def test_asset_merge_during_inference_supersedes_job_without_crash(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, merged_id, context = self._workspace(temp_dir, auto_tag="false")
            canonical_id = "asset_canonical"
            timestamp = datetime(2026, 9, 27, tzinfo=UTC).isoformat()
            with db.connect(db_path) as connection:
                connection.execute(
                    """
                    INSERT INTO assets (id, media_type, state, metadata_json, created_at, updated_at)
                    VALUES (?, 'image', 'active', ?, ?, ?)
                    """,
                    (
                        canonical_id,
                        json.dumps({"title": "Canonical", "tags": []}),
                        timestamp,
                        timestamp,
                    ),
                )
            canonical_snapshot = tagging_tools._snapshot_for_asset(db_path, canonical_id)
            asset_tagging_jobs.enqueue(
                db_path,
                asset_id=canonical_id,
                input_fingerprint=tagging_tools.metadata_fingerprint(canonical_snapshot),
                provider="openai_compatible",
                model="qwen3-8b",
                prompt_version=tagging_tools.JOB_CONTRACT_VERSION,
            )

            class MergingLLM(FakeLLM):
                def generate(inner_self, prompt: str, *, system: str | None = None) -> str:
                    with db.connect(db_path) as connection:
                        connection.execute("BEGIN IMMEDIATE")
                        assets._merge_assets(
                            connection,
                            canonical_id=canonical_id,
                            merged_id=merged_id,
                            now=timestamp,
                        )
                    return super().generate(prompt, system=system)

            with patch(
                "mediagent.tools.tagging_tools.build_llm_client",
                return_value=MergingLLM(),
            ):
                result = asyncio.run(
                    tagging_tools.auto_tag_assets(
                        context,
                        {"asset_ids": [merged_id], "force": True},
                    )
                )

            self.assertTrue(result.is_success)
            self.assertEqual(result.data["tagging"]["deferred"], 1)
            self.assertEqual(result.data["tagging"]["tagged"], 0)
            with db.connect(db_path) as connection:
                jobs = connection.execute("SELECT * FROM asset_tagging_jobs").fetchall()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["asset_id"], canonical_id)
            self.assertEqual(jobs[0]["status"], "pending")

    def test_metadata_change_during_inference_discards_stale_tags(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true")

            class MutatingLLM(FakeLLM):
                def generate(inner_self, prompt: str, *, system: str | None = None) -> str:
                    with db.connect(db_path) as connection:
                        connection.execute(
                            "UPDATE assets SET metadata_json = ? WHERE id = ?",
                            (json.dumps({"title": "Changed title", "tags": ["manual"]}), asset_id),
                        )
                    return super().generate(prompt, system=system)

            with patch("mediagent.tools.tagging_tools.build_llm_client", return_value=MutatingLLM()):
                result = asyncio.run(
                    tagging_tools.finalize_add_result(
                        context,
                        {},
                        ToolResult.success({"asset_ids": [asset_id]}),
                    )
                )

            self.assertEqual(result.data["tagging"]["deferred"], 1)
            self.assertEqual(result.data["tagging"]["tagged"], 0)
            self.assertEqual(assets.load_asset(db_path, asset_id)["metadata"]["tags"], ["manual"])
            with db.connect(db_path) as connection:
                job = connection.execute("SELECT * FROM asset_tagging_jobs").fetchone()
            self.assertEqual(job["status"], "pending")
            self.assertIsNone(job["lease_token"])

    def test_dry_run_never_enqueues_or_calls_llm(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path, asset_id, context = self._workspace(temp_dir, auto_tag="true", dry_run=True)
            with patch("mediagent.tools.tagging_tools.build_llm_client") as factory:
                result = asyncio.run(
                    tagging_tools.finalize_add_result(
                        context,
                        {},
                        ToolResult.success({"asset_ids": [asset_id]}),
                    )
                )

            self.assertFalse(result.data["tagging"]["enabled"])
            factory.assert_not_called()
            with db.connect(db_path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_tagging_jobs").fetchone()[0], 0)

    def test_explicit_tool_reports_invalid_model_output_as_partial(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _db_path, asset_id, context = self._workspace(temp_dir, auto_tag="false")
            llm = FakeLLM(response="not json")
            with patch("mediagent.tools.tagging_tools.build_llm_client", return_value=llm):
                result = asyncio.run(
                    tagging_tools.auto_tag_assets(
                        context,
                        {"asset_ids": [asset_id], "force": True},
                    )
                )

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "asset_auto_tag_partial")
            self.assertEqual(result.data["tagging"]["failed"], 1)

    def _workspace(
        self,
        temp_dir: str,
        *,
        auto_tag: str,
        dry_run: bool = False,
    ) -> tuple[Path, str, ToolContext]:
        root = Path(temp_dir)
        db_path = root / "data" / "mediagent.sqlite3"
        db.initialize_database(db_path)
        db.upsert_media_item(
            db_path,
            {
                "platform": "example",
                "remote_id": "remote-secret-id",
                "media_type": "photo",
                "source_url": "https://private.example/item?token=TOKEN-SECRET",
                "author_name": "Alice",
                "metadata": {
                    "title": "Visible title",
                    "caption": "A calm night scene",
                    "download_url": "https://private.example/file?token=TOKEN-SECRET",
                    "local_path": "/private/library/file.jpg",
                    "session": "TOKEN-SECRET",
                },
            },
        )
        timestamp = datetime(2026, 9, 27, tzinfo=UTC).isoformat()
        asset_id = "asset_example"
        with db.connect(db_path) as connection:
            item_id = connection.execute(
                "SELECT id FROM media_items WHERE platform = 'example' AND remote_id = 'remote-secret-id'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO assets (id, media_type, state, metadata_json, created_at, updated_at)
                VALUES (?, 'image', 'active', ?, ?, ?)
                """,
                (
                    asset_id,
                    json.dumps({"title": "Visible title", "tags": ["manual"]}),
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO asset_sources (asset_id, media_item_id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                """,
                (asset_id, item_id, timestamp, timestamp),
            )
        context = ToolContext(
            cwd=root,
            env={
                "MEDIAGENT_AUTO_TAG": auto_tag,
                "MEDIAGENT_LLM_PROVIDER": "openai_compatible",
                "MEDIAGENT_OPENAI_MODEL": "qwen3-8b",
            },
            dry_run=dry_run,
            run_id="tagging-test",
            data_dir=db_path.parent,
            db_path=db_path,
        )
        return db_path, asset_id, context


if __name__ == "__main__":
    unittest.main()
