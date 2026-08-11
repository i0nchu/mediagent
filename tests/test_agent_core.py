import asyncio
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mediagent import cli
from mediagent.agent import AgentRunner
from mediagent.agent.actions import parse_agent_action
from mediagent.agent.prompts import action_prompt, skill_selection_prompt
from mediagent.agent.schema import AgentRunResult, AgentStatus
from mediagent.agent.skills import SkillRegistry, default_skill_registry
from mediagent.core.tooling import Permission, ToolContext, ToolDefinition, ToolRegistry, ToolResult, ToolSpec


class FakeLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("No fake LLM responses left.")
        return self.responses.pop(0)


class FailingLLM:
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        raise RuntimeError("Ollama request failed: timed out")


class CapturingRunner:
    def __init__(self) -> None:
        self.execute_values: list[bool] = []

    async def run(self, *, task: str, context: ToolContext, skill_name: str | None = None, execute: bool = False):
        self.execute_values.append(execute)
        return AgentRunResult(
            status=AgentStatus.SUCCESS,
            task=task,
            skill=skill_name or "explicit_link_download",
            dry_run=not execute,
            message="ok",
        )


def fake_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.auth.status",
                description="Check Telegram auth.",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK,),
                dry_run_supported=True,
            ),
            handler=lambda context, input_data: ToolResult.success({"status": "usable"}),
        )
    )
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.inbox.collect_links",
                description="Collect inbox links.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "chat": {"type": "object"},
                        "limit": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "full_sync": {"type": "boolean"},
                        "store_cursor": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK,),
                dry_run_supported=True,
                hidden=True,
            ),
            handler=lambda context, input_data: ToolResult.success({"summary": {"links_collected": 2}}),
        )
    )

    async def sync_links(context: ToolContext, input_data: dict) -> ToolResult:
        return ToolResult.success(
            {
                "summary": {
                    "links_considered": 2,
                    "resolved": 2,
                    "files_downloaded": 0 if context.dry_run else 2,
                },
                "dry_run": context.dry_run,
                "input": input_data,
            }
        )

    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="telegram.inbox.sync_links",
                description="Sync inbox links.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "chat": {"type": "object"},
                        "limit": {"type": "integer"},
                        "max_messages": {"type": "integer"},
                        "full_sync": {"type": "boolean"},
                        "store_cursor": {"type": "boolean"},
                        "write_sidecar_metadata": {"type": "boolean"},
                        "retry_failed": {"type": "boolean"},
                        "repair_missing_files": {"type": "boolean"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.WRITE_FILES),
                dry_run_supported=True,
                hidden=True,
            ),
            handler=sync_links,
        )
    )
    registry.register(
        ToolDefinition(
            spec=ToolSpec(
                name="link.media.sync",
                description="Sync explicit links.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "library_root": {"type": "string"},
                        "target_dir": {"type": "string"},
                        "target_path": {"type": "string"},
                    },
                },
                output_schema={"type": "object"},
                permissions=(Permission.NETWORK, Permission.WRITE_FILES),
                dry_run_supported=True,
            ),
            handler=lambda context, input_data: ToolResult.success(
                {
                    "summary": {
                        "resolved": 1,
                        "has_library_root": "library_root" in input_data,
                        "has_target_dir": "target_dir" in input_data,
                        "has_target_path": "target_path" in input_data,
                        "target_dir": input_data.get("target_dir"),
                    }
                }
            ),
        )
    )
    return registry


class AgentCoreTests(unittest.TestCase):
    def test_builtin_skills_are_english_and_include_telegram_inbox(self) -> None:
        registry = default_skill_registry()
        skill = registry.get("telegram_inbox_download")

        self.assertIn("telegram.inbox.sync_links", skill.allowed_tools)
        self.assertIn("Use this skill", skill.body)
        self.assertNotIn("下載", skill.body)
        self.assertTrue(skill.supports_unbounded)
        self.assertTrue(
            any("full scan the configured Telegram inbox workflow" in intent for intent in skill.supported_intents)
        )
        self.assertIn("arbitrary Telegram chat scanning outside the configured inbox workflow", skill.unsupported_intents)
        self.assertIn("checking whether a Telegram inbox exists", skill.unsupported_intents)

    def test_instagram_saved_skill_keeps_unbounded_and_explicit_link_intents_separate(self) -> None:
        skill = default_skill_registry().get("instagram_saved_sync")

        self.assertTrue(skill.supports_unbounded)
        self.assertIn("instagram.saved.sync", skill.allowed_tools)
        self.assertIn("download all saved Instagram media", skill.supported_intents)
        self.assertIn("download an explicit Instagram post or Reel URL", skill.unsupported_intents)
        self.assertIn("omit `limit` and `max_pages`", skill.body)

    def test_skill_summary_includes_intent_boundaries(self) -> None:
        skill = default_skill_registry().get("explicit_link_download")
        summary = skill.summary()

        self.assertTrue(summary["supports_unbounded"])
        self.assertIn("download explicit user-provided media URLs", summary["supported_intents"])
        self.assertIn("account feed crawling", summary["unsupported_intents"])

    def test_parse_agent_action_extracts_json_from_model_text(self) -> None:
        action = parse_agent_action(
            'Here is the action: {"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/a.jpg"},"dry_run":true}'
        )

        self.assertEqual(action.tool, "link.media.sync")
        self.assertTrue(action.dry_run)

    def test_action_prompt_tells_model_to_stop_after_successful_tool_result(self) -> None:
        skill = default_skill_registry().get("explicit_link_download")
        spec = fake_registry().inspect("link.media.sync")

        prompt = action_prompt(
            task="download https://example.com/a.jpg",
            skill=skill,
            tool_specs=[spec],
            dry_run=True,
            previous_steps=[
                {
                    "action": {
                        "action": "call_tool",
                        "tool": "link.media.sync",
                        "input": {"url": "https://example.com/a.jpg"},
                        "dry_run": True,
                    },
                    "tool_result": {"status": "success", "summary": {"resolved": 1}},
                }
            ],
        )

        self.assertIn("Return final now", prompt)

    def test_skill_selection_prompt_allows_unsupported_task(self) -> None:
        prompt = skill_selection_prompt(
            task="我目前有存在的 telegram inbox 嗎？",
            skills=default_skill_registry().summaries(),
        )

        self.assertIn("unsupported_task", prompt)
        self.assertIn("supported_intents", prompt)
        self.assertIn("asking whether a Telegram inbox exists", prompt)

    def test_agent_selects_skill_and_calls_allowed_tool_in_dry_run(self) -> None:
        llm = FakeLLM(
            [
                '{"skill":"telegram_inbox_download","reason":"Inbox download task."}',
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"limit":10,"write_sidecar_metadata":false},"dry_run":true,"reason":"Preview inbox media."}',
                '{"action":"final","message":"Dry-run completed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=True,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(runner.run(task="download inbox media", context=context))

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertEqual(result.skill, "telegram_inbox_download")
        self.assertNotIn("chat", result.steps[0].action.input)
        self.assertEqual(result.steps[0].tool_result["summary"]["links_considered"], 2)
        self.assertEqual(result.message, "Dry-run completed.")

    def test_skill_selection_can_return_unsupported_task_without_tool_call(self) -> None:
        llm = FakeLLM(
            [
                '{"skill":"unsupported_task","reason":"No available skill can inspect configured Telegram inbox existence."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=default_skill_registry(),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(runner.run(task="我目前有存在的 telegram inbox 嗎？", context=context))

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertIsNone(result.skill)
        self.assertEqual(result.steps, [])
        self.assertEqual(result.error.code, "unsupported_task")

    def test_llm_transport_error_during_skill_selection_is_structured_failure(self) -> None:
        runner = AgentRunner(
            llm_client=FailingLLM(),
            tool_registry=fake_registry(),
            skill_registry=default_skill_registry(),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(runner.run(task="download https://example.com/a.jpg", context=context))

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.error.code, "llm_request_failed")
        self.assertIn("Ollama request failed", result.error.details["reason"])

    def test_llm_transport_error_during_action_generation_is_structured_failure(self) -> None:
        runner = AgentRunner(
            llm_client=FailingLLM(),
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("explicit_link_download")]),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download https://example.com/a.jpg",
                context=context,
                skill_name="explicit_link_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.skill, "explicit_link_download")
        self.assertEqual(result.error.code, "llm_request_failed")

    def test_dry_run_rejects_model_execute_action(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"}},"dry_run":false,"reason":"Execute."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=True,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download inbox media",
                context=context,
                skill_name="telegram_inbox_download",
                execute=False,
            )
        )

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.error.code, "execute_not_allowed")

    def test_hidden_stable_inbox_skill_runs_without_experimental_flag(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"}},"dry_run":true,"reason":"Preview inbox media."}',
                '{"action":"final","message":"Dry-run completed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=False,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download inbox media",
                context=context,
                skill_name="telegram_inbox_download",
                execute=False,
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertEqual(result.skill, "telegram_inbox_download")
        self.assertEqual(result.steps[0].tool_result["summary"]["links_considered"], 2)

    def test_required_initial_tool_call_retries_premature_final(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"final","message":"No media found."}',
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"}},"dry_run":true,"reason":"Inspect inbox before answering."}',
                '{"action":"final","message":"Dry-run completed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=False,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download inbox media",
                context=context,
                skill_name="telegram_inbox_download",
                execute=False,
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertEqual(result.steps[0].error.code, "tool_call_required")
        self.assertEqual(result.steps[1].action.tool, "telegram.inbox.sync_links")
        self.assertEqual(result.steps[1].tool_result["summary"]["links_considered"], 2)

    def test_execute_mode_overrides_model_dry_run_action(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"}},"dry_run":true,"reason":"Model requested preview."}',
                '{"action":"final","message":"Executed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=True,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=False)

        result = asyncio.run(
            runner.run(
                task="download inbox media",
                context=context,
                skill_name="telegram_inbox_download",
                execute=True,
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertFalse(result.steps[0].action.dry_run)
        self.assertEqual(result.steps[0].tool_result["summary"]["files_downloaded"], 2)

    def test_unbounded_inbox_task_uses_full_sync_without_invented_limit(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"},"full_sync":true,"store_cursor":false},"dry_run":true,"reason":"Scan all inbox media."}',
                '{"action":"final","message":"Dry-run completed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="完整下載 telegram inbox 中可以下載的所有媒體",
                context=context,
                skill_name="telegram_inbox_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertTrue(result.steps[0].action.input["full_sync"])
        self.assertFalse(result.steps[0].action.input["store_cursor"])
        self.assertNotIn("limit", result.steps[0].action.input)

    def test_bounded_inbox_task_can_use_limit(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"telegram.inbox.sync_links","input":{"chat":{"type":"saved_messages"},"limit":50},"dry_run":true,"reason":"Use explicit limit."}',
                '{"action":"final","message":"Dry-run completed."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="下載 telegram inbox 中前 50 個媒體連結",
                context=context,
                skill_name="telegram_inbox_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        self.assertEqual(result.steps[0].action.input["limit"], 50)

    def test_agent_strips_destination_paths_not_present_in_user_task(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/a.jpg","library_root":"/data/user/0/com.mediagent/app/media","target_dir":"/data/user/0/com.mediagent/app/media"},"dry_run":true,"reason":"Download link."}',
                '{"action":"final","message":"Done."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("explicit_link_download")]),
        )
        context = ToolContext.from_env(env={"MEDIAGENT_DATA_DIR": "/tmp/mediagent-test"}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download https://example.com/a.jpg",
                context=context,
                skill_name="explicit_link_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        summary = result.steps[0].tool_result["summary"]
        self.assertFalse(summary["has_library_root"])
        self.assertFalse(summary["has_target_dir"])
        self.assertNotIn("library_root", result.steps[0].action.input)
        self.assertNotIn("target_dir", result.steps[0].action.input)

    def test_agent_keeps_explicit_user_destination_inside_write_roots(self) -> None:
        target_dir = "/tmp/mediagent-test/explicit-destination"
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/a.jpg","target_dir":"/tmp/mediagent-test/explicit-destination"},"dry_run":true,"reason":"Use user destination."}',
                '{"action":"final","message":"Done."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("explicit_link_download")]),
        )
        context = ToolContext.from_env(env={"MEDIAGENT_DATA_DIR": "/tmp/mediagent-test"}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task=f"download https://example.com/a.jpg to {target_dir}",
                context=context,
                skill_name="explicit_link_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.SUCCESS)
        summary = result.steps[0].tool_result["summary"]
        self.assertTrue(summary["has_target_dir"])
        self.assertEqual(summary["target_dir"], target_dir)

    def test_agent_rejects_explicit_user_destination_outside_write_roots(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/a.jpg","target_dir":"/etc/mediagent"},"dry_run":true,"reason":"Use user destination."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("explicit_link_download")]),
        )
        context = ToolContext.from_env(env={"MEDIAGENT_DATA_DIR": "/tmp/mediagent-test"}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(
                task="download https://example.com/a.jpg to /etc/mediagent",
                context=context,
                skill_name="explicit_link_download",
            )
        )

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.error.code, "unsafe_agent_destination")

    def test_agent_cli_defaults_to_execute_mode(self) -> None:
        runner = CapturingRunner()
        output = io.StringIO()

        with (
            patch("mediagent.cli.build_llm_client", return_value=FakeLLM([])),
            patch("mediagent.cli.AgentRunner.default", return_value=runner),
            redirect_stdout(output),
        ):
            exit_code = cli.run(["agent", "run", "download inbox media", "--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(runner.execute_values, [True])

    def test_agent_cli_dry_run_flag_disables_execute_mode(self) -> None:
        runner = CapturingRunner()
        output = io.StringIO()

        with (
            patch("mediagent.cli.build_llm_client", return_value=FakeLLM([])),
            patch("mediagent.cli.AgentRunner.default", return_value=runner),
            redirect_stdout(output),
        ):
            exit_code = cli.run(["agent", "run", "download inbox media", "--dry-run", "--json"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(runner.execute_values, [False])

    def test_forbidden_tool_is_rejected_before_execution(self) -> None:
        llm = FakeLLM(
            [
                '{"action":"call_tool","tool":"link.media.sync","input":{"url":"https://example.com/a.jpg"},"dry_run":true,"reason":"Wrong tool."}',
            ]
        )
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("telegram_inbox_download")]),
            allow_experimental=True,
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(task="download inbox media", context=context, skill_name="telegram_inbox_download")
        )

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.error.code, "forbidden_tool")

    def test_invalid_model_json_is_structured_failure(self) -> None:
        llm = FakeLLM(["not json"])
        runner = AgentRunner(
            llm_client=llm,
            tool_registry=fake_registry(),
            skill_registry=SkillRegistry([default_skill_registry().get("explicit_link_download")]),
        )
        context = ToolContext.from_env(env={}, cwd=Path.cwd(), dry_run=True)

        result = asyncio.run(
            runner.run(task="download https://example.com/a.jpg", context=context, skill_name="explicit_link_download")
        )

        self.assertEqual(result.status, AgentStatus.FAILURE)
        self.assertEqual(result.error.code, "invalid_model_output")


if __name__ == "__main__":
    unittest.main()
