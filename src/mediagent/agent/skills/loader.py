"""Local Markdown SKILL loader."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentSkill:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    default_dry_run: bool
    risk_level: str
    requires_initial_tool_call: bool
    supports_unbounded: bool
    supported_intents: tuple[str, ...]
    unsupported_intents: tuple[str, ...]
    body: str
    source: str

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_tools": list(self.allowed_tools),
            "default_dry_run": self.default_dry_run,
            "risk_level": self.risk_level,
            "requires_initial_tool_call": self.requires_initial_tool_call,
            "supports_unbounded": self.supports_unbounded,
            "supported_intents": list(self.supported_intents),
            "unsupported_intents": list(self.unsupported_intents),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.summary(), "body": self.body, "source": self.source}


class SkillRegistry:
    def __init__(self, skills: list[AgentSkill] | None = None) -> None:
        self._skills = {skill.name: skill for skill in skills or []}

    def list(self) -> list[AgentSkill]:
        return [self._skills[name] for name in sorted(self._skills)]

    def get(self, name: str) -> AgentSkill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"Unknown skill: {name}") from exc

    def summaries(self) -> list[dict[str, Any]]:
        return [skill.summary() for skill in self.list()]


def default_skill_registry() -> SkillRegistry:
    package = "mediagent.agent.skills.builtin"
    skills: list[AgentSkill] = []
    for item in resources.files(package).iterdir():
        if item.name.endswith(".md"):
            skills.append(load_skill_text(item.read_text(encoding="utf-8"), source=item.name))
    return SkillRegistry(skills)


def load_skill_file(path: Path) -> AgentSkill:
    return load_skill_text(path.read_text(encoding="utf-8"), source=str(path))


def load_skill_text(text: str, *, source: str = "<memory>") -> AgentSkill:
    metadata, body = _split_frontmatter(text)
    required = ("name", "description", "allowed_tools", "default_dry_run", "risk_level")
    missing = [key for key in required if key not in metadata]
    if missing:
        raise ValueError(f"SKILL is missing required fields: {missing}")
    allowed_tools = metadata["allowed_tools"]
    if not isinstance(allowed_tools, list) or not all(isinstance(item, str) for item in allowed_tools):
        raise ValueError("SKILL allowed_tools must be a list of strings.")
    supported_intents = _optional_string_list(metadata.get("supported_intents"), "supported_intents")
    unsupported_intents = _optional_string_list(metadata.get("unsupported_intents"), "unsupported_intents")
    return AgentSkill(
        name=str(metadata["name"]),
        description=str(metadata["description"]),
        allowed_tools=tuple(allowed_tools),
        default_dry_run=bool(metadata["default_dry_run"]),
        risk_level=str(metadata["risk_level"]),
        requires_initial_tool_call=bool(metadata.get("requires_initial_tool_call", False)),
        supports_unbounded=bool(metadata.get("supports_unbounded", False)),
        supported_intents=tuple(supported_intents),
        unsupported_intents=tuple(unsupported_intents),
        body=body.strip(),
        source=source,
    )


def _optional_string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"SKILL {name} must be a list of strings.")
    return value


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL must start with YAML-like frontmatter.")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("SKILL frontmatter is not closed.") from exc
    return _parse_simple_yaml(lines[1:end]), "\n".join(lines[end + 1 :])


def _parse_simple_yaml(lines: list[str]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - "):
            if current_key is None:
                raise ValueError("YAML list item without a key.")
            data.setdefault(current_key, []).append(line[4:].strip())
            continue
        if ":" not in line:
            raise ValueError(f"Unsupported YAML line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        current_key = key
        if value == "":
            data[key] = []
        elif value.lower() == "true":
            data[key] = True
        elif value.lower() == "false":
            data[key] = False
        else:
            data[key] = value.strip('"').strip("'")
    return data
