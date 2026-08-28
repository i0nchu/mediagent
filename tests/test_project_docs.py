from __future__ import annotations

import hashlib
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_SHA256 = "8f9473bc08afb299aa63a1bc2625552cae120077dfed8a90da1494c41e906f5e"


class ProjectDocumentationTests(unittest.TestCase):
    def test_agent_contract_is_unchanged_and_concise(self) -> None:
        content = (PROJECT_ROOT / "AGENT.md").read_bytes()

        self.assertEqual(hashlib.sha256(content).hexdigest(), AGENT_SHA256)
        self.assertLessEqual(len(content.decode("utf-8").splitlines()), 150)

    def test_legacy_handoff_trees_are_removed(self) -> None:
        for name in (".agents", ".agents_zh_tw", ".agents_jp"):
            self.assertFalse((PROJECT_ROOT / name).exists(), name)

    def test_root_documentation_is_present(self) -> None:
        for name in ("README.md", "AGENT.md", "TODO.md"):
            self.assertTrue((PROJECT_ROOT / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
