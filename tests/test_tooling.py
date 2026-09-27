import asyncio
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mediagent.core.tooling import (
    Permission,
    ToolContext,
    ToolDefinition,
    ToolNotFoundError,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from mediagent.core.redaction import redact_secrets
from mediagent.tools.defaults import create_default_registry


class ToolingTests(unittest.TestCase):
    def test_default_registry_imports_in_fresh_python_process(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from mediagent.tools.defaults import create_default_registry; "
                "create_default_registry()",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_tool_spec_and_result_are_json_compatible(self) -> None:
        spec = ToolSpec(
            name="example.tool",
            description="Example",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permissions=(Permission.READ_ENV,),
            dry_run_supported=True,
        )
        result = ToolResult.success({"ok": True})

        self.assertEqual(spec.to_dict()["permissions"], ["read_env"])
        self.assertEqual(result.to_dict()["status"], "success")
        self.assertEqual(result.to_dict()["data"], {"ok": True})
        self.assertIn("rate_limit", result.to_dict())

    def test_default_registry_lists_bottom_tools(self) -> None:
        registry = create_default_registry()
        names = {spec.name for spec in registry.list()}

        self.assertTrue(
            {
                "auth.session.refresh",
                "auth.session.revoke",
                "auth.session.status",
                "core.db.init",
                "core.env.check",
                "core.path.prepare",
                "core.run.record",
                "core.sync_cursor.get",
                "core.sync_cursor.set",
                "download.http",
                "media.file.upsert",
                "media.item.filter_new",
                "media.item.set_status",
                "media.item.upsert",
                "metadata.write",
                "pixiv.auth.login",
                "pixiv.auth.refresh",
                "pixiv.auth.status",
                "pixiv.bookmarks.collect",
                "pixiv.bookmarks.sync",
                "x.auth.exchange",
                "x.auth.refresh",
                "x.auth.start",
                "x.auth.status",
                "x.bookmarks.collect",
            }.issubset(names)
        )

    def test_unknown_tool_raises_structured_error(self) -> None:
        registry = create_default_registry()

        with self.assertRaises(ToolNotFoundError) as raised:
            registry.inspect("missing.tool")

        self.assertEqual(raised.exception.error.code, "unknown_tool")

    def test_tool_context_uses_explicit_empty_env(self) -> None:
        context = ToolContext.from_env(env={}, dry_run=True)

        self.assertIsNone(context.data_dir)
        self.assertIsNone(context.db_path)
        context.operation_log.info("This remains silent in library use.")

    def test_invalid_input_returns_validation_error(self) -> None:
        registry = create_default_registry()
        with TemporaryDirectory() as temp_dir:
            context = ToolContext.from_env(
                env={"MEDIAGENT_DATA_DIR": temp_dir},
                cwd=Path(temp_dir),
                dry_run=True,
            )

            async def run_invalid() -> None:
                await registry.run("core.path.prepare", {}, context)

            with self.assertRaises(Exception) as raised:
                asyncio.run(run_invalid())

        self.assertEqual(raised.exception.error.code, "invalid_input")

    def test_runtime_exceptions_are_redacted(self) -> None:
        def fail_with_secret(context: ToolContext, input_data: dict) -> ToolResult:
            raise RuntimeError("request failed with token=super-secret")

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                spec=ToolSpec(
                    name="example.fail",
                    description="Fail",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    permissions=(),
                    dry_run_supported=True,
                ),
                handler=fail_with_secret,
            )
        )
        context = ToolContext.from_env(env={}, dry_run=True)

        result = asyncio.run(registry.run("example.fail", {}, context))

        self.assertFalse(result.is_success)
        payload = result.to_dict()
        self.assertNotIn("super-secret", str(payload))
        self.assertEqual(payload["error"]["message"], "Tool failed unexpectedly.")

    def test_structured_error_category_is_serialized(self) -> None:
        result = ToolResult.failure(
            "auth_failed",
            "token=super-secret",
            category="auth",
            details={"refresh_token": "super-secret"},
        )
        payload = result.to_dict()

        self.assertEqual(payload["error"]["category"], "auth")
        self.assertNotIn("super-secret", str(payload))

    def test_result_warning_redaction_preserves_diagnostics_but_removes_credentials(self) -> None:
        warning = (
            "Authorization: Bearer secret-value at "
            "https://user:pass@example.com/file?signature=secret&width=100 "
            "for /home/user/My File.jpg 🚀"
        )

        rendered = ToolResult.success(warnings=[warning]).to_dict()["warnings"][0]

        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("user:pass", rendered)
        self.assertNotIn("signature=secret", rendered)
        self.assertIn("signature=redacted", rendered)
        self.assertIn("width=100", rendered)
        self.assertIn("/home/user/My File.jpg", rendered)
        self.assertIn("🚀", rendered)

    def test_structured_redaction_keeps_status_and_identity_keys(self) -> None:
        rendered = redact_secrets(
            {
                "collection_key": "favorites:account",
                "session_present": True,
                "session_checkpointed": True,
                "access_token": "secret-access",
                "authorization_code": "secret-code",
                "cookies": {"identity": "secret-cookie"},
            }
        )

        self.assertEqual(rendered["collection_key"], "favorites:account")
        self.assertIs(rendered["session_present"], True)
        self.assertIs(rendered["session_checkpointed"], True)
        self.assertEqual(rendered["access_token"], "<redacted>")
        self.assertEqual(rendered["authorization_code"], "<redacted>")
        self.assertEqual(rendered["cookies"], "<redacted>")
