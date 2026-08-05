"""Parse and validate strict JSON actions returned by the LLM."""

from __future__ import annotations

import json
from typing import Any

from mediagent.agent.schema import AgentAction, AgentActionType, AgentError


class AgentActionError(ValueError):
    def __init__(self, error: AgentError) -> None:
        super().__init__(error.message)
        self.error = error


def parse_agent_action(text: str) -> AgentAction:
    payload = _loads_first_json_object(text)
    if not isinstance(payload, dict):
        raise AgentActionError(AgentError("invalid_model_output", "Model output must be a JSON object."))
    action_value = payload.get("action")
    try:
        action = AgentActionType(str(action_value))
    except ValueError as exc:
        raise AgentActionError(
            AgentError("unknown_agent_action", "Model returned an unknown action.", {"action": action_value})
        ) from exc

    if action == AgentActionType.CALL_TOOL:
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            raise AgentActionError(AgentError("invalid_agent_action", "call_tool action requires a tool name."))
        input_data = payload.get("input", {})
        if not isinstance(input_data, dict):
            raise AgentActionError(AgentError("invalid_agent_action", "call_tool input must be an object."))
        dry_run = payload.get("dry_run")
        if dry_run is not None and not isinstance(dry_run, bool):
            raise AgentActionError(AgentError("invalid_agent_action", "call_tool dry_run must be a boolean."))
        return AgentAction(
            action=action,
            tool=tool,
            input=input_data,
            dry_run=dry_run,
            reason=_optional_string(payload.get("reason")),
        )

    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise AgentActionError(AgentError("invalid_agent_action", f"{action.value} action requires a message."))
    return AgentAction(action=action, message=message.strip())


def parse_skill_choice(text: str) -> tuple[str, str | None]:
    payload = _loads_first_json_object(text)
    if not isinstance(payload, dict):
        raise AgentActionError(AgentError("invalid_model_output", "Skill choice must be a JSON object."))
    skill = payload.get("skill")
    if not isinstance(skill, str) or not skill.strip():
        raise AgentActionError(AgentError("invalid_skill_choice", "Skill choice requires a skill name."))
    return skill.strip(), _optional_string(payload.get("reason"))


def _loads_first_json_object(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    start = stripped.find("{")
    if start < 0:
        raise AgentActionError(AgentError("invalid_model_output", "Model output did not contain JSON."))
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(stripped[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : index + 1])
    raise AgentActionError(AgentError("invalid_model_output", "Model output contained incomplete JSON."))


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
