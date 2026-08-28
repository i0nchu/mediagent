from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core.config import EnvFileError, load_env_file


class EnvFileTests(unittest.TestCase):
    def test_loads_quotes_comments_and_sequential_placeholders(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "DATA=/tmp/from-file\n"
                "LIB=${DATA}/library\n"
                "SCOPES=\"one two\" # comment\n",
                encoding="utf-8",
            )
            env = {"DATA": "/tmp/from-environment"}

            loaded = load_env_file(path, env=env)

        self.assertEqual(env["DATA"], "/tmp/from-environment")
        self.assertEqual(env["LIB"], "/tmp/from-environment/library")
        self.assertEqual(env["SCOPES"], "one two")
        self.assertEqual(loaded["DATA"], "/tmp/from-environment")

    def test_shell_syntax_is_never_executed(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            marker = Path(directory) / "not-executed"
            path.write_text(f"BAD=$(touch {marker})\n", encoding="utf-8")

            env: dict[str, str] = {}
            load_env_file(path, env=env)

        self.assertIn("$(touch", env["BAD"])
        self.assertFalse(marker.exists())

    def test_missing_file_is_a_noop(self) -> None:
        self.assertEqual(load_env_file(Path("/missing/mediagent/.env"), env={}), {})


if __name__ == "__main__":
    unittest.main()
