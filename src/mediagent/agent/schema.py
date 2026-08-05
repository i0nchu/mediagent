"""Agent Core V1 data contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mediagent.core.redaction import redact_secrets, redact_text


class AgentStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    NEEDS_USER = "needs_user"


class AgentActionType(StrEnum):
    CALL_TOOL = "call_tool"
    FINAL = "final"
    ASK_USER = "ask_user"


@dataclass(frozen=True)
class AgentError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": redact_text(self.message),
            "details": redact_secrets(self.details),
        }


@dataclass(frozen=True)
class AgentAction:
    action: AgentActionType
    tool: str | None = None
    input: dict[str, Any] = field(default_factory=dict)
    dry_run: bool | None = None
    reason: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action.value}
        if self.tool is not None:
            payload["tool"] = self.tool
        if self.input:
            payload["input"] = redact_secrets(self.input)
        if self.dry_run is not None:
            payload["dry_run"] = self.dry_run
        if self.reason:
            payload["reason"] = redact_text(self.reason)
        if self.message:
            payload["message"] = redact_text(self.message)
        return payload


@dataclass
class AgentStep:
    index: int
    action: AgentAction
    tool_result: dict[str, Any] | None = None
    error: AgentError | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action.to_dict(),
            "tool_result": redact_secrets(self.tool_result) if self.tool_result is not None else None,
            "error": self.error.to_dict() if self.error else None,
        }


@dataclass
class AgentRunResult:
    status: AgentStatus
    task: str
    skill: str | None
    dry_run: bool
    steps: list[AgentStep] = field(default_factory=list)
    message: str | None = None
    error: AgentError | None = None

    @property
    def is_success(self) -> bool:
        return self.status == AgentStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "task": redact_text(self.task),
            "skill": self.skill,
            "dry_run": self.dry_run,
            "steps": [step.to_dict() for step in self.steps],
            "message": redact_text(self.message) if self.message else None,
            "error": self.error.to_dict() if self.error else None,
        }
