from __future__ import annotations

import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = PROJECT_ROOT / "deploy" / "systemd" / "system"


class SystemComicTimerTests(unittest.TestCase):
    def test_comic_services_share_lock_and_use_summary_output(self) -> None:
        for provider in ("jmcomic", "nhentai"):
            service = (SYSTEMD_DIR / f"mediagent-{provider}-favorites.service").read_text(
                encoding="utf-8"
            )
            self.assertIn("/run/lock/mediagent-sync.lock", service)
            self.assertIn("--conflict-exit-code 75", service)
            self.assertIn("SuccessExitStatus=75", service)
            self.assertIn("--summary-json", service)
            self.assertNotIn(" --json", service)

    def test_comic_timer_inputs_enable_retry_and_missing_file_repair(self) -> None:
        for provider in ("jmcomic", "nhentai"):
            payload = json.loads(
                (SYSTEMD_DIR / f"{provider}.favorites.sync.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload,
                {"retry_failed": True, "repair_missing_files": True},
            )


if __name__ == "__main__":
    unittest.main()
