import asyncio
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mediagent.core import db
from mediagent.core.http import HttpResponse
from mediagent.core.tooling import ToolContext
from mediagent.tools import link_tools
from test_comic_tools import _comic_item, _png_bytes


class FileBudgetTests(unittest.TestCase):
    def test_repair_without_tracked_file_never_overwrites_an_unmanaged_collision(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            context = ToolContext.from_env(cwd=root, env={"MEDIAGENT_DATA_DIR": str(root), "MEDIAGENT_DB_PATH": str(database)})
            item = _comic_item()
            db.initialize_database(database)
            db.upsert_media_item(database, item)
            target = Path(link_tools._planned_downloads(context, {}, item)[0]["target_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"operator-owned existing file")
            item["_repair"] = link_tools._repair_assessment(database, item)
            self.assertEqual(item["_repair"]["missing"], 1)
            with patch.object(link_tools, "download_resolved_file_safely") as download:
                result = asyncio.run(link_tools._sync_one_link_item(context, database, item, {}))
            download.assert_not_called()
            self.assertEqual(result["files_failed"], 1)
            self.assertEqual(target.read_bytes(), b"operator-owned existing file")

    def test_pause_between_pages_keeps_first_file_and_resume_only_fetches_remaining_page(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            now = [0.0]
            calls = []
            content = _png_bytes()

            class Images:
                def get_limited(self, url, **kwargs):
                    calls.append(url)
                    now[0] = 2.0
                    return HttpResponse(200, {"Content-Type": "image/png"}, content, url)

            context = ToolContext.from_env(cwd=root, env={"MEDIAGENT_DATA_DIR": str(root), "MEDIAGENT_DB_PATH": str(database)}, http_client=Images())
            item = _comic_item()
            item["metadata"]["files"].append({**item["metadata"]["files"][0], "url": "https://1.1.1.1/page2.png", "page": 1, "page_number": 2})
            item["metadata"]["page_count"] = 2
            db.initialize_database(database)
            db.upsert_media_item(database, item)
            with patch.object(link_tools.time, "monotonic", side_effect=lambda: now[0]):
                first = asyncio.run(link_tools._sync_one_link_item(context, database, item, {"_deadline_monotonic": 1}))
            self.assertTrue(first["budget_exhausted"])
            self.assertEqual(first["files_pending"], 1)
            self.assertEqual(first["files_failed"], 0)
            self.assertEqual(db.get_media_statuses(database, [item])[("nhentai", "gallery:123")], "partial")
            self.assertEqual(len(db.list_media_files(database, platform="nhentai", remote_id="gallery:123")), 1)
            second = asyncio.run(link_tools._sync_one_link_item(context, database, item, {}))
            self.assertEqual(second["status"], "downloaded")
            self.assertEqual(calls, ["https://1.1.1.1/page.png", "https://1.1.1.1/page2.png"])

    def test_corrupt_tracked_page_is_actually_replaced_while_healthy_page_is_reused(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            calls = []
            content = _png_bytes()

            class Images:
                def get_limited(self, url, **kwargs):
                    calls.append(url)
                    return HttpResponse(200, {"Content-Type": "image/png"}, content, url)

            context = ToolContext.from_env(cwd=root, env={"MEDIAGENT_DATA_DIR": str(root), "MEDIAGENT_DB_PATH": str(database)}, http_client=Images())
            item = _comic_item()
            db.initialize_database(database)
            db.upsert_media_item(database, item)
            asyncio.run(link_tools._sync_one_link_item(context, database, item, {}))
            record = db.list_media_files(database, platform="nhentai", remote_id="gallery:123")[0]
            path = Path(record["local_path"])
            path.write_bytes(b"corrupt")
            with db.connect(database) as connection:
                connection.execute("UPDATE media_files SET file_health='corrupt' WHERE id=?", (record["id"],))
            repair = deepcopy(item)
            repair["_repair"] = link_tools._repair_assessment(database, item)
            self.assertTrue(repair["_repair"]["repairable"])
            result = asyncio.run(link_tools._sync_one_link_item(context, database, repair, {}))
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
