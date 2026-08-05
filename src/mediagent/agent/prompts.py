"""Prompt builders for Agent Core V1."""

from __future__ import annotations

import json
from typing import Any

from mediagent.agent.skills import AgentSkill
from mediagent.core.tooling import ToolSpec


SYSTEM_PROMPT = """You are Mediagent Agent Core.
You turn user media automation tasks into safe Mediagent tool calls.
Return exactly one JSON object and no Markdown.
Do not invent tools. Do not run shell commands. Do not ask to read environment files.
Use the same natural language as the user for final or ask_user messages when possible.
Do not include hidden reasoning. Keep JSON values short.
"""


def skill_selection_prompt(*, task: str, skills: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Choose one Mediagent SKILL for the user task only when the task clearly matches a SKILL boundary.",
            "Use each SKILL's supported_intents and unsupported_intents as the main boundary hints.",
            "If no SKILL clearly supports the task, do not guess. Return unsupported_task.",
            "Status, configuration, or existence questions are unsupported unless a SKILL explicitly says it can inspect that exact state.",
            "Example unsupported task: asking whether a Telegram inbox exists is not a Telegram inbox download task.",
            "Return exactly one of:",
            '{"skill":"skill_name","reason":"short reason"}',
            '{"skill":"unsupported_task","reason":"why no available SKILL can safely perform this task"}',
            "",
            "Available skills:",
            json.dumps(skills, ensure_ascii=False, indent=2),
            "",
            "User task:",
            task,
        ]
    )


def action_prompt(
    *,
    task: str,
    skill: AgentSkill,
    tool_specs: list[ToolSpec],
    dry_run: bool,
    previous_steps: list[dict[str, Any]],
) -> str:
    compact_tools = [_compact_tool_spec(spec) for spec in tool_specs]
    mode = "dry-run" if dry_run else "execute"
    completion_hint = _completion_hint(previous_steps)
    return "\n".join(
        [
            "Use the selected SKILL to decide the next Mediagent action.",
            "Return exactly one JSON object.",
            completion_hint,
            "",
            "Allowed JSON actions:",
            '{"action":"call_tool","tool":"tool.name","input":{},"dry_run":true,"reason":"short reason"}',
            '{"action":"final","message":"short user-facing summary"}',
            '{"action":"ask_user","message":"short question or confirmation request"}',
            "",
            f"Current mode: {mode}",
            "In dry-run mode, call tools with dry_run true.",
            "In execute mode, call tools with dry_run false.",
            "If the selected SKILL says requires_initial_tool_call, the first action must be call_tool.",
            "If the user asks for all/everything/complete/until exhausted and the SKILL describes a full-source sync mode, use that mode and do not invent an artificial limit.",
            "For full-source sync tasks, trust tool-layer dedupe and omit count/page limits unless the user explicitly provides a boundary.",
            "If the selected SKILL has no full-source mode for the requested scope, ask_user or return final instead of guessing a narrower task.",
            "If a previous step already has a successful tool_result for this task, return final.",
            "Do not repeat the same tool call with the same input.",
            "",
            "Selected SKILL metadata:",
            json.dumps(skill.summary(), ensure_ascii=False, indent=2),
            "",
            "Selected SKILL instructions:",
            skill.body,
            "",
            "Allowed tools:",
            json.dumps(compact_tools, ensure_ascii=False, indent=2),
            "",
            "Previous steps:",
            json.dumps(previous_steps, ensure_ascii=False, indent=2),
            "",
            "User task:",
            task,
        ]
    )


def _compact_tool_spec(spec: ToolSpec) -> dict[str, Any]:
    return {
        "name": spec.name,
        "description": spec.description,
        "input_schema": _compact_schema(spec.input_schema),
        "permissions": [permission.value for permission in spec.permissions],
        "dry_run_supported": spec.dry_run_supported,
        "experimental": spec.experimental,
        "hidden": spec.hidden,
    }


def _completion_hint(previous_steps: list[dict[str, Any]]) -> str:
    for step in reversed(previous_steps):
        result = step.get("tool_result") if isinstance(step, dict) else None
        if isinstance(result, dict) and result.get("status") == "success":
            return "IMPORTANT: A previous tool call already succeeded for this task. Return final now."
    return "If no tool has been called yet, choose the single best next tool call."


def _compact_schema(schema: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"type": schema.get("type", "object")}
    for key in ("required", "required_any"):
        if key in schema:
            compact[key] = schema[key]
    properties = schema.get("properties")
    if isinstance(properties, dict):
        compact["properties"] = {
            name: _compact_property(value)
            for name, value in properties.items()
            if isinstance(value, dict)
        }
    return compact


def _compact_property(schema: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key in ("type", "enum", "items"):
        if key in schema:
            value[key] = schema[key]
    return value
