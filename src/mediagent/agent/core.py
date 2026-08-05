"""SKILL-guided LLM agent runner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from mediagent.agent.actions import AgentActionError, parse_agent_action, parse_skill_choice
from mediagent.agent.prompts import SYSTEM_PROMPT, action_prompt, skill_selection_prompt
from mediagent.agent.schema import AgentAction, AgentActionType, AgentError, AgentRunResult, AgentStatus, AgentStep
from mediagent.agent.skills import AgentSkill, SkillRegistry, default_skill_registry
from mediagent.core.filesystem import PathSafetyError, ensure_inside, normalize_path
from mediagent.core.redaction import redact_secrets
from mediagent.core.schema import validate_input
from mediagent.core.tooling import ToolContext, ToolRegistry, ToolRegistryError
from mediagent.tools.defaults import create_default_registry

UNSUPPORTED_SKILL_NAMES = frozenset({"unsupported_task", "tool_gap", "no_skill", "none"})
DESTINATION_INPUT_FIELDS = frozenset({"library_root", "target_dir", "target_path"})


class LLMClient(Protocol):
    def generate(self, prompt: str, *, system: str | None = None) -> str:
        ...


@dataclass
class AgentRunner:
    llm_client: LLMClient
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    max_steps: int = 4
    allow_experimental: bool = False

    @classmethod
    def default(cls, llm_client: LLMClient, *, max_steps: int = 4, allow_experimental: bool = False) -> "AgentRunner":
        return cls(
            llm_client=llm_client,
            tool_registry=create_default_registry(),
            skill_registry=default_skill_registry(),
            max_steps=max_steps,
            allow_experimental=allow_experimental,
        )

    async def run(
        self,
        *,
        task: str,
        context: ToolContext,
        skill_name: str | None = None,
        execute: bool = False,
    ) -> AgentRunResult:
        dry_run = not execute
        try:
            skill = self._select_skill(task, skill_name)
        except AgentActionError as exc:
            return AgentRunResult(
                status=AgentStatus.FAILURE,
                task=task,
                skill=None,
                dry_run=dry_run,
                error=exc.error,
            )
        except KeyError as exc:
            return AgentRunResult(
                status=AgentStatus.FAILURE,
                task=task,
                skill=skill_name,
                dry_run=dry_run,
                error=AgentError("unknown_skill", str(exc)),
            )

        steps: list[AgentStep] = []
        previous_steps: list[dict] = []
        for index in range(1, self.max_steps + 1):
            try:
                allowed_specs = self._allowed_tool_specs(skill)
            except ToolRegistryError as exc:
                error = AgentError(exc.error.code, exc.error.message, exc.error.details)
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=error,
                )
            prompt = action_prompt(
                task=task,
                skill=skill,
                tool_specs=allowed_specs,
                dry_run=dry_run,
                previous_steps=previous_steps,
            )
            try:
                action = parse_agent_action(self._generate(prompt))
            except AgentActionError as exc:
                step = AgentStep(index=index, action=_error_action(), error=exc.error)
                steps.append(step)
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=exc.error,
                )

            step = AgentStep(index=index, action=action)
            steps.append(step)
            if self._requires_tool_before_response(action, skill=skill, previous_steps=previous_steps):
                error = AgentError(
                    "tool_call_required",
                    "The selected SKILL must call an allowed tool before responding.",
                    {"skill": skill.name},
                )
                step.error = error
                previous_steps.append(step.to_dict())
                continue
            if action.action == AgentActionType.FINAL:
                return AgentRunResult(
                    status=AgentStatus.SUCCESS,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    message=action.message,
                )
            if action.action == AgentActionType.ASK_USER:
                return AgentRunResult(
                    status=AgentStatus.NEEDS_USER,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    message=action.message,
                )
            try:
                action = self._apply_destination_policy(action, task=task, context=context)
            except AgentActionError as exc:
                step.error = exc.error
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=exc.error,
                )
            step.action = action
            error = self._validate_tool_action(action, skill=skill, global_dry_run=dry_run, task=task)
            if error:
                step.error = error
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=error,
                )
            action = self._apply_run_mode(action, global_dry_run=dry_run)
            step.action = action
            assert action.tool is not None
            tool_context = ToolContext.from_env(
                dry_run=dry_run,
                cwd=context.cwd,
                env=context.env,
                http_client=context.http_client,
            )
            try:
                result = await self.tool_registry.run(
                    action.tool,
                    action.input,
                    tool_context,
                    allow_experimental=self.allow_experimental,
                )
            except ToolRegistryError as exc:
                error = AgentError(exc.error.code, exc.error.message, exc.error.details)
                step.error = error
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=error,
                )
            tool_payload = _compact_tool_result(result.to_dict())
            step.tool_result = tool_payload
            previous_steps.append(step.to_dict())
            if not result.is_success:
                error = AgentError(
                    result.error.code if result.error else "tool_failed",
                    result.error.message if result.error else "Tool failed.",
                    result.error.details if result.error else {},
                )
                return AgentRunResult(
                    status=AgentStatus.FAILURE,
                    task=task,
                    skill=skill.name,
                    dry_run=dry_run,
                    steps=steps,
                    error=error,
                )

        return AgentRunResult(
            status=AgentStatus.FAILURE,
            task=task,
            skill=skill.name,
            dry_run=dry_run,
            steps=steps,
            error=AgentError("max_steps_exceeded", "Agent reached the maximum step limit."),
        )

    def _select_skill(self, task: str, skill_name: str | None) -> AgentSkill:
        if skill_name:
            return self.skill_registry.get(skill_name)
        prompt = skill_selection_prompt(task=task, skills=self.skill_registry.summaries())
        selected, reason = parse_skill_choice(self._generate(prompt))
        if selected.strip().lower() in UNSUPPORTED_SKILL_NAMES:
            raise AgentActionError(
                AgentError(
                    "unsupported_task",
                    reason or "No available SKILL can safely perform this task.",
                    {"reason": reason or "no_matching_skill"},
                )
            )
        return self.skill_registry.get(selected)

    def _allowed_tool_specs(self, skill: AgentSkill):
        specs = []
        for tool_name in skill.allowed_tools:
            specs.append(self.tool_registry.inspect(tool_name, allow_experimental=self.allow_experimental))
        return specs

    def _validate_tool_action(
        self,
        action,
        *,
        skill: AgentSkill,
        global_dry_run: bool,
        task: str,
    ) -> AgentError | None:
        if action.tool not in skill.allowed_tools:
            return AgentError("forbidden_tool", "Tool is not allowed by the selected SKILL.", {"tool": action.tool})
        if global_dry_run and action.dry_run is False:
            return AgentError("execute_not_allowed", "Dry-run agent run cannot execute real tool calls.")
        try:
            spec = self.tool_registry.inspect(action.tool or "", allow_experimental=self.allow_experimental)
        except ToolRegistryError as exc:
            return AgentError(exc.error.code, exc.error.message, exc.error.details)
        if global_dry_run and not spec.dry_run_supported:
            return AgentError("dry_run_not_supported", "Tool does not support dry-run.", {"tool": action.tool})
        errors = validate_input(spec.input_schema, action.input)
        if errors:
            return AgentError("invalid_tool_input", "Tool input does not match schema.", {"errors": errors})
        return None

    def _apply_run_mode(self, action, *, global_dry_run: bool):
        return replace(action, dry_run=global_dry_run)

    def _apply_destination_policy(self, action: AgentAction, *, task: str, context: ToolContext) -> AgentAction:
        if action.action != AgentActionType.CALL_TOOL or not action.input:
            return action
        filtered_input = dict(action.input)
        for field in DESTINATION_INPUT_FIELDS:
            value = filtered_input.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            if value not in task:
                filtered_input.pop(field, None)
                continue
            try:
                destination = normalize_path(value, env=context.env, cwd=context.cwd)
                ensure_inside(destination, context.allowed_write_roots())
            except PathSafetyError as exc:
                raise AgentActionError(
                    AgentError(
                        "unsafe_agent_destination",
                        "User-provided destination path is outside configured write roots.",
                        {"field": field, "path": value, "reason": str(exc)},
                    )
                ) from exc
        if filtered_input == action.input:
            return action
        return replace(action, input=filtered_input)

    def _requires_tool_before_response(self, action, *, skill: AgentSkill, previous_steps: list[dict]) -> bool:
        if not skill.requires_initial_tool_call:
            return False
        if previous_steps:
            return False
        return action.action in (AgentActionType.FINAL, AgentActionType.ASK_USER)

    def _generate(self, prompt: str) -> str:
        try:
            return self.llm_client.generate(prompt, system=SYSTEM_PROMPT)
        except RuntimeError as exc:
            raise AgentActionError(
                AgentError(
                    "llm_request_failed",
                    "LLM request failed. Check the configured LLM provider and retry.",
                    {"reason": str(exc)},
                )
            ) from exc


def _error_action():
    return AgentAction(action=AgentActionType.FINAL, message="Invalid model output.")


def _compact_tool_result(result: dict) -> dict:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else None
    compact = {
        "status": result.get("status"),
        "summary": summary,
        "artifacts_count": len(result.get("artifacts") or []),
        "warnings": (result.get("warnings") or [])[:5],
        "error": result.get("error"),
    }
    if summary is None:
        compact["data_keys"] = sorted(data.keys())[:20]
    return redact_secrets(compact)
