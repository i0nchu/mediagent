import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from PIL import Image

from mediagent.core import assets, db, library_content, local_import
from mediagent.core.tooling import ToolContext
from mediagent.tools.defaults import create_default_registry


class LocalImportToolTests(unittest.TestCase):
    def test_external_image_is_copied_and_second_add_is_existing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context = self._workspace(temp_dir)
            source = root / "external" / "sample.png"
            self._image(source, color="red")
            original = source.read_bytes()

            first = self._run(context, source)
            second = self._run(context, source)

            self.assertTrue(first.is_success, first.error)
            self.assertTrue(second.is_success, second.error)
            self.assertEqual(first.data["summary"]["imported"], 1)
            self.assertEqual(first.data["summary"]["bytes_copied"], len(original))
            self.assertEqual(second.data["summary"]["existing"], 1)
            self.assertEqual(second.data["summary"]["bytes_copied"], 0)
            self.assertEqual(source.read_bytes(), original)
            target = Path(first.data["results"][0]["target_path"])
            self.assertTrue(target.is_file())
            self.assertNotEqual(target, source)
            self.assertEqual(target.read_bytes(), original)
            asset_id = first.data["results"][0]["asset_id"]
            self.assertEqual(first.data["asset_ids"], [asset_id])
            self.assertEqual(second.data["results"][0]["asset_id"], asset_id)
            self.assertEqual(second.data["asset_ids"], [asset_id])
            asset = assets.load_asset(db_path, asset_id)
            self.assertEqual(asset["metadata"]["tags"], ["source:local", "type:image"])
            self.assertEqual(asset["metadata"]["title"], "sample.png")
            self.assertEqual(len(list(library.rglob("*.png"))), 1)

    def test_equal_files_from_two_paths_preserve_two_sources_without_duplicate_copy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context = self._workspace(temp_dir)
            first_source = root / "a" / "same.jpg"
            second_source = root / "b" / "copy.jpg"
            self._image(first_source, color="blue", image_format="JPEG")
            second_source.parent.mkdir(parents=True)
            second_source.write_bytes(first_source.read_bytes())

            first = self._run(context, first_source)
            second = self._run(context, second_source)

            first_item = first.data["results"][0]
            second_item = second.data["results"][0]
            self.assertEqual(first_item["status"], "imported")
            self.assertEqual(second_item["status"], "existing")
            self.assertEqual(first_item["asset_id"], second_item["asset_id"])
            asset = assets.load_asset(db_path, first_item["asset_id"])
            self.assertEqual(asset["source_count"], 2)
            self.assertEqual(asset["representation_count"], 1)
            self.assertEqual(len(list(library.rglob("*.jpg"))), 1)
            self.assertTrue(first_source.is_file())
            self.assertTrue(second_source.is_file())

    def test_local_copy_of_provider_content_adds_source_to_same_asset(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context = self._workspace(temp_dir)
            provider_file = library / "pixiv" / "photo" / "provider.png"
            self._image(provider_file, color="teal")
            db.initialize_database(db_path)
            db.upsert_media_item(
                db_path,
                {
                    "platform": "pixiv",
                    "remote_id": "provider-work",
                    "media_type": "photo",
                    "status": "downloaded",
                    "metadata": {"title": "Provider work"},
                },
            )
            checksum, size = library_content.sha256_checksum(provider_file)
            record = db.upsert_media_file(
                db_path,
                platform="pixiv",
                remote_id="provider-work",
                file_key="main",
                remote_url="https://example.invalid/provider.png",
                local_path=str(provider_file),
                mime_type="image/png",
                size_bytes=size,
                checksum=checksum,
                status="downloaded",
                library_relative_path="pixiv/photo/provider.png",
            )
            provider_adoption = library_content.adopt_media_file(db_path, file_id=record["id"])
            local_copy = root / "external" / "provider-copy.png"
            local_copy.parent.mkdir(parents=True)
            local_copy.write_bytes(provider_file.read_bytes())

            imported = self._run(context, local_copy)

            item = imported.data["results"][0]
            self.assertEqual(item["status"], "existing")
            self.assertEqual(item["asset_id"], provider_adoption["asset_id"])
            self.assertEqual(imported.data["asset_ids"], [provider_adoption["asset_id"]])
            asset = assets.load_asset(db_path, item["asset_id"])
            self.assertEqual(asset["source_count"], 2)
            self.assertEqual(asset["representation_count"], 1)
            self.assertEqual(
                asset["metadata"]["tags"],
                ["source:pixiv", "source:local", "type:image"],
            )
            self.assertEqual(len(list(library.rglob("*.png"))), 1)
            self.assertTrue(local_copy.is_file())

    def test_file_already_in_library_is_adopted_without_copy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, _db_path, context = self._workspace(temp_dir)
            source = library / "manual" / "inside.png"
            self._image(source, color="green")
            before = source.stat()

            result = self._run(context, source)

            self.assertTrue(result.is_success, result.error)
            item = result.data["results"][0]
            self.assertEqual(item["status"], "adopted")
            self.assertEqual(Path(item["target_path"]), source)
            self.assertFalse(item["copied"])
            self.assertEqual(source.stat().st_ino, before.st_ino)

    def test_directory_continues_after_unsupported_broken_and_symlink_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, _db_path, context = self._workspace(temp_dir)
            incoming = root / "incoming"
            self._image(incoming / "good.png", color="purple")
            (incoming / "notes.txt").write_text("not media", encoding="utf-8")
            (incoming / "broken.jpg").write_bytes(b"not a jpeg")
            outside = root / "outside.png"
            self._image(outside, color="yellow")
            os.symlink(outside, incoming / "linked.png")

            result = self._run(context, incoming)

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "local_import_partial")
            summary = result.data["summary"]
            self.assertEqual(summary["imported"], 1)
            self.assertEqual(summary["unsupported"], 1)
            self.assertEqual(summary["failed"], 1)
            self.assertEqual(summary["symlinks_skipped"], 1)
            self.assertTrue(outside.is_file())
            self.assertEqual((incoming / "broken.jpg").read_bytes(), b"not a jpeg")

    def test_single_unsupported_file_returns_validation_failure(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, _db_path, context = self._workspace(temp_dir)
            source = root / "document.txt"
            source.write_text("plain text", encoding="utf-8")

            result = self._run(context, source)

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "unsupported_local_media")
            self.assertEqual(result.data["summary"]["unsupported"], 1)

    def test_recursive_import_excludes_configured_data_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, _db_path, context = self._workspace(temp_dir)
            source = root / "outside.png"
            private = root / "data" / "private.png"
            self._image(source, color="silver")
            self._image(private, color="gold")

            result = self._run(context, root)

            self.assertTrue(result.is_success, result.error)
            self.assertEqual(result.data["summary"]["imported"], 1)
            self.assertEqual(result.data["summary"]["excluded"], 1)
            self.assertEqual(len(list(library.rglob("*.png"))), 1)
            self.assertTrue(private.is_file())

    def test_recursive_import_excludes_managed_library_outside_data_directory(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, _db_path, context = self._workspace(temp_dir)
            self._image(root / "incoming.png", color="silver")
            self._image(library / "existing.png", color="gold")

            result = self._run(context, root)

            self.assertTrue(result.is_success, result.error)
            self.assertEqual(result.data["summary"]["imported"], 1)
            self.assertEqual(result.data["summary"]["excluded"], 1)
            self.assertTrue((library / "existing.png").is_file())

    def test_file_inside_managed_trash_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            _root, library, _db_path, context = self._workspace(temp_dir)
            source = library / ".trash" / "mediagent" / "removed.png"
            self._image(source, color="maroon")

            result = self._run(context, source)

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "local_import_failed")
            self.assertTrue(source.is_file())

    def test_dry_run_hashes_and_plans_without_database_or_copy(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context = self._workspace(temp_dir, dry_run=True)
            source = root / "incoming" / "dry.png"
            self._image(source, color="white")

            result = self._run(context, source)

            self.assertTrue(result.is_success, result.error)
            self.assertEqual(result.data["summary"]["would_import"], 1)
            self.assertFalse(db_path.exists())
            self.assertFalse(library.exists())
            self.assertTrue(source.is_file())

    def test_removed_content_is_blocked_and_requires_restore(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, library, db_path, context = self._workspace(temp_dir)
            source = root / "incoming" / "removed.png"
            self._image(source, color="black")
            imported = self._run(context, source)
            entry_id = db.list_media_files(db_path, platform="local")[0]["library_entry_id"]
            library_content.remove_entry(db_path, entry_id=entry_id, library_root=library)

            blocked = self._run(context, source)

            self.assertFalse(blocked.is_success)
            self.assertEqual(blocked.error.code, "local_content_removed")
            self.assertEqual(blocked.data["summary"]["blocked"], 1)
            self.assertTrue(source.is_file())
            asset = assets.load_asset(db_path, imported.data["results"][0]["asset_id"])
            self.assertEqual(asset["state"], "removed")

    def test_missing_active_content_is_repaired_from_explicit_local_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, db_path, context = self._workspace(temp_dir)
            source = root / "incoming" / "repair.png"
            self._image(source, color="navy")
            imported = self._run(context, source)
            first_item = imported.data["results"][0]
            target = Path(first_item["target_path"])
            target.unlink()

            repaired = self._run(context, source)

            self.assertTrue(repaired.is_success, repaired.error)
            repaired_item = repaired.data["results"][0]
            self.assertEqual(repaired_item["status"], "repaired")
            self.assertEqual(repaired_item["asset_id"], first_item["asset_id"])
            self.assertEqual(repaired.data["summary"]["repaired"], 1)
            self.assertEqual(repaired.data["summary"]["bytes_copied"], source.stat().st_size)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(db.list_media_files(db_path, platform="local")[0]["file_health"], "valid")

    def test_corrupt_active_content_is_atomically_repaired(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, _db_path, context = self._workspace(temp_dir)
            source = root / "incoming" / "corrupt-repair.png"
            self._image(source, color="magenta")
            expected = source.read_bytes()
            imported = self._run(context, source)
            target = Path(imported.data["results"][0]["target_path"])
            target.write_bytes(b"corrupt-managed-content")

            repaired = self._run(context, source)

            self.assertTrue(repaired.is_success, repaired.error)
            self.assertEqual(repaired.data["results"][0]["status"], "repaired")
            self.assertEqual(target.read_bytes(), expected)
            self.assertEqual(source.read_bytes(), expected)
            self.assertEqual(list(target.parent.glob("*.partial")), [])

    def test_valid_cbz_is_imported_as_comic(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, db_path, context = self._workspace(temp_dir)
            page = root / "page.png"
            self._image(page, color="orange")
            archive = root / "incoming" / "chapter.cbz"
            archive.parent.mkdir(parents=True)
            with ZipFile(archive, "w") as output:
                output.write(page, "001.png")

            result = self._run(context, archive)

            self.assertTrue(result.is_success, result.error)
            item = result.data["results"][0]
            self.assertEqual(item["media_type"], "comic")
            self.assertEqual(item["mime_type"], "application/vnd.comicbook+zip")
            asset = assets.load_asset(db_path, item["asset_id"])
            self.assertEqual(asset["media_type"], "comic")
            self.assertEqual(asset["metadata"]["tags"], ["source:local", "type:comic"])

    def test_audio_and_video_signatures_are_detected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio = root / "sample.mp3"
            video = root / "sample.mp4"
            audio.write_bytes(b"ID3" + b"\x00" * 64)
            video.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)

            audio_type = local_import.detect_media(audio)
            video_type = local_import.detect_media(video)

            self.assertEqual((audio_type.media_type, audio_type.mime_type), ("audio", "audio/mpeg"))
            self.assertEqual((video_type.media_type, video_type.mime_type), ("video", "video/mp4"))

    def test_image_content_with_unknown_extension_uses_detected_type(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, _db_path, context = self._workspace(temp_dir)
            source = root / "incoming" / "image.bin"
            self._image(source, color="lime")

            result = self._run(context, source)

            self.assertTrue(result.is_success, result.error)
            target = Path(result.data["results"][0]["target_path"])
            self.assertEqual(target.suffix, ".png")
            self.assertEqual(result.data["results"][0]["mime_type"], "image/png")

    def test_input_through_symlink_parent_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root, _library, _db_path, context = self._workspace(temp_dir)
            real = root / "real"
            source = real / "sample.png"
            self._image(source, color="cyan")
            alias = root / "alias"
            os.symlink(real, alias)

            result = self._run(context, alias / "sample.png")

            self.assertFalse(result.is_success)
            self.assertEqual(result.error.code, "local_import_failed")
            self.assertTrue(source.is_file())

    def test_copy_failure_removes_partial_and_publishes_nothing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "library"
            source = root / "source.bin"
            target = library / "local" / "photo" / "2026" / "08" / "target.bin"
            source.write_bytes(b"atomic-copy")
            checksum, size = library_content.sha256_checksum(source)

            with patch("mediagent.core.local_import.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    local_import.copy_verified(
                        source=source,
                        target=target,
                        library_root=library,
                        expected_checksum=checksum,
                        expected_size=size,
                    )

            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob("*.partial")), [])
            self.assertEqual(source.read_bytes(), b"atomic-copy")

    def _workspace(
        self,
        temp_dir: str,
        *,
        dry_run: bool = False,
    ) -> tuple[Path, Path, Path, ToolContext]:
        root = Path(temp_dir)
        library = root / "library"
        data = root / "data"
        db_path = data / "mediagent.sqlite3"
        context = ToolContext.from_env(
            cwd=root,
            dry_run=dry_run,
            env={
                "MEDIAGENT_DATA_DIR": str(data),
                "MEDIAGENT_DB_PATH": str(db_path),
                "MEDIAGENT_LIBRARY_DIR": str(library),
            },
        )
        return root, library, db_path, context

    def _run(self, context: ToolContext, path: Path):
        return asyncio.run(
            create_default_registry().run(
                "media.local.import",
                {"path": str(path)},
                context,
            )
        )

    def _image(self, path: Path, *, color: str, image_format: str = "PNG") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), color=color).save(path, format=image_format)


if __name__ == "__main__":
    unittest.main()
