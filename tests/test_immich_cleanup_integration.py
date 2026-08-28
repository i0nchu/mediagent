import importlib.util
import json
import subprocess
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "deploy/integrations/immich/cleanup_delete_candidates.py"
)
SPEC = importlib.util.spec_from_file_location("immich_cleanup_integration", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ImmichCleanupIntegrationTests(unittest.TestCase):
    def test_systemd_drop_ins_use_service_account_shared_lock_and_valid_timer_reset(self) -> None:
        directory = SCRIPT.parent
        service = (directory / "mediagent.conf").read_text(encoding="utf-8")
        timer = (directory / "timer.conf").read_text(encoding="utf-8")

        self.assertIn("User=server", service)
        self.assertIn("/run/lock/mediagent-sync.lock", service)
        self.assertIn("SuccessExitStatus=75", service)
        self.assertIn("\nOnCalendar=\n", timer)
        self.assertNotIn("OnClaendar", timer)

    def test_container_path_maps_only_inside_library(self) -> None:
        root = Path("/data/nas/mediagent")

        mapped = MODULE.source_path_from_immich(
            "/mnt/mediagent/pixiv/photo/a.jpg",
            container_root="/mnt/mediagent",
            library_root=root,
        )
        outside = MODULE.source_path_from_immich(
            "/mnt/mediagent/../../etc/passwd",
            container_root="/mnt/mediagent",
            library_root=root,
        )

        self.assertEqual(mapped, root / "pixiv/photo/a.jpg")
        self.assertIsNone(outside)

    def test_remove_invokes_one_shot_mediagent_cli_with_audit_reference(self) -> None:
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "status": "success",
                        "data": {"removal_id": "rmv_1", "trash_path": "/library/.trash/mediagent/rmv_1/a.jpg"},
                    }
                ),
                stderr="",
            )

        result = MODULE.remove_with_mediagent(
            project=Path("/service/mediagent"),
            db_path=Path("/service/mediagent/data/mediagent.sqlite3"),
            library_root=Path("/library"),
            source=Path("/library/photo/a.jpg"),
            asset_id="asset-42",
            runner=fake_runner,
        )

        command, options = calls[0]
        self.assertEqual(result["removal_id"], "rmv_1")
        self.assertIn("library", command)
        self.assertIn("remove", command)
        self.assertEqual(command[command.index("--external-ref") + 1], "immich:asset-42")
        self.assertEqual(command[command.index("--reason") + 1], "Immich delete-candidate album")
        self.assertFalse(options["check"])
        self.assertEqual(options["env"]["MEDIAGENT_LIBRARY_DIR"], "/library")

    def test_remove_fails_closed_when_mediagent_rejects_entry(self) -> None:
        def fake_runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command,
                4,
                stdout=json.dumps(
                    {
                        "status": "failure",
                        "error": {"code": "library_entry_not_found", "message": "not managed"},
                    }
                ),
                stderr="",
            )

        with self.assertRaisesRegex(RuntimeError, "library_entry_not_found"):
            MODULE.remove_with_mediagent(
                project=Path("/service/mediagent"),
                db_path=Path("/service/mediagent/data/mediagent.sqlite3"),
                library_root=Path("/library"),
                source=Path("/library/photo/a.jpg"),
                asset_id="asset-42",
                runner=fake_runner,
            )


if __name__ == "__main__":
    unittest.main()
